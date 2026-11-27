# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The averaging override on the meter (FW-06, ``docs/architecture.md`` §3.2).

``SENSe:AVERage`` replaces a profile's averaging count and shunt conversion
time, and its ``ADC_CONFIG`` word and period follow. On the meter:

* ``STATE`` at AVG 16 and at AVG 64 streams for :data:`CAPTURE_S` at the period
  the word gives, paced by the ALERT pin, with no read error, no dropped
  sample, ``pace_err_ppm`` inside ``STATE``'s band and the header's keys its
  own; off again it reads 1152 us;
* the standard deviation on the null path falls from AVG 1 to 16 to 64;
* the periods next to each limit the refusals state are kept: a limit nothing
  can hold would show here;
* ``BURSt`` and a period past a limit are refused, ``*RST`` turns every
  override off, and ``DIAGnostic:HEAP?`` counts nothing.

    pytest tests/bench/test_averaging_bench.py --run-bench \\
        --meter-port "$KLEIDOS_METER_PORT" --bench-lock <lock file>
"""

from __future__ import annotations

import time

import pytest
from bench_support import since_main_problems
from kleidos_meter.client import Meter

#: How long each capture runs.
CAPTURE_S = 15.0

#: The band ``pace_err_ppm`` is filed in for an alert-paced capture, the
#: INA228's oscillator against the S3's crystal, and for ``TRACE``'s timer
#: (``test_i2c_bench.PACE_BANDS``).
ALERT_BAND = (40, 130)
TIMER_BAND = (-40, 50)

#: ``STATE``'s word, count and period, off and at the counts of the case.
STATE_OFF = ("0xF140", 1, 1152)
STATE_ON = {16: ("0xF142", 18_432), 64: ("0xF143", 73_728)}

#: Next to each limit: the profile, the bus clock, the count, the conversion
#: time in seconds, and the period they give.
EDGES = (
    ("STAT", 400_000, 4, "0.00028", 1520),
    ("STAT", 1_000_000, 4, "0.00005", 600),
    ("STAT", 1_000_000, 1, "0.00054", 640),
    ("TRAC", 400_000, 4, "0.00005", 200),
    ("TRAC", 1_000_000, 1, "0.00015", 150),
    ("SLE", 400_000, 512, "0.00028", 194_560),
)

#: Past each limit: the same, and how the refusal names it.
PAST = (
    ("STAT", 400_000, 1, "0.00054", "under the 1051 us its reads need at 400000 Hz"),
    ("STAT", 1_000_000, 1, "0.00028", "under the 421 us its reads need at 1000000 Hz"),
    ("TRAC", 400_000, 1, "0.00015", "under the 191 us its reads need at 400000 Hz"),
    ("TRAC", 1_000_000, 1, "0.000084", "under the 100 us its reads need at 1000000 Hz"),
    ("SLE", 400_000, 64, "0.00412", "over the 200000 us its pacing allows"),
)


def rest(meter: Meter) -> None:
    """Closes any window and puts the instrument at its defaults, every override off."""
    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    meter.exchange(["*RST", "*CLS", "FORM TEXT"], timeout=20.0)


def override(meter: Meter, profile: str, count: int, seconds: str) -> None:
    """Turns *profile*'s override on at *count* and *seconds*."""
    meter.exchange(
        [
            f"SENS:PROF {profile}",
            "SENS:AVER OFF",
            f"SENS:AVER:COUN {count}",
            f"SENS:CURR:APER {seconds}",
            "SENS:AVER ON",
        ]
    )


def capture(
    meter: Meter, kept: str = "DATA", start: str = "INIT"
) -> tuple[dict[str, str], dict[str, str]]:
    """A capture of :data:`CAPTURE_S` on the live profile: its header and its statistics.

    :param kept: ``SENSe:CAPTure``'s word: the stream, or the statistics only.
    :param start: ``INIT``, or ``INIT:FORC`` on the null path.
    """
    meter.exchange([f"SENS:CAPT {kept}", start], timeout=20.0)
    header = meter.report("FETC:HEAD?", timeout=20.0)
    time.sleep(CAPTURE_S)
    meter.exchange(["ABOR"], timeout=20.0)
    stats = meter.report("FETC:STAT?", timeout=20.0)
    return header, stats


def clean(stats: dict[str, str]) -> None:
    """A capture that lost nothing and kept its own pace.

    One stale edge is allowed: a conversion that ends while the start's own
    ``DIAG_ALRT`` read releases the pin leaves its edge with no flag behind it
    (``docs/architecture.md`` §3.2), whatever the period.
    """
    assert int(stats["samples"]) > 0, stats
    for key in ("read_err", "dropped", "pace_fallback", "pace_unmet"):
        assert stats[key] == "0", (key, stats)
    assert int(stats["stale_edges"]) <= 1, stats


@pytest.mark.bench
@pytest.mark.parametrize("count", list(STATE_ON))
def test_a_state_stream_runs_at_the_period_its_word_gives(meter: Meter, count: int) -> None:
    rest(meter)
    word, period = STATE_ON[count]
    override(meter, "STAT", count, "0.001052")
    report = meter.report("SENS:PROF:REP?")
    assert (report["adc_config"], report["period_us"]) == (word, str(period)), report
    assert (report["avg"], report["avg_state"]) == (str(count), "on"), report
    header, stats = capture(meter)
    print(f"AVG {count}: {header} {stats}")
    assert (header["adc_config"], header["period_us"]) == (word, str(period)), header
    assert (header["vbusct_us"], header["vshct_us"], header["vtct_us"]) == ("50", "1052", "50")
    assert (header["avg"], header["avg_state"], header["pace"]) == (str(count), "on", "alert")
    clean(stats)
    low, high = ALERT_BAND
    assert low <= int(stats["pace_err_ppm"]) <= high, stats
    meter.exchange(["SENS:AVER OFF"])
    report = meter.report("SENS:PROF:REP?")
    word, own, period = STATE_OFF
    assert (report["adc_config"], report["avg"]) == (word, str(own)), report
    assert (report["period_us"], report["avg_state"]) == (str(period), "off"), report
    header, _ = capture(meter, "STAT")
    assert (header["adc_config"], header["period_us"]) == (word, str(period)), header
    assert header["avg_state"] == "off", header
    assert meter.query("SYST:ERR:COUN?") == "0"
    assert since_main_problems(f"AVG {count}", meter.report("DIAG:HEAP?")) == []
    rest(meter)


@pytest.mark.bench
def test_the_null_paths_deviation_falls_with_the_count(meter: Meter) -> None:
    rest(meter)
    found: dict[int, dict[str, str]] = {}
    for count in (1, 16, 64):
        # The hold of a null path is shorter than the three captures.
        meter.exchange(["ROUT:NULL ON"], timeout=20.0)
        try:
            meter.exchange(["SENS:PROF STAT", "SENS:AVER OFF"])
            if count != 1:
                override(meter, "STAT", count, "0.001052")
            _, stats = capture(meter, "STAT", start="INIT:FORC")
        finally:
            meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
            meter.exchange(["ROUT:NULL OFF"], timeout=20.0)
        print(f"null path, AVG {count}: {stats}")
        clean(stats)
        found[count] = stats
    deviations = {count: float(stats["sd_na"]) for count, stats in found.items()}
    print(f"null path: sd_na {deviations}")
    assert deviations[64] < deviations[16] < deviations[1], deviations
    assert meter.report("ROUT:STAT?")["path"] == "shunt"
    assert meter.query("SYST:ERR:COUN?") == "0"
    rest(meter)


@pytest.mark.bench
@pytest.mark.parametrize(("profile", "hz", "count", "seconds", "period"), EDGES)
def test_a_period_next_to_a_limit_is_kept(
    meter: Meter, profile: str, hz: int, count: int, seconds: str, period: int
) -> None:
    rest(meter)
    meter.exchange([f"SYST:COMM:I2C:FREQ {hz}"], timeout=20.0)
    override(meter, profile, count, seconds)
    header, stats = capture(meter, "STAT")
    print(f"{profile} {count} x {seconds} at {hz}: {stats}")
    assert header["period_us"] == str(period), header
    clean(stats)
    assert header["pace"] == ("timer" if profile == "TRAC" else "alert"), header
    low, high = TIMER_BAND if profile == "TRAC" else ALERT_BAND
    assert low <= int(stats["pace_err_ppm"]) <= high, stats
    assert meter.query("SYST:ERR:COUN?") == "0"
    rest(meter)


@pytest.mark.bench
@pytest.mark.parametrize(("profile", "hz", "count", "seconds", "reason"), PAST)
def test_a_period_past_a_limit_is_refused_and_changes_nothing(
    meter: Meter, profile: str, hz: int, count: int, seconds: str, reason: str
) -> None:
    rest(meter)
    meter.exchange([f"SYST:COMM:I2C:FREQ {hz}", f"SENS:PROF {profile}"], timeout=20.0)
    own = meter.report("SENS:PROF:REP?")
    meter.exchange([f"SENS:AVER:COUN {count}", f"SENS:CURR:APER {seconds}"])
    _, errors, _ = meter.exchange(["SENS:AVER ON"], raise_errors=False)
    assert [error.code for error in errors] == [-221] and reason in str(errors[0]), errors
    assert meter.report("SENS:PROF:REP?") == own
    rest(meter)


@pytest.mark.bench
def test_burst_has_none_and_a_reset_turns_every_override_off(meter: Meter) -> None:
    rest(meter)
    _, errors, _ = meter.exchange(["SENS:PROF BURS", "SENS:AVER ON"], raise_errors=False)
    assert [error.code for error in errors] == [-221], errors
    assert "BURSt has no averaging override" in str(errors[0])
    for profile in ("TRAC", "STAT", "SLE"):
        meter.exchange([f"SENS:PROF {profile}", "SENS:AVER ON"])
        assert meter.query("SENS:AVER?") == "1"
    meter.exchange(["*RST"], timeout=20.0)
    for profile in ("TRAC", "STAT", "SLE"):
        meter.exchange([f"SENS:PROF {profile}"])
        assert meter.query("SENS:AVER?") == "0"
    assert since_main_problems("every override", meter.report("DIAG:HEAP?")) == []
    rest(meter)
