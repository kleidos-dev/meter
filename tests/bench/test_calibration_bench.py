# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The calibration in the flash log (FW-16), on the meter.

From FW-16 the calibration is the newest record of the firmware's own log over
the ``calib`` partition (``firmware/src/flash_log.h``, ``flash_port.h``), and
a save allocates nothing. The bench asks:

* :data:`SAVES` saves that each change the stored record (the tag altered and
  put back, as the heap sweep's save step does) leave every count of
  ``DIAGnostic:HEAP?`` as it was, and none queues an error;
* the last one survives a reboot through the carrier's auto-reset, the
  instrument answers ``*TST?`` 0 after it (bit 128: the stored calibration
  reads back), and the calibration ends as it began, so
  ``kleidos-meter calibration verify`` still answers ``applied``.

    pytest tests/bench/test_calibration_bench.py --run-bench \\
        --meter-port "$KLEIDOS_METER_PORT" --bench-lock <lock file>
"""

from __future__ import annotations

import time

import pytest
from bench_support import BenchLine, altered
from kleidos_meter.client import Meter

#: The saves that change the record.
SAVES = 20

#: How long the boot is let settle after the reset, as the heap sweep does.
SETTLE_S = 1.5

#: The counts of ``DIAGnostic:HEAP?``.
HEAP_COUNTS = (
    "armed",
    "allocs_after_init",
    "frees_after_init",
    "maint_allocs",
    "maint_frees",
    "pre_main_allocs",
    "pre_main_frees",
    "allocs_since_main",
    "frees_since_main",
)


def counts(meter: Meter) -> dict[str, str]:
    """``DIAGnostic:HEAP?``'s counts, with nothing else of the report."""
    report = meter.report("DIAG:HEAP?")
    return {key: report[key] for key in HEAP_COUNTS}


@pytest.mark.bench
def test_saves_that_change_the_record_allocate_nothing_and_the_last_survives_a_reboot(
    meter: Meter, meter_line: BenchLine
) -> None:
    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    meter.exchange(["*CLS"])
    tag = meter.query("CAL:TAG?").strip('"')
    data = meter.query("CAL:DATA?")
    before = counts(meter)
    for i in range(SAVES):
        written = altered(tag) if i % 2 == 0 else tag
        meter.exchange([f'CAL:TAG "{written}"'])
        assert meter.query("CAL:TAG?").strip('"') == written
    assert meter.query("SYST:ERR:COUN?") == "0", meter.query("SYST:ERR:ALL?")
    after = counts(meter)
    assert after == before, f"the saves moved the heap counts: {before} -> {after}"

    # The last save wrote the tag back as it was; one more alters it, and a
    # reboot must find that one, then it is put back and survives too.
    meter.exchange([f'CAL:TAG "{altered(tag)}"'])
    meter_line.reset()
    assert meter.wait_ready(timeout=20.0), "no power-on bit: the reset did not reboot the meter"
    time.sleep(SETTLE_S)
    assert meter.query("CAL:TAG?").strip('"') == altered(tag)
    meter.exchange([f'CAL:TAG "{tag}"'])
    meter_line.reset()
    assert meter.wait_ready(timeout=20.0), "no power-on bit: the reset did not reboot the meter"
    time.sleep(SETTLE_S)
    assert meter.query("CAL:TAG?").strip('"') == tag
    assert meter.query("CAL:DATA?") == data, "the calibration did not end as it began"
    assert meter.query("*TST?", timeout=10.0) == "0"
    assert meter.query("SYST:ERR:COUN?") == "0", meter.query("SYST:ERR:ALL?")
