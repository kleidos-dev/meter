# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The heap session of record on one boot (FW-32, ``docs/misra.md``).

The sweep (``test_heap_sweep.py``) holds what each command family adds; this
long case is the claim's session, all of it on one boot: the meter is rebooted
through the carrier's auto-reset, every step of the sweep runs (a capture of
every profile, a null and its save, the calibration saves, the clock changes,
the relay orders, 500 ``SYSTem:STATus?``) and, before the last ``*RST``, a
full ``TRACE`` ring is captured and dumped whole, ``RAW`` and then ``SCALed``,
each held to its CRC-32 and to the ring's count (a block the line damaged is
asked for again, and the record keeps that it was). ``DIAGnostic:HEAP?`` must
read ``allocs_since_main=0 frees_since_main=0`` after every step and at the
end, with the pre-main counts D1's, and the boot must be the same one
throughout: no reset in between could have started the counts over.

    pytest tests/bench/test_heap_session.py --run-bench --bench-long \\
        --meter-port "$KLEIDOS_METER_PORT" --bench-lock <lock file> \\
        [--bench-session-record session.json]
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from bench_support import (
    CONSOLE_STACK_FLOOR,
    EXPECTATIONS,
    BenchLine,
    dump_whole,
    fill_ring,
    load_expectations,
    phase_counts,
    run_step,
    since_main_problems,
    sweep_steps,
)
from kleidos_meter.client import Meter, MeterTimeout
from kleidos_meter.protocol import check_dump

#: How long the boot is let settle, as the sweep does.
SETTLE_S = 1.5


def boot_instant(meter: Meter) -> float:
    """When the running boot began, on this host's monotonic clock."""
    health = meter.report("SYST:HEAL?")
    return time.monotonic() - int(health["uptime_us"]) / 1e6


@pytest.mark.bench
@pytest.mark.bench_long
def test_a_whole_session_on_one_boot_allocates_and_frees_nothing(
    meter: Meter, meter_line: BenchLine, request: pytest.FixtureRequest
) -> None:
    expected = load_expectations(EXPECTATIONS.read_text(encoding="utf-8")).boot
    meter_line.reset()
    assert meter.wait_ready(timeout=20.0), "no power-on bit: the reset did not reboot the meter"
    time.sleep(SETTLE_S)
    info = meter.report("SYST:INF?")
    booted = boot_instant(meter)
    first = meter.report("DIAG:HEAP?")
    problems = since_main_problems("boot", first)
    *families, last = sweep_steps(meter.query("CAL:TAG?").strip('"'))
    assert last.name == "reset"
    for sweep_step in families:
        problems += run_step(meter, sweep_step)
        problems += since_main_problems(sweep_step.name, meter.report("DIAG:HEAP?"))

    fill_ring(meter, lambda: None)
    problems += since_main_problems("a full ring", meter.report("DIAG:HEAP?"))
    meter.exchange(["ABOR"], timeout=20.0)
    statistics = meter.report("FETC:STAT?")
    dumped: dict[str, int] = {}
    damaged: list[str] = []
    for form in ("RAW", "SCAL"):
        block, again = dump_whole(meter, form, check_dump, MeterTimeout)
        dumped[form] = len(block)
        damaged += again
        problems += since_main_problems(f"its {form} dump", meter.report("DIAG:HEAP?"))
    problems += run_step(meter, last)

    end = meter.report("DIAG:HEAP?")
    problems += since_main_problems("the session", end)
    for key in ("pre_main_allocs", "pre_main_frees"):
        for when, report in (("boot", first), ("the session", end)):
            if phase_counts(report)[key] != expected[key]:
                problems.append(f"after {when}: {key}={report[key]}, D1 holds {expected[key]}")
    assert abs(boot_instant(meter) - booted) < 0.5, "the instrument reset during the session"
    health = meter.report("SYST:HEAL?")
    if int(health["stack_console"]) < CONSOLE_STACK_FLOOR:
        problems.append(f"stack_console={health['stack_console']}, below {CONSOLE_STACK_FLOOR}")
    watchdog = meter.report("DIAG:WATC?")
    if watchdog.get("stalls") != "0":
        problems.append(f"the watchdog saw a stall: {watchdog}")

    record = request.config.getoption("--bench-session-record")
    if record:
        Path(record).write_text(
            json.dumps(
                {
                    "fw": info.get("fw"),
                    "elf": info.get("elf"),
                    "serial": info.get("serial"),
                    "boot": first,
                    "ring": statistics,
                    "dump_bytes": dumped,
                    "damaged_dumps": damaged,
                    "end": end,
                    "health": health,
                    "watchdog": watchdog,
                    "problems": problems,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    assert not problems, "\n".join(problems)
