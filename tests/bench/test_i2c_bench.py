# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The INA228 on the own I2C master (FW-14), held to 0.16.5's acceptance.

From FW-14 every transaction is the firmware's own I2C master's, polled to its
end on Core 1, and a change of the bus clock rewrites its timing registers
(``firmware/src/i2c_bus.h``, ``docs/architecture.md`` §2.4). The bench asks of
it what it asked of ESP-IDF's driver on 2026-11-21 (``CHANGELOG.md`` 0.16.5):

* ``*TST?`` answers 0 in 100 of 100;
* 20 scans at each of 400, 100 and 50 kHz list ``0x40`` alone;
* 200 rounds of ``MANUFACTURER_ID``, ``DEVICE_ID`` and ``DIAG_ALRT``'s
  ``MEMSTAT`` read right;
* each of the four profiles (``BURST`` at 1 MHz) captures for
  :data:`CAPTURE_S` with no read error, no dropped sample and no stale edge,
  ``STATE`` and ``SLEEP`` paced by the ALERT pin, and ``pace_err_ppm`` inside
  the band :data:`PACE_BANDS` files for it;
* the null path's floor is no noisier than :data:`FLOOR_SD_NA`, as far as one
  capture can tell (:data:`FLOOR_ERRORS`);
* three clock changes leave every count of ``DIAGnostic:HEAP?`` as it was.

    pytest tests/bench/test_i2c_bench.py --run-bench \\
        --meter-port "$KLEIDOS_METER_PORT" --bench-lock <lock file>
"""

from __future__ import annotations

import math
import time

import pytest
from kleidos_meter.client import Meter

#: The self-test rounds, the scans per clock, and the identity rounds.
TST_ROUNDS = 100
SCAN_ROUNDS = 20
SCAN_CLOCKS = (400_000, 100_000, 50_000)
ID_ROUNDS = 200

#: What a scan answers with the breakout alone on the bus.
SCAN = "expected=0x40 found=0x40 n=1"

#: How long each profile's statistics-only capture runs.
CAPTURE_S = 15.0

#: The band ``pace_err_ppm`` is filed in, by profile, from the bench records
#: of ``docs/architecture.md`` (§3.4 and §5.2, 0.9.0 to 0.16.5): an
#: alert-paced capture reads the INA228's own oscillator against the S3's
#: crystal, +55 to +104 ppm on this part; a timer-paced one whether the loop
#: kept its deadline, -18 to +32 ppm for ``TRACE`` and 0 to +120 ppm for
#: ``BURST`` at 1 MHz from 0.14.0. Each band is the filed range widened by a
#: margin, never moved to fit a reading.
PACE_BANDS = {
    "STAT": (40, 130),
    "SLE": (40, 130),
    "TRAC": (-40, 50),
    "BURS": (-40, 150),
}

#: The profiles paced by the ALERT pin.
ALERT_PACED = ("STAT", "SLE")

#: The floor: the sample standard deviation of a ``STATE`` capture, 19.0 uA
#: with the radio off on the NW-00 spike's bench (``docs/architecture.md``
#: §5.2), in nanoamps. The null path's, with the DUT's lead open and the
#: shunt bypassed, may not be above it.
FLOOR_SD_NA = 19_000

#: The standard errors of its own estimate one capture may read above the
#: floor. A capture of n samples estimates the deviation to ``1 / sqrt(2(n -
#: 1))`` of it, 0.6 % for the 13,100 of 15 s of ``STATE``: on 2026-11-25
#: twenty captures of one rig read 18.90 uA in the mean and 18.61 to 19.17
#: one by one, five of them above 19.0, on two images alike
#: (``docs/architecture.md`` §2.6). Three errors put the limit at 19.35 uA.
FLOOR_ERRORS = 3.0

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


def rest(meter: Meter) -> None:
    """Closes any window and puts the instrument at its defaults."""
    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    meter.exchange(["*RST", "*CLS", "FORM TEXT"], timeout=20.0)


def capture(
    meter: Meter, profile: str, start: str = "INIT"
) -> tuple[dict[str, str], dict[str, str]]:
    """A statistics-only capture of :data:`CAPTURE_S` on *profile*: its header and statistics.

    *start* is ``INIT``, or ``INIT:FORC`` on the null path, whose bypassed
    shunt a plain ``INITiate`` refuses (every report on it says ``q=invalid``).
    """
    meter.exchange([f"SENS:PROF {profile}", "SENS:CAPT STAT", start], timeout=20.0)
    header = meter.report("FETC:HEAD?")
    time.sleep(CAPTURE_S)
    meter.exchange(["ABOR"], timeout=20.0)
    return header, meter.report("FETC:STAT?")


@pytest.mark.bench
def test_the_self_test_passes_a_hundred_times_in_a_hundred(meter: Meter) -> None:
    rest(meter)
    answers = [meter.query("*TST?", timeout=10.0) for _ in range(TST_ROUNDS)]
    failed = [i for i, answer in enumerate(answers) if answer != "0"]
    assert not failed, f"*TST? failed at rounds {failed}: {[answers[i] for i in failed]}"
    assert meter.query("SYST:ERR:COUN?") == "0"


@pytest.mark.bench
@pytest.mark.parametrize("hz", SCAN_CLOCKS)
def test_every_scan_finds_the_part_alone_and_leaves_it_readable(meter: Meter, hz: int) -> None:
    rest(meter)
    meter.exchange([f"SYST:COMM:I2C:FREQ {hz}"], timeout=20.0)
    assert meter.query("SYST:COMM:I2C:FREQ?") == str(hz)
    for _ in range(SCAN_ROUNDS):
        assert meter.query("DIAG:I2C:SCAN?") == SCAN
        assert meter.query("DIAG:REG? #H3E") == "addr=0x3E bytes=2 raw=0x5449"
    assert meter.query("SYST:ERR:COUN?") == "0"
    rest(meter)


@pytest.mark.bench
def test_the_identity_reads_right_two_hundred_times(meter: Meter) -> None:
    rest(meter)
    for _ in range(ID_ROUNDS):
        assert meter.query("DIAG:REG? #H3E") == "addr=0x3E bytes=2 raw=0x5449"
        assert meter.query("DIAG:REG? #H3F") == "addr=0x3F bytes=2 raw=0x2281"
        assert meter.report("DIAG:ALER?")["memstat"] == "1"
    assert meter.query("SYST:ERR:COUN?") == "0"


@pytest.mark.bench
@pytest.mark.parametrize("profile", list(PACE_BANDS))
def test_each_profile_captures_clean_and_keeps_its_pace(meter: Meter, profile: str) -> None:
    rest(meter)
    if profile == "BURS":
        meter.exchange(["SYST:COMM:I2C:FREQ 1000000"], timeout=20.0)
    header, stats = capture(meter, profile)
    print(f"{profile}: {header.get('pace')} {stats}")
    assert int(stats["samples"]) > 0
    assert stats["read_err"] == "0", stats
    assert stats["dropped"] == "0", stats
    assert stats["stale_edges"] == "0", stats
    if profile in ALERT_PACED:
        assert header["pace"] == "alert", header
    low, high = PACE_BANDS[profile]
    assert low <= int(stats["pace_err_ppm"]) <= high, stats
    assert meter.query("SYST:ERR:COUN?") == "0"
    rest(meter)


@pytest.mark.bench
def test_the_null_paths_floor_is_no_noisier_than_filed(meter: Meter) -> None:
    rest(meter)
    meter.exchange(["ROUT:NULL ON"], timeout=20.0)
    try:
        _, stats = capture(meter, "STAT", start="INIT:FORC")
    finally:
        meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
        meter.exchange(["ROUT:NULL OFF"], timeout=20.0)
    print(f"null path: {stats}")
    assert stats["read_err"] == "0" and stats["dropped"] == "0", stats
    allowance = 1.0 + FLOOR_ERRORS / math.sqrt(2.0 * (int(stats["samples"]) - 1))
    assert float(stats["sd_na"]) <= FLOOR_SD_NA * allowance, stats
    assert meter.report("ROUT:STAT?")["path"] == "shunt"
    assert meter.query("SYST:ERR:COUN?") == "0"


@pytest.mark.bench
def test_three_clock_changes_leave_every_heap_count_as_it_was(meter: Meter) -> None:
    rest(meter)
    before = meter.report("DIAG:HEAP?")
    for hz in (100_000, 1_000_000, 400_000):
        meter.exchange([f"SYST:COMM:I2C:FREQ {hz}"], timeout=20.0)
        assert meter.query("SYST:COMM:I2C:FREQ?") == str(hz)
        assert meter.query("DIAG:REG? #H3E") == "addr=0x3E bytes=2 raw=0x5449"
    after = meter.report("DIAG:HEAP?")
    moved = {key: (before.get(key), after.get(key)) for key in HEAP_COUNTS}
    moved = {key: pair for key, pair in moved.items() if pair[0] != pair[1]}
    assert not moved, f"DIAGnostic:HEAP? counts moved: {moved}"
