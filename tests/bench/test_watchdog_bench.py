# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The firmware's own watchdog on the meter (FW-11).

Timer Group 0's MWDT resets the chip 5 s after its last feed, and it is fed
only once the console, the sampler and Core 0's idle task have all checked in
(``firmware/src/watchdog.h``, ``docs/architecture.md`` §5.1). On the meter:

* ``DIAGnostic:WATChdog:TEST`` stops the console's check-ins, and the chip
  resets within the timeout; the next ``SYSTem:HEALth?`` says
  ``reset=task_wdt``, the reset found no coil energized, the reset's
  ``pre_main_`` counts are the boot's of ``heap_expectations.toml``, and
  nothing allocated or freed since ``app_main()`` (FW-17);
* with ``--bench-long``, a 600 s idle and a full ``TRACE`` ring, captured and
  dumped, do not reset it, and the full ring and its dump allocate and free
  nothing (FW-17); the dump holds the whole ring and matches its CRC-32
  (FW-18, with PSRAM in the memory map only).

    pytest tests/bench/test_watchdog_bench.py --run-bench [--bench-long] \\
        --meter-port "$KLEIDOS_METER_PORT" --bench-lock <lock file>
"""

from __future__ import annotations

import time

import pytest
from bench_support import (
    CONSOLE_STACK_FLOOR,
    EXPECTATIONS,
    RING_SAMPLES,
    BenchLine,
    dump_whole,
    fill_ring,
    load_expectations,
    since_main_problems,
)
from kleidos_meter.client import Meter, MeterTimeout
from kleidos_meter.protocol import check_dump

#: What ``DIAGnostic:WATChdog?`` reads on a healthy instrument with no stall.
HEALTHY = "console=1 sampler=1 timeout_s=5 panic=1 stalls=0"

#: The longest a party may be gone before the chip resets: the 5 s timeout
#: plus the longest interval of the others (the sampler's 1 s idle slice),
#: and the shortest, the timeout less a console check-in interval. The boot
#: instant is read back from ``uptime_us``, whose clock starts after the ROM
#: and the bootloader have run, so the latest bound also has a second for
#: them and the line (the bench read 5.65 to 6.35 s, 2026-11-22).
EARLIEST_S = 3.9
LATEST_S = 6.1 + 1.0


def boot_instant(meter: Meter) -> tuple[float, dict[str, str]]:
    """When the running boot began, on this host's monotonic clock, and the health report."""
    health = meter.report("SYST:HEAL?")
    answered = time.monotonic()
    return answered - int(health["uptime_us"]) / 1e6, health


@pytest.mark.bench
def test_the_forced_hang_resets_within_the_timeout_and_is_named_task_wdt(
    meter: Meter, meter_line: BenchLine
) -> None:
    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    assert meter.query("DIAG:WATC?") == HEALTHY
    asked = time.monotonic()
    meter_line.write(b"DIAG:WATC:TEST\n")
    # Nothing is asked before the reset is over: the ROM prints its boot
    # banner at 115200 baud, which a 6 Mbaud reader takes for a run of NUL
    # bytes, and wait_ready() drains the line only before its first query.
    time.sleep(LATEST_S + 2.0)
    assert meter.wait_ready(timeout=20.0), "no power-on bit: the instrument did not reset"
    booted, health = boot_instant(meter)
    assert health["reset"] == "task_wdt", health
    assert health["reset_released"] == "-", health
    assert EARLIEST_S <= booted - asked <= LATEST_S, f"reset {booted - asked:.2f} s after the hang"
    assert meter.query("DIAG:WATC?") == HEALTHY
    heap = meter.report("DIAG:HEAP?")
    boot = load_expectations(EXPECTATIONS.read_text(encoding="utf-8")).boot
    for key in ("pre_main_allocs", "pre_main_frees"):
        assert int(heap[key]) == boot[key], f"{key} after the watchdog's reset: {heap[key]}"
    assert since_main_problems("the watchdog's reset", heap) == []


def assert_same_boot(meter: Meter, booted: float, reset: str) -> dict[str, str]:
    """The instrument has not reset since *booted*: the same boot instant and reason."""
    now, health = boot_instant(meter)
    assert abs(now - booted) < 0.5, f"the instrument reset: {health}"
    assert health["reset"] == reset
    return health


@pytest.mark.bench
@pytest.mark.bench_long
def test_a_600_s_idle_does_not_reset(meter: Meter) -> None:
    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    booted, health = boot_instant(meter)
    end = time.monotonic() + 600.0
    while time.monotonic() < end:
        time.sleep(min(30.0, max(0.0, end - time.monotonic())))
        assert_same_boot(meter, booted, health["reset"])
    assert meter.query("DIAG:WATC?") == HEALTHY


@pytest.mark.bench
@pytest.mark.bench_long
def test_a_full_trace_ring_and_its_dump_do_not_reset(meter: Meter) -> None:
    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    booted, health = boot_instant(meter)
    fill_ring(meter, lambda: assert_same_boot(meter, booted, health["reset"]))
    assert since_main_problems("a full ring", meter.report("DIAG:HEAP?")) == []
    meter.exchange(["ABOR"], timeout=20.0)
    # FW-18: the ring, in PSRAM that is no longer a heap, holds every sample
    # and dumps whole: the opening row counts the ring, and the block matches
    # the CRC-32 its closing row carries.
    block, _ = dump_whole(meter, "RAW", check_dump, MeterTimeout)
    assert len(block) > RING_SAMPLES
    assert since_main_problems("its dump", meter.report("DIAG:HEAP?")) == []
    stack_console = int(meter.report("SYST:HEAL?")["stack_console"])
    assert stack_console >= CONSOLE_STACK_FLOOR, f"stack_console={stack_console} after the dump"
    assert_same_boot(meter, booted, health["reset"])
    assert meter.query("DIAG:WATC?") == HEALTHY
    meter.exchange(["*RST"], raise_errors=False, timeout=20.0)
