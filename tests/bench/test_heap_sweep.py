# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The heap sweep on the meter: the boot's counts and every command family's.

The meter is rebooted through the carrier's auto-reset, its boot is let
settle, and ``DIAGnostic:HEAP?``'s four counts by phase are read; then every
command family of ``docs/scpi.md`` runs (:data:`bench_support.SWEEP`, with the
calibration save before the null), each followed by another
``DIAGnostic:HEAP?``. The boot's counts and what each family adds to the two
since-main counts must equal ``heap_expectations.toml``; the pre-main counts
never move after the boot. A count above the file is a new allocation, one
below it means a change removed one and the file is to be lowered in the same
commit (``test_bench_support.py`` holds the file to only falling). And from
FW-17 every report, the boot's and each step's, must read
``allocs_since_main=0 frees_since_main=0`` (and the 0.7.0 steady state's two
counts, the same window), with no exemption
(:func:`bench_support.since_main_problems`), and the console's stack must
keep at least 0.16.6's margin after it (:data:`bench_support.CONSOLE_STACK_FLOOR`).

    pytest tests/bench --run-bench --meter-port "$KLEIDOS_METER_PORT" \\
        --bench-lock <lock file> [--bench-record sweep.json]
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from bench_support import (
    CONSOLE_STACK_FLOOR,
    EXPECTATIONS,
    SAVE_STEP,
    BenchLine,
    compare,
    load_expectations,
    phase_counts,
    run_step,
    since_main_problems,
    sweep_steps,
)
from kleidos_meter.client import Meter

#: How long the boot is let settle after the instrument first answers. Since
#: FW-17 the counts start at ``app_main()``'s first line and nothing frees the
#: main task, so nothing is left to settle; the pause is kept so that a late
#: event, which there must not be, would be seen.
SETTLE_S = 1.5


@pytest.mark.bench
def test_the_heap_counts_of_the_boot_and_of_every_command_family_are_the_files(
    meter: Meter, meter_line: BenchLine, request: pytest.FixtureRequest
) -> None:
    expected = load_expectations(EXPECTATIONS.read_text(encoding="utf-8"))
    meter_line.reset()
    assert meter.wait_ready(timeout=20.0), "no power-on bit: the reset did not reboot the meter"
    time.sleep(SETTLE_S)
    info = meter.report("SYST:INF?")
    report = meter.report("DIAG:HEAP?")
    boot = phase_counts(report)
    unexpected: list[str] = since_main_problems("boot", report)

    previous = boot
    deltas: dict[str, dict[str, int]] = {}
    for sweep_step in sweep_steps(meter.query("CAL:TAG?").strip('"')):
        unexpected += run_step(meter, sweep_step)
        report = meter.report("DIAG:HEAP?")
        unexpected += since_main_problems(sweep_step.name, report)
        now = phase_counts(report)
        for key in ("pre_main_allocs", "pre_main_frees"):
            if now[key] != boot[key]:
                unexpected.append(f"{sweep_step.name}: {key} moved from {boot[key]} to {now[key]}")
        deltas[sweep_step.name] = {
            "allocs": now["allocs_since_main"] - previous["allocs_since_main"],
            "frees": now["frees_since_main"] - previous["frees_since_main"],
        }
        previous = now
    assert SAVE_STEP in deltas
    stack_console = int(meter.report("SYST:HEAL?")["stack_console"])
    if stack_console < CONSOLE_STACK_FLOOR:
        unexpected.append(
            f"stack_console={stack_console}, below 0.16.6's margin of {CONSOLE_STACK_FLOOR}"
        )

    record = request.config.getoption("--bench-record")
    if record:
        Path(record).write_text(
            json.dumps(
                {
                    "fw": info.get("fw"),
                    "elf": info.get("elf"),
                    "serial": info.get("serial"),
                    "boot": boot,
                    "steps": deltas,
                    "end": previous,
                    "unexpected_errors": unexpected,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    problems = unexpected + compare(expected, boot, deltas)
    measured = "\n".join(
        [f"boot: {boot}"] + [f"{name}: {counts}" for name, counts in deltas.items()]
    )
    assert not problems, "\n".join(problems) + "\n\nmeasured:\n" + measured
