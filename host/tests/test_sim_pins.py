# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The simulator's pin map and averaging override (0.18.0) on a board it is told the wiring of."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pytest
from conftest import FakeClock
from kleidos_meter.pins import DEFAULT_MAP, DIGEST, RIG_WIRING, PinMap, Wiring
from kleidos_meter.protocol import parse_errors, parse_fields, split_units
from kleidos_meter.sim import SimulatedMeter


def ask(sim: SimulatedMeter, message: str) -> list[str]:
    """*message*'s units, and ``SYSTem:ERRor:ALL?``'s last."""
    sim.write((message + ";:SYST:ERR:ALL?\n").encode())
    return split_units(sim.read(1 << 20).decode().strip())


def fields(sim: SimulatedMeter, query: str) -> dict[str, str]:
    """One report's fields; the queue must be empty after it."""
    units = ask(sim, query)
    assert units[-1] == '0,"No error"', units
    return parse_fields(units[0])


def codes(units: list[str]) -> list[int]:
    """The error codes in an answer's last unit."""
    return [entry.code for entry in parse_errors(units[-1])]


def live(sim: SimulatedMeter) -> dict[str, str]:
    return fields(sim, "SYST:PINS?")


DEFAULT_LIVE = DEFAULT_MAP.words() | {"src": "default", "verified": "1", "table": DIGEST}


def stage(sim: SimulatedMeter, *units: str) -> None:
    """Stage *units* (``SDA,18``, ``ADDR #H41``) and check none was refused."""
    message = ";:".join(u if u.startswith("SYST:") else f"SYST:PINS:STAG {u}" for u in units)
    assert ask(sim, message) == ['0,"No error"']


# -- apply: prove, revert, save ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("units", "where"),
    [
        (("SDA,18",), "sda=18 scl=5 addr=auto"),
        (("SCL,18",), "sda=4 scl=18 addr=auto"),
        (("SDA,21", "SCL,4", "SDA,5"), "sda=5 scl=4 addr=auto"),
        (("SYST:PINS:ADDR #H41",), "sda=4 scl=5 addr=0x41"),
    ],
)
def test_a_bus_the_ina228_is_not_on_reverts_with_one_error_and_saves_nothing(
    units: tuple[str, ...], where: str
) -> None:
    sim = SimulatedMeter()
    stage(sim, *units)
    answer = ask(sim, "SYST:PINS:APPL")
    assert answer == [
        f'117,"Pin map reverted;the INA228 did not answer on {where}: the last good map is back"'
    ]
    assert fields(sim, "SYST:PINS:RES?") == {
        "result": "reverted",
        "reason": "no_chip",
        "mfg": "-",
        "die": "-",
        "tst": "1",
        "saved": "0",
    }
    assert live(sim) == DEFAULT_LIVE
    assert ask(sim, "*TST?") == ["0", '0,"No error"']
    assert fields(sim, "SYST:INF?")["chip"] == "ina228"
    assert sim.pins_record is None
    # The staging is left as it was refused, for SYSTem:PINS:PENDing? to show.
    assert fields(sim, "SYST:PINS:PEND?")["valid"] == "1"
    assert fields(sim, "SYST:PINS:PEND?") != DEFAULT_MAP.words() | {
        "valid": "1",
        "problem": "-",
        "at": "-",
    }


def test_an_alert_pad_with_no_wire_reverts_on_the_self_tests_alert_bit() -> None:
    sim = SimulatedMeter()
    stage(sim, "ALER,16")
    assert ask(sim, "SYST:PINS:APPL") == [
        '117,"Pin map reverted;no ALERT edge on GPIO16: the last good map is back"'
    ]
    result = fields(sim, "SYST:PINS:RES?")
    assert result == {
        "result": "reverted",
        "reason": "alert",
        "mfg": "0x5449",
        "die": "0x228",
        "tst": "32",
        "saved": "0",
    }


def test_a_good_map_is_proved_saved_and_kept_across_a_reboot() -> None:
    sim = SimulatedMeter(wiring=Wiring.parse("sda=18,scl=21,address=0x41"))
    # On the default map the INA228 is not where the wiring has it.
    assert fields(sim, "SYST:INF?")["chip"] == "none"
    stage(sim, "SDA,18", "SCL,21", "SYST:PINS:ADDR #H41", "SYST:PINS:CLOC 100000")
    assert ask(sim, "SYST:PINS:APPL;:SYST:PINS:RES?") == [
        "result=ok reason=- mfg=0x5449 die=0x228 tst=0 saved=1",
        '0,"No error"',
    ]
    moved = DEFAULT_MAP.words() | {"sda": "18", "scl": "21", "addr": "0x41", "scl_hz": "100000"}
    assert live(sim) == moved | {"src": "stored", "verified": "1", "table": DIGEST}
    info = fields(sim, "SYST:INF?")
    assert (info["chip"], info["addr"], info["pins_src"]) == ("ina228", "0x41", "stored")
    assert fields(sim, "SYST:HEAL?")["pins_src"] == "stored"
    assert ask(sim, "SYST:COMM:I2C:FREQ?")[0] == "100000"
    sim.reboot()
    assert live(sim) == moved | {"src": "stored", "verified": "1", "table": DIGEST}
    staged = replace(DEFAULT_MAP, sda=18, scl=21, addr=0x41, scl_hz=100_000)
    assert ask(sim, "SYST:PINS:RES?;:SYST:PINS:PEND?;:SYST:COMM:I2C:FREQ?") == [
        "result=none reason=- mfg=- die=- tst=- saved=0",
        f"{staged.text()} valid=1 problem=- at=-",
        "100000",
        '0,"No error"',
    ]
    # *RST leaves the map, and the bus clock is the map's.
    assert ask(sim, "SYST:COMM:I2C:FREQ 50000;:*RST;:SYST:COMM:I2C:FREQ?")[0] == "100000"
    assert live(sim)["src"] == "stored"


def test_a_record_saved_under_another_table_reads_as_none() -> None:
    sim = SimulatedMeter()
    sim.pins_record = ("0x00000000", replace(DEFAULT_MAP, relay1=16))
    sim.reboot()
    assert live(sim) == DEFAULT_LIVE


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        ("INIT", '-221,"Settings conflict;capture running (ABORt first)"'),
        ("CAL:ZERO 5", '112,"Null window running;null window running (ABORt first)"'),
    ],
)
def test_apply_and_defaults_are_refused_under_a_window(setup: str, error: str) -> None:
    sim = SimulatedMeter()
    assert ask(sim, setup) == ['0,"No error"']
    for order in ("SYST:PINS:APPL", "SYST:PINS:DEF"):
        assert ask(sim, order) == [error]
    assert fields(sim, "SYST:PINS:RES?")["result"] == "none"


@pytest.mark.parametrize(
    ("order", "detail"),
    [
        ("ROUT:NULL ON", "a null path is held (path=bypass): ROUTe:NULLpath OFF first"),
        ("ROUT:BYP ON", "the relays are not at rest (path=both): put them back first"),
        (
            "ROUT:LEAD OPEN,FORC",
            "the relays are not at rest (path=none): put them back first",
        ),
    ],
)
def test_apply_is_refused_while_a_relay_is_not_at_rest(order: str, detail: str) -> None:
    sim = SimulatedMeter()
    assert ask(sim, order) == ['0,"No error"']
    stage(sim, "REL1,16")
    for apply in ("SYST:PINS:APPL", "SYST:PINS:DEF"):
        assert ask(sim, apply) == [f'-221,"Settings conflict;{detail}"']
        assert fields(sim, "SYST:PINS:RES?") == {
            "result": "refused",
            "reason": "relays",
            "mfg": "-",
            "die": "-",
            "tst": "-",
            "saved": "0",
        }
    assert live(sim) == DEFAULT_LIVE


def test_a_map_the_rules_refuse_is_refused_with_its_problem() -> None:
    sim = SimulatedMeter()
    stage(sim, "SDA,45")
    assert ask(sim, "SYST:PINS:APPL") == [
        '-221,"Settings conflict;the staged map breaks a rule: reserved at sda'
        ' (SYSTem:PINS:PENDing?)"'
    ]
    assert fields(sim, "SYST:PINS:RES?")["reason"] == "invalid"


# -- the defaults, and a board with no INA228 -------------------------------------------------


def test_defaults_put_the_rig_back_and_need_no_ina228() -> None:
    sim = SimulatedMeter()
    stage(sim, "REL1,16")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    sim.device_ready = False  # the INA228 is gone
    assert ask(sim, "SYST:PINS:DEF;:SYST:PINS:RES?") == [
        "result=ok reason=no_chip mfg=- die=- tst=1 saved=1",
        '0,"No error"',
    ]
    # Relay 1 moved back: its wiring is to be checked again, until a boot.
    assert live(sim) == DEFAULT_LIVE | {"verified": "0"}
    assert fields(sim, "SYST:INF?")["chip"] == "none"
    sim.reboot()
    assert live(sim) == DEFAULT_LIVE


def test_the_default_map_is_the_default_map_however_it_is_applied() -> None:
    sim = SimulatedMeter()
    stage(sim, "SYST:PINS:CLOC 100000")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    assert live(sim)["src"] == "stored"
    stage(sim, "SYST:PINS:CLOC 400000")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    assert live(sim) == DEFAULT_LIVE
    sim.reboot()
    assert live(sim) == DEFAULT_LIVE


def test_a_revert_whose_own_proof_fails_leaves_no_ina228_and_the_console_answering() -> None:
    sim = SimulatedMeter()
    sim.device_ready = False
    stage(sim, "SDA,18")
    assert ask(sim, "SYST:PINS:APPL") == [
        '117,"Pin map reverted;the INA228 did not answer on sda=18 scl=5 addr=auto: the last good'
        ' map is back; it fails too (tst=1)"'
    ]
    assert fields(sim, "SYST:INF?")["chip"] == "none"
    assert ask(sim, "*TST?")[0] == "1"
    assert codes(ask(sim, "SENS:CURR:RANG:IND 1")) == [-241]
    assert live(sim) == DEFAULT_LIVE


def test_a_board_wired_elsewhere_finds_no_ina228_on_the_default_map() -> None:
    sim = SimulatedMeter(wiring=Wiring.parse("sda=18"))
    assert ask(sim, "*TST?;:DIAG:I2C:SCAN?") == [
        "1",
        "expected=0x40 found=- n=0",
        '0,"No error"',
    ]
    stage(sim, "SDA,18")
    assert ask(sim, "SYST:PINS:APPL;:DIAG:I2C:SCAN?") == [
        "expected=0x40 found=0x40 n=1",
        '0,"No error"',
    ]


# -- the boot ---------------------------------------------------------------------------------


def moved_board() -> SimulatedMeter:
    """A simulator whose INA228 sits on GPIO18 and GPIO21, with that map applied and saved."""
    sim = SimulatedMeter(wiring=Wiring.parse("sda=18,scl=21"))
    stage(sim, "SDA,18", "SCL,21", "SYST:PINS:CLOC 100000")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    return sim


def test_a_stored_bus_that_no_longer_answers_falls_back_at_boot_with_one_error() -> None:
    sim = moved_board()
    sim.wiring = RIG_WIRING  # the wires went back to GPIO4 and GPIO5
    sim.reboot()
    units = ask(sim, "SYST:PINS?")
    assert units[0] == f"{DEFAULT_MAP.text()} src=fallback verified=1 table={DIGEST}"
    assert codes(units) == [117]
    assert units[-1] == (
        '117,"Pin map reverted;the stored bus (sda=18 scl=21 addr=auto) did not answer: the'
        ' default bus is back (the record is kept)"'
    )
    assert fields(sim, "SYST:INF?")["chip"] == "ina228"
    # The record is kept for the person to see, and defaults replace it.
    assert sim.pins_record is not None and sim.pins_record[1].sda == 18
    assert ask(sim, "SYST:PINS:DEF") == ['0,"No error"']
    assert live(sim)["src"] == "default"


def test_a_stored_bus_whose_default_pads_a_relay_holds_boots_with_no_ina228() -> None:
    sim = SimulatedMeter(wiring=Wiring.parse("sda=18,scl=21,relay1=4"))
    stage(sim, "SDA,18", "SCL,21", "REL1,4")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    sim.wiring = RIG_WIRING
    sim.reboot()
    units = ask(sim, "SYST:PINS?;:SYST:INF?")
    assert parse_fields(units[0])["src"] == "stored"
    assert parse_fields(units[1])["chip"] == "none"
    assert codes(units) == [117]
    assert "did not answer and a role holds the default bus pins: no INA228" in units[-1]
    # The console is the way back.
    assert ask(sim, "SYST:PINS:DEF;:SYST:PINS:RES?")[0].startswith("result=ok reason=- ")
    assert fields(sim, "SYST:INF?")["chip"] == "ina228"


def test_a_stored_map_on_the_default_bus_has_nothing_to_fall_back_to() -> None:
    sim = SimulatedMeter()
    stage(sim, "REL1,16")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    sim.device_ready = False
    sim.reboot()
    units = ask(sim, "SYST:PINS?")
    assert codes(units) == []
    assert parse_fields(units[0])["src"] == "stored" and parse_fields(units[0])["relay1"] == "16"


# -- the relays' check ------------------------------------------------------------------------


def test_relays_moved_are_unverified_until_a_null_path_check_passes_on_them() -> None:
    sim = SimulatedMeter(wiring=Wiring.parse("relay1=16,relay2=17"))
    stage(sim, "REL1,16", "REL2,17,HIGH")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    assert live(sim)["verified"] == "0"
    state = fields(sim, "ROUT:STAT?")
    assert (state["relay1_gpio"], state["relay2_gpio"]) == ("16", "17")
    assert ask(sim, "ROUT:NULL ON;:ROUT:NULL OFF") == ['0,"No error"']
    assert live(sim)["verified"] == "1"
    # Moving no relay keeps the verdict; moving them back to rev D's does not give it.
    stage(sim, "SYST:PINS:CLOC 100000")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    assert live(sim)["verified"] == "1"
    assert ask(sim, "SYST:PINS:DEF") == ['0,"No error"']
    assert (live(sim)["src"], live(sim)["verified"]) == ("default", "0")


@pytest.mark.parametrize(
    ("wiring", "code", "text"),
    [
        ("relay2=17", 103, "bypass check failed (k2 took no current"),
        ("relay2_active=low", 103, "bypass check failed (k2 took no current"),
        ("relay1=16", 104, "lead check failed (the shunt still conducts"),
    ],
)
def test_a_relay_the_wiring_does_not_have_fails_its_check_and_releases_the_path(
    wiring: str, code: int, text: str
) -> None:
    sim = SimulatedMeter(wiring=Wiring.parse(wiring))
    units = ask(sim, "ROUT:NULL ON;:ROUT:STAT?")
    assert parse_fields(units[0])["path"] == "shunt"
    assert codes(units) == [code]
    assert text in units[-1] and units[-1].endswith('; released to path=shunt)"')
    assert live(sim)["verified"] == "0"
    assert sim.hold_due is None


def test_relays_swapped_with_their_levels_lose_the_whole_share_at_the_first_step() -> None:
    sim = SimulatedMeter(current_a=0.005)
    stage(sim, "SYST:PINS:STAG REL1,15,HIGH;:SYST:PINS:STAG REL2,7,LOW")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    units = ask(sim, "ROUT:NULL ON;:ROUT:STAT?")
    assert codes(units) == [103]
    assert "the shunt lost all of the dut's current: vs_pv=500000000,0;" in units[-1]
    assert parse_fields(units[0])["path"] == "shunt"
    assert live(sim)["verified"] == "0"


@pytest.mark.parametrize(
    ("staged", "after_a", "vs_pv"),
    [
        ("SYST:PINS:STAG REL1,7,HIGH", 0.0, "500000000,0"),
        ("SYST:PINS:STAG REL2,15,LOW", 0.002, "500000000,200000000"),
    ],
)
def test_a_flipped_active_level_is_seen_at_rest_and_put_back(
    staged: str, after_a: float, vs_pv: str
) -> None:
    sim = SimulatedMeter(current_a=0.005)
    stage(sim, staged)
    units = ask(sim, "SYST:PINS:APPL")
    assert codes(units) == [117]
    assert (
        f"the relays at rest took the dut's current off the shunt (vs_pv={vs_pv}):"
        " the last good map is back" in units[-1]
    )
    assert fields(sim, "SYST:PINS:RES?")["reason"] == "rest"
    assert live(sim) == DEFAULT_LIVE
    assert sim.pins_record is None
    assert float(ask(sim, "MEAS:CURR?")[0]) == pytest.approx(0.005)
    # Kept by a map that is wrong in the same way, the shunt reads what is left.
    sim.pins_live = _flipped(staged)
    assert float(ask(sim, "MEAS:CURR?")[0]) == pytest.approx(after_a)


def _flipped(staged: str) -> PinMap:
    """The default map with the level *staged* gives its relay."""
    level = staged.rsplit(",", 1)[1].lower()
    if "REL1" in staged:
        return replace(DEFAULT_MAP, relay1_active=level)
    return replace(DEFAULT_MAP, relay2_active=level)


def test_under_the_floor_a_flipped_level_is_kept_and_defaults_bring_the_dut_back() -> None:
    sim = SimulatedMeter(current_a=0.000042, clocked=True, clock=FakeClock())
    stage(sim, "SYST:PINS:STAG REL1,7,HIGH")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    assert fields(sim, "SYST:PINS:RES?")["result"] == "ok"
    assert live(sim)["verified"] == "0"
    assert sim.current_at(0) == 0.0
    assert ask(sim, "SYST:PINS:DEF") == ['0,"No error"']
    assert sim.current_at(0) == pytest.approx(0.000042)


def test_defaults_are_kept_whatever_the_rest_check_finds() -> None:
    # A board whose Relay 1 is active high: the default map energizes it at rest.
    sim = SimulatedMeter(current_a=0.005, wiring=Wiring.parse("relay1_active=high"))
    stage(sim, "SYST:PINS:STAG REL1,7,HIGH")
    assert ask(sim, "SYST:PINS:APPL") == ['0,"No error"']
    assert ask(sim, "SYST:PINS:DEF") == ['0,"No error"']
    result = fields(sim, "SYST:PINS:RES?")
    assert (result["result"], result["reason"], result["saved"]) == ("ok", "rest", "1")


def test_a_capture_header_names_the_maps_source() -> None:
    sim = SimulatedMeter()
    stage(sim, "SYST:PINS:CLOC 100000")
    assert ask(sim, "SYST:PINS:APPL;:SENS:PROF STAT;:INIT") == ['0,"No error"']
    header = parse_fields(ask(sim, "ABOR;:FETC:HEAD?")[0])
    assert (header["pins_src"], header["scl_hz"]) == ("stored", "100000")


# -- the averaging override -------------------------------------------------------------------


def report(sim: SimulatedMeter) -> dict[str, str]:
    return fields(sim, "SENS:PROF:REP?")


@pytest.mark.parametrize(
    ("profile", "units", "word", "period", "shunt", "count"),
    [
        ("TRAC", ("SENS:AVER:COUN 4",), "0xA0C1", "1120", "280", "4"),
        ("TRAC", ("SENS:CURR:APER 0.000084", "SENS:AVER:COUN 16"), "0xA042", "1344", "84", "16"),
        ("STAT", ("SENS:AVER:COUN 64",), "0xF143", "73728", "1052", "64"),
        ("SLE", ("SENS:CURR:APER 0.00054", "SENS:AVER:COUN 4"), "0xF101", "2560", "540", "4"),
    ],
)
def test_the_override_sets_the_word_and_the_period_follows(
    profile: str, units: tuple[str, ...], word: str, period: str, shunt: str, count: str
) -> None:
    sim = SimulatedMeter()
    assert ask(sim, ";:".join([f"SENS:PROF {profile}", *units])) == ['0,"No error"']
    own = report(sim)
    assert own["avg_state"] == "off"  # set while off, it converts with its own word
    assert ask(sim, "SENS:AVER ON;:SENS:AVER?") == ["1", '0,"No error"']
    found = report(sim)
    assert (found["adc_config"], found["period_us"]) == (word, period)
    assert (found["vshct_us"], found["avg"], found["avg_state"]) == (shunt, count, "on")
    answers = ask(sim, "SENS:AVER:COUN?;:SENS:CURR:APER?;:SAMP:TIM?")
    assert answers[0] == count
    assert float(answers[1]) == int(shunt) / 1e6 and float(answers[2]) == int(period) / 1e6
    assert ask(sim, "INIT") == ['0,"No error"']
    header = parse_fields(ask(sim, "ABOR;:FETC:HEAD?")[0])
    assert (header["adc_config"], header["period_us"], header["avg"]) == (word, period, count)
    assert header["avg_state"] == "on"
    assert ask(sim, "SENS:AVER OFF") == ['0,"No error"']
    assert report(sim) == own


def test_the_override_is_per_profile_and_off_after_rst_and_a_reboot() -> None:
    sim = SimulatedMeter()
    assert ask(sim, "SENS:AVER:COUN 4;:SENS:AVER ON;:SENS:PROF STAT") == ['0,"No error"']
    assert report(sim)["avg_state"] == "off"
    assert ask(sim, "SENS:PROF TRAC;:SENS:AVER?")[0] == "1"
    assert ask(sim, "*RST;:SENS:AVER?;:SENS:AVER:COUN?")[:2] == ["0", "1"]
    assert ask(sim, "SENS:AVER:COUN 4;:SENS:AVER ON") == ['0,"No error"']
    sim.reboot()
    assert ask(sim, "SENS:AVER?")[0] == "0"


COUNT_USAGE = "use SENSe:AVERage:COUNt 1|4|16|64|128|256|512|1024"
TIME_USAGE = "use SENSe:CURRent:APERture <seconds> (0.00005..0.00412)"
TOO_FAST = "averaging 1 x 50 us gives 50 us a sample, under the 191 us its reads need at 400000 Hz"
TOO_SLOW = "averaging 256 x 1052 us gives 294912 us a sample, over the 200000 us its pacing allows"


@pytest.mark.parametrize(
    ("setup", "order", "error"),
    [
        ("SENS:PROF TRAC", "SENS:AVER:COUN 3", f'-224,"Illegal parameter value;{COUNT_USAGE}"'),
        ("SENS:PROF TRAC", "SENS:CURR:APER 0.0003", f'-224,"Illegal parameter value;{TIME_USAGE}"'),
        (
            "SENS:PROF TRAC",
            "SENS:CURR:APER 0.0000505",
            f'-224,"Illegal parameter value;{TIME_USAGE}"',
        ),
        (
            "SENS:PROF BURS",
            "SENS:AVER ON",
            '-221,"Settings conflict;BURSt has no averaging override: its 50 us conversion is'
            ' its definition"',
        ),
        ("SENS:CURR:APER 0.00005", "SENS:AVER ON", f'-221,"Settings conflict;{TOO_FAST}"'),
        ("SENS:AVER ON", "SENS:CURR:APER 0.00005", f'-221,"Settings conflict;{TOO_FAST}"'),
        (
            "SENS:PROF STAT;:SENS:AVER:COUN 256",
            "SENS:AVER ON",
            f'-221,"Settings conflict;{TOO_SLOW}"',
        ),
    ],
)
def test_an_override_the_instrument_cannot_run_is_refused_and_changes_nothing(
    setup: str, order: str, error: str
) -> None:
    sim = SimulatedMeter()
    assert ask(sim, setup) == ['0,"No error"']
    before = ask(sim, "SENS:AVER?;:SENS:PROF:REP?")
    assert ask(sim, order) == [error]
    assert ask(sim, "SENS:AVER?;:SENS:PROF:REP?") == before


SLOWED = (
    '-221,"Settings conflict;averaging 1 x 150 us gives 150 us a sample, under the 191 us its'
    ' reads need at 400000 Hz"'
)


@pytest.mark.parametrize("order", ["INIT", "MEAS:CURR?", "CAL:ZERO 1"])
def test_every_window_judges_the_override_again_at_the_running_clock(order: str) -> None:
    sim = SimulatedMeter()
    setup = "SYST:COMM:I2C:FREQ 1000000;:SENS:CURR:APER 0.00015;:SENS:AVER ON"
    assert ask(sim, setup) == ['0,"No error"']
    assert ask(sim, "SYST:COMM:I2C:FREQ 400000") == ['0,"No error"']
    assert ask(sim, order) == [SLOWED]
    assert not sim.window_open


@pytest.mark.parametrize("order", ["INIT", "SENS:CAPT STAT;:INIT", "MEAS:CURR?", "CAL:ZERO 1"])
def test_every_window_needs_a_ring_profiles_read_under_half_its_period(order: str) -> None:
    sim = SimulatedMeter()
    said = ask(sim, f"SENS:PROF BURS;:{order}")
    assert len(said) == 1 and "BURST needs a faster bus" in said[0]
    assert not sim.window_open


def test_a_rings_acquisition_is_bounded_to_600_s_of_the_samples_the_ring_holds() -> None:
    sim = SimulatedMeter(ring_capacity=2048)
    # 143,360 us a sample: the ring's 2,048 of them take 294 s, 8,192 take 1,174 s.
    setup = "SENS:PROF TRAC;:SENS:AVER:COUN 512;:SENS:AVER ON;:SAMP:COUN 1000000"
    assert ask(sim, setup) == ['0,"No error"']
    assert codes(ask(sim, "MEAS:CURR?")) == []
    sim = SimulatedMeter(ring_capacity=8192)
    assert ask(sim, setup) == ['0,"No error"']
    assert ask(sim, "MEAS:CURR?") == [
        '-222,"Data out of range;acquisition of 8192 x 143360 us exceeds 600 s (lower SAMPle:COUNt,'
        ' or INITiate a capture)"'
    ]


def clocked_noise(**knobs: Any) -> tuple[SimulatedMeter, FakeClock]:
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep, noise_a=1e-3, **knobs)
    sim.read_timeout = 0.0
    return sim, clock


def spread(sim: SimulatedMeter, clock: FakeClock, setup: str, seconds: float) -> dict[str, str]:
    """A ring capture of *seconds* after *setup*: its statistics."""
    sim.write((setup + ";:INIT\n").encode())
    sim.read(1 << 20)
    clock.now += seconds
    sim.write(b"ABOR;:FETC:STAT?\n")
    out = b""
    while chunk := sim.read(1 << 20):
        out += chunk
    return parse_fields(split_units(out.decode().strip())[0])


def test_the_noise_falls_with_the_square_root_of_the_samples_averaged() -> None:
    sim, clock = clocked_noise()
    own = spread(sim, clock, "SENS:PROF TRAC", 1.0)
    averaged = spread(sim, clock, "SENS:AVER:COUN 16;:SENS:AVER ON", 4.0)
    assert int(own["samples"]) > 3000 and int(averaged["samples"]) > 800
    sd_own, sd_averaged = float(own["sd_na"]), float(averaged["sd_na"])
    assert sd_own == pytest.approx(1e6, rel=0.08)  # 1 mA, in nanoamps
    assert sd_own / sd_averaged == pytest.approx(math.sqrt(16), rel=0.12)
    # The mean stays the DUT's current, whatever the count.
    for stats in (own, averaged):
        assert float(stats["mean_na"]) == pytest.approx(88.6e6, rel=2e-3)


def test_a_dumped_ring_reads_the_same_noise_the_statistics_were_taken_from() -> None:
    sim, clock = clocked_noise()
    stats = spread(sim, clock, "SENS:PROF TRAC", 0.05)
    dumped = sim.last_stats
    assert dumped is not None
    first = [sim._counts(dumped, i) for i in range(int(stats["samples"]))]
    again = [sim._counts(dumped, i) for i in range(int(stats["samples"]))]
    assert first == again and len(set(first)) > 10


def test_no_noise_keeps_every_sample_the_duts_current() -> None:
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep)
    sim.read_timeout = 0.0
    stats = spread(sim, clock, "SENS:PROF TRAC", 0.05)
    assert float(stats["sd_na"]) == 0.0
