# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The simulator's reports, parameters and acquisitions, as the firmware has them.

``test_conformance.py`` holds the two together by signature; here are the values
the simulator computes itself and the refusals of one command at a time, which
every run checks without the firmware's bridge.
"""

from __future__ import annotations

import pytest
from conftest import FakeClock
from kleidos_meter.protocol import parse_errors, parse_fields
from kleidos_meter.sim import SimulatedMeter


def ask(sim: SimulatedMeter, message: str) -> list[str]:
    """The response line of *message*, its units."""
    sim.write((message + "\n").encode())
    out = sim.read(1 << 24).decode().strip()
    return out.split(";") if out else []


def codes(sim: SimulatedMeter, message: str) -> list[int]:
    """The error codes *message* queues."""
    sim.write((message + "\n").encode())
    sim.read(1 << 24)
    sim.write(b"SYST:ERR:ALL?\n")
    return [entry.code for entry in parse_errors(sim.read(1 << 20).decode().strip())]


def refusals(code: int) -> list[int]:
    """The codes a message that earns *code* queues: none for 0."""
    return [code] if code else []


def report(sim: SimulatedMeter, query: str) -> dict[str, str]:
    """A report's fields."""
    return parse_fields(ask(sim, query)[0])


# -- errors --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bit", "edges"),
    [
        (0x20, (-100, -199)),  # command error
        (0x10, (-200, -299)),  # execution error
        (0x08, (-300, -399, -500, 1, 117)),  # device-dependent
        (0x04, (-400, -499)),  # query error
    ],
)
def test_an_error_sets_the_bit_of_its_class(bit: int, edges: tuple[int, ...]) -> None:
    """IEEE 488.2's classes, at their edges, as ``esrBitFor()`` (scpi.cpp) has them."""
    for code in edges:
        sim = SimulatedMeter()
        sim.esr = 0
        sim._fail(code, "planted")
        assert ask(sim, "*ESR?") == [str(bit)], code


# -- reports -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "word", "period", "shunt", "averages"),
    [
        ("TRAC", "0xA0C0", "280", "280", "1"),
        ("STAT", "0xF140", "1152", "1052", "1"),
        ("SLE", "0xF1C2", "67520", "4120", "16"),
        ("BURS", "0xA000", "100", "50", "1"),
    ],
)
def test_the_profile_report_is_the_profile_table(
    profile: str, word: str, period: str, shunt: str, averages: str
) -> None:
    sim = SimulatedMeter()
    ask(sim, f"SENS:PROF {profile}")
    fields = report(sim, "SENS:PROF:REP?")
    assert (fields["adc_config"], fields["period_us"]) == (word, period)
    assert (fields["vshct_us"], fields["avg"]) == (shunt, averages)


def test_a_range_switch_reports_what_it_discarded() -> None:
    sim = SimulatedMeter()
    ask(sim, "SENS:CURR:RANG:IND 1")
    fields = report(sim, "SENS:CURR:RANG:REP?")
    # No capture has written a profile's word: the part converts at its reset word's period.
    assert (fields["ceiling_ma"], fields["rstacc"]) == ("409", "1")
    assert (fields["discard_us"], fields["cnvrf"]) == ("3156", "1")
    ask(sim, "SENS:CURR:RANG:IND 1")
    assert report(sim, "SENS:CURR:RANG:REP?")["rstacc"] == "0"
    ask(sim, "SENS:PROF STAT;:INIT;:ABOR;:SENS:CURR:RANG:IND 0")
    fields = report(sim, "SENS:CURR:RANG:REP?")
    assert (fields["ceiling_ma"], fields["discard_us"]) == ("1638", "1152")


def test_a_captures_header_names_its_sink_its_trigger_and_its_relays() -> None:
    sim = SimulatedMeter()
    ask(sim, "ROUT:BYP ON")
    ask(sim, "ROUT:BYP OFF;:SENS:PROF STAT;:INIT")
    header = report(sim, "FETC:HEAD?")
    assert (header["sink"], header["ring"], header["adc_config"]) == ("stream", "0", "0xF140")
    assert (header["k2"], header["path"], header["check"]) == ("bypass:open", "shunt", "-")
    assert (header["trig_source"], header["trig_level_na"]) == ("imm", "-")
    ask(sim, "ABOR;:SENS:CAPT STAT;:SENS:CURR:RANG:IND 1;:INIT;:ABOR")
    header = report(sim, "FETC:HEAD?")
    assert (header["sink"], header["config"]) == ("stats", "0x0010")
    ask(sim, "SENS:CAPT DATA;:SENS:PROF TRAC;:TRIG:SOUR LEV;:TRIG:LEV 0.1;:TRIG:SLOP NEG")
    ask(sim, "INIT;:ABOR")
    header = report(sim, "FETC:HEAD?")
    assert (header["sink"], header["trig_source"]) == ("ring", "level")
    assert (header["trig_level_na"], header["trig_slope"]) == ("100000000", "neg")
    ask(sim, "TRIG:SLOP POS;:INIT;:ABOR")
    assert report(sim, "FETC:HEAD?")["trig_slope"] == "pos"


def test_the_calibration_report_carries_each_ranges_null() -> None:
    sim = SimulatedMeter()
    fields = report(sim, "CAL:DATA?")
    assert (fields["off_pv_r0"], fields["off_die_mc_r0"]) == ("0", "0")
    ask(sim, "SENS:CURR:RANG:IND 1;:ROUT:NULL ON;:CAL:ZERO 1;:ABOR;:ROUT:NULL OFF")
    fields = report(sim, "CAL:DATA?")
    assert (fields["off_pv_r0"], fields["off_die_mc_r0"]) == ("0", "0")
    assert (fields["off_pv_r1"], fields["off_die_mc_r1"]) == ("-449746", "25000")


def test_the_route_result_is_the_last_orders() -> None:
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep)
    fields = report(sim, "ROUT:RES?")
    assert (fields["moved"], fields["settle_us"], fields["hold_s"]) == ("-", "0", "-")
    ask(sim, "ROUT:NULL ON")
    fields = report(sim, "ROUT:RES?")
    assert (fields["moved"], fields["settle_us"]) == ("k2,k1", "40000")
    assert (fields["live"], fields["marked"], fields["hold_s"]) == ("0", "0", "120")
    ask(sim, "ROUT:NULL OFF;:SENS:PROF STAT;:INIT;:ROUT:BYP ON,FORC,LIVE")
    fields = report(sim, "ROUT:RES?")
    assert (fields["live"], fields["marked"], fields["settle_us"]) == ("1", "1", "20000")
    ask(sim, "ROUT:BYP ON,FORC,LIVE")  # an order that moves nothing
    fields = report(sim, "ROUT:RES?")
    assert (fields["moved"], fields["live"], fields["settle_us"]) == ("-", "0", "0")
    ask(sim, "ROUT:LEAD OPEN")  # refused under the capture without LIVE
    assert codes(sim, "ROUT:LEAD OPEN") == [-221, -221]
    assert report(sim, "ROUT:RES?")["settle_us"] == "0"


# -- MEASure's acquisition -----------------------------------------------------------------


def test_an_acquisition_is_the_capture_fetch_describes() -> None:
    sim = SimulatedMeter()
    assert codes(sim, "FETC:STAT?") == [-230]
    ask(sim, "SAMP:COUN 250;:MEAS:CURR?")
    stats, header = report(sim, "FETC:STAT?"), report(sim, "FETC:HEAD?")
    assert (stats["samples"], stats["mean_na"]) == ("250", "88600000.000")
    # TRACE: the acquisition filled the ring, which a dump then reads.
    assert (header["sink"], header["ring"]) == ("ring", "250")
    assert ask(sim, "TRAC:POIN?") == ["250"]
    ask(sim, "SENS:PROF STAT;:MEAS:CURR?")
    assert report(sim, "FETC:HEAD?")["sink"] == "stream"
    assert ask(sim, "TRAC:POIN?") == ["0"]


@pytest.mark.parametrize("function", ["VOLT", "POW"])
def test_measuring_what_the_profile_does_not_convert_selects_state(function: str) -> None:
    sim = SimulatedMeter()
    ask(sim, f"MEAS:{function}?")
    assert ask(sim, "SENS:PROF?") == ["STATE"]
    ask(sim, f"SENS:PROF SLE;:MEAS:{function}?")
    assert ask(sim, "SENS:PROF?") == ["SLEEP"]  # it converts the bus already


def test_a_clocked_acquisition_takes_its_samples_and_streams_none() -> None:
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep, ring_capacity=64)
    ask(sim, "SENS:PROF STAT")
    sim.write(b"MEAS:CURR?\n")
    clock.now += 0.2
    assert sim.read(1 << 20).decode().strip() == "0.088600000"
    stats = report(sim, "FETC:STAT?")
    assert (stats["samples"], stats["dropped"], stats["dur_us"]) == ("100", "0", "115200")
    assert float(stats["mean_na"]) == pytest.approx(88.6e6, rel=1e-3)
    # A ring profile takes at most what the ring holds.
    ask(sim, "SENS:PROF TRAC")
    sim.write(b"MEAS:CURR?\n")
    clock.now += 0.2
    sim.read(1 << 20)
    assert (report(sim, "FETC:STAT?")["samples"], ask(sim, "TRAC:POIN?")) == ("64", ["64"])


def test_an_aborted_acquisition_closes_at_the_samples_it_took() -> None:
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep)
    ask(sim, "SENS:PROF STAT;:SAMP:COUN 1000")
    sim.write(b"MEAS:CURR?\n")
    clock.now += 0.1
    sim.write(b"ABOR\n")
    sim.read(1 << 20)
    assert codes(sim, "*OPC?") == [-230]
    assert report(sim, "FETC:STAT?")["samples"] == str(int(0.1 * 1e6 // 1152))


@pytest.mark.parametrize(
    ("parameter", "index"),
    [("0.4096", 1), ("0.41", 0), ("1.6384", 0), ("MIN", 1), ("MAX", 0), ("DEF", 0), ("4e-3", 1)],
)
def test_a_measurement_takes_the_smallest_range_that_holds_its_parameter(
    parameter: str, index: int
) -> None:
    sim = SimulatedMeter()
    assert codes(sim, f"MEAS:CURR? {parameter}") == []
    assert ask(sim, "SENS:CURR:RANG:IND?") == [str(index)]


@pytest.mark.parametrize(
    ("parameter", "code"), [("1.7", -222), ("-0.1", -222), ("BIG", -224), ("'x'", -104)]
)
def test_a_range_no_measurement_has_is_refused(parameter: str, code: int) -> None:
    sim = SimulatedMeter()
    assert codes(sim, f"MEAS:CURR? {parameter}") == refusals(code)
    assert codes(sim, "FETC:STAT?") == [-230]  # nothing was acquired


# -- parameters ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("parameter", "code"),
    [
        ("+200", 0),
        ("-1", -222),
        ("4294967296", -222),
        ("1.5", -104),
        ("1e3", -104),
        (".5", -104),
        ("x", -120),
        ("1x", -120),
        ("+", -120),
        ("'1'", -120),
        ("0", -222),
    ],
)
def test_an_unsigned_integer_is_read_as_the_parser_reads_it(parameter: str, code: int) -> None:
    sim = SimulatedMeter()
    assert codes(sim, f"SAMP:COUN {parameter}") == refusals(code)
    assert ask(sim, "SAMP:COUN?") == ["200" if code == 0 else "100"]


@pytest.mark.parametrize(("parameter", "code"), [("on", 0), ("1", 0), ("2", -224), ("YES", -224)])
def test_a_boolean_takes_four_words(parameter: str, code: int) -> None:
    sim = SimulatedMeter()
    assert codes(sim, f"SENS:CAPT:LIM {parameter}") == refusals(code)
    assert ask(sim, "SENS:CAPT:LIM?") == [str(int(code == 0))]


@pytest.mark.parametrize("header", ["SENS:CAPT:LIM", "TRIG:LEV", "CAL:TAG", "TRAC:MARK"])
def test_a_missing_parameter_is_minus_109_and_changes_nothing(header: str) -> None:
    assert codes(SimulatedMeter(), header) == [-109]


@pytest.mark.parametrize(
    "message",
    ["SENS:PROF 1", "FORM 'TEXT'", "TRIG:SOUR #H1", "ROUT:LEAD +1", "TRIG:LEV ON", "TRIG:LEV '1'"],
)
def test_a_number_or_a_string_where_a_word_is_meant_and_the_reverse_are_minus_104(
    message: str,
) -> None:
    assert codes(SimulatedMeter(), message) == [-104]


@pytest.mark.parametrize(
    ("parameter", "code", "kept"),
    [
        ('"km-1/2026"', 0, "km-1/2026"),
        ("plain", 0, "plain"),
        (f"'{'x' * 23}'", 0, "x" * 23),
        (f"'{'x' * 24}'", -224, ""),
        ('""', -224, ""),
        ('"a b"', -224, ""),
        ('"a=b"', -224, ""),
    ],
)
def test_a_tag_is_one_printable_token(parameter: str, code: int, kept: str) -> None:
    sim = SimulatedMeter()
    assert codes(sim, f"CAL:TAG {parameter}") == refusals(code)
    assert ask(sim, "CAL:TAG?") == [f'"{kept}"']


@pytest.mark.parametrize(
    ("parameter", "code"),
    [
        ("2026,11,24", 0),
        ("2026,11", -109),
        ("2026,x,24", -120),
        ("2026,2,30", -222),
        ("2025,11,24", -222),
        ("4294967295,1,1", -222),
    ],
)
def test_a_date_is_three_integers_of_the_calendar(parameter: str, code: int) -> None:
    sim = SimulatedMeter()
    assert codes(sim, f"CAL:DATE {parameter}") == refusals(code)
    assert ask(sim, "CAL:DATE?") == ["2026,11,24" if code == 0 else "0,0,0"]


@pytest.mark.parametrize(("seconds", "code"), [("0", -222), ("601", -222), ("600", 0)])
def test_a_null_lasts_one_to_six_hundred_seconds(seconds: str, code: int) -> None:
    assert codes(SimulatedMeter(), f"CAL:ZERO {seconds}") == refusals(code)


@pytest.mark.parametrize(
    ("parameters", "code"),
    [
        ("", 0),
        ("RAW", 0),
        ("SCAL,0,10", 0),
        ("0,10", 0),
        ("HEX", -224),
        ("0,10,20", -108),
        ("RAW,x", -120),
        ("RAW,64", -222),
        ("RAW,0,0", -224),
        ("RAW,0,x", -120),
    ],
)
def test_a_dumps_window_is_checked_on_the_deterministic_ring_too(
    parameters: str, code: int
) -> None:
    sim = SimulatedMeter()
    ask(sim, "INIT;:ABOR")
    assert codes(sim, f"TRAC:DATA? {parameters}".strip()) == refusals(code)


def test_the_deterministic_ring_dumps_the_window_asked_for() -> None:
    sim = SimulatedMeter()
    ask(sim, "INIT;:ABOR")
    sim.write(b"TRAC:DATA? RAW,60,10\n")
    rows = sim.read(1 << 20).decode().splitlines()
    assert " count=4 " in rows[0]  # the ring holds 64
    assert [row.split(" c=")[0] for row in rows if row.startswith("R ")] == ["R n=60"]
    assert len(next(row for row in rows if row.startswith("R ")).split(",")) == 4
    sim.write(b"TRAC:DATA? 62\n")
    rows = sim.read(1 << 20).decode().splitlines()
    assert len([row for row in rows if row.startswith("METER t=")]) == 2


@pytest.mark.parametrize(("parameter", "code"), [("0", -222), ("60001", -222), ("x,FORC", -120)])
def test_a_lead_cycle_lasts_a_number_of_milliseconds(parameter: str, code: int) -> None:
    sim = SimulatedMeter()
    assert codes(sim, f"ROUT:LEAD:CYCL {parameter}") == refusals(code)


def test_a_range_index_is_zero_or_one_and_a_channel_list_has_a_channel() -> None:
    sim = SimulatedMeter()
    assert codes(sim, "SENS:CURR:RANG:IND 2") == [-222]
    assert codes(sim, "SENS:FUNC:ON") == [-109]
    assert codes(sim, "SENS:FUNC:ON 'OHMS'") == [-224]
