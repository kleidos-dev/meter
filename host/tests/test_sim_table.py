# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The simulator's command table against the firmware's ``kCommands``."""

from __future__ import annotations

import pytest
from conftest import FakeClock, firmware_commands
from kleidos_meter import sim as sim_module
from kleidos_meter.pins import DEFAULT_MAP, DIGEST
from kleidos_meter.protocol import ErrorEntry, parse_errors
from kleidos_meter.sim import IMPLEMENTED, UNDEFINED, SimulatedMeter
from kleidos_meter.sim_table import COMMANDS, compile_pattern, key_of, matches, resolve


def header_of(pattern: str, *, long: bool = False, suffix: str = "1") -> str:
    """A header *pattern* matches: short forms and no optional node, or every node long."""
    if pattern.startswith("*"):
        return pattern
    nodes = []
    for node in compile_pattern(pattern):
        if node.optional and not long:
            continue
        nodes.append((node.name if long else node.short) + (suffix if node.suffix else ""))
    return ":".join(nodes) + ("?" if pattern.endswith("?") else "")


def errors_after(sim: SimulatedMeter, message: str) -> list[int] | None:
    """Send *message* and then ``SYSTem:ERRor:ALL?``: the codes it queued.

    :returns: None when the instrument answered nothing (a starved watchdog).
    """
    sim.write((message + "\n").encode())
    sim.read(1 << 20)
    sim.write(b"SYST:ERR:ALL?\n")
    lines = sim.read(1 << 20).decode().strip().splitlines()
    return [entry.code for entry in parse_errors(lines[-1])] if lines else None


def test_the_table_is_the_firmwares_kcommands_row_for_row_flags_and_counts_included() -> None:
    assert len(COMMANDS) > 100
    assert list(COMMANDS) == firmware_commands()


def test_every_header_is_implemented_or_named_undefined_and_never_both() -> None:
    patterns = {pattern for pattern, *_ in COMMANDS}
    assert patterns >= UNDEFINED, "an undefined header the firmware no longer has"
    assert patterns == IMPLEMENTED | UNDEFINED
    assert not IMPLEMENTED & UNDEFINED
    keys = [key_of(pattern) for pattern in patterns]
    assert len(keys) == len(set(keys)), "two patterns share a dispatch key"
    handled = set(sim_module._HANDLERS)
    assert {key_of(pattern) for pattern in IMPLEMENTED} == handled
    assert not {key_of(pattern) for pattern in UNDEFINED} & handled


def test_the_panel_needs_these_headers_implemented() -> None:
    needed = {
        "SYSTem:STATus?",
        "STATus:OPERation[:EVENt]?",
        "STATus:QUEStionable:CONDition?",
        "TRIGger:SOURce",
        "TRIGger:SLOPe",
        "TRIGger:PRETrigger",
        "SENSe:CAPTure",
        "SENSe:CAPTure:LIMit",
        "SENSe:CURRent:THReshold",
        "SENSe:CURRent:THReshold:MARK",
        "SYSTem:COMMunicate:I2C:FREQuency",
        "SYSTem:COMMunicate:I2C:FREQuency?",
        "DIAGnostic:WATChdog?",
        "DIAGnostic:WATChdog:TEST",
        "DIAGnostic:I2C:SCAN?",
        "*TST?",
        "DIAGnostic:HEAP?",
    }
    assert needed <= IMPLEMENTED


@pytest.mark.parametrize("pattern", sorted(p for p, *_ in COMMANDS))
@pytest.mark.parametrize("long", [False, True])
def test_each_header_answers_or_is_undefined(pattern: str, long: bool) -> None:
    """With no parameter, in either spelling: -113 for an undefined one, never for the rest."""
    sim = SimulatedMeter()
    header = header_of(pattern, long=long, suffix="0")
    resolved = resolve(header)
    assert resolved is not None and resolved.pattern == pattern
    codes = errors_after(sim, header)
    if pattern in UNDEFINED:
        assert codes == [-113]
    elif pattern == "DIAGnostic:WATChdog:TEST":
        assert codes is None  # the console stopped: nothing answers until the reboot
    else:
        assert codes is not None and -113 not in codes


def test_an_unknown_header_and_an_empty_node_are_minus_113() -> None:
    sim = SimulatedMeter()
    assert errors_after(sim, "SYST:NOPE?") == [-113]
    assert errors_after(sim, "SYST::ERR?") == [-113]
    assert errors_after(sim, "SYST:ERR") == [-113]  # a command form the query lacks
    assert errors_after(sim, "ROUT:RELAYX ON") == [-113]


def test_the_matcher_reads_suffixes_and_both_forms_as_the_firmware_does() -> None:
    assert matches("SENSe:CURRent:SHUNt:RANGe#?", "sens:curr:shun:range1?") == (True, 1)
    assert matches("ROUTe:RELay#", "ROUTE:RELAY") == (True, None)
    assert matches("ROUTe:RELay#", "ROUT:REL12") == (True, 12)
    assert matches("ROUTe:LEAD", "ROUT:LEAD3") == (False, None)  # no suffix on LEAD
    assert matches("ROUTe:RELay#", "ROUT:3") == (False, None)
    assert matches("ROUTe:RELay#", "ROUT:RELX3") == (False, None)
    assert matches("SYSTem:ERRor[:NEXT]?", "SYSTEM:ERROR:NEXT?") == (True, None)
    assert matches("SYSTem:ERRor[:NEXT]?", "SYST:ERRO?") == (False, None)  # neither form
    assert matches("*IDN?", "*idn?") == (True, None)
    form = resolve("FORM:DATA")
    assert form is not None and form.key == "FORM"
    assert key_of("SENSe:CURRent:SHUNt:RANGe#?") == "SENS:CURR:SHUN:RANG#?"


@pytest.mark.parametrize(
    "pattern", sorted(p for p, flags, _ in COMMANDS if "R" in flags and p in IMPLEMENTED)
)
def test_flag_r_refuses_under_a_running_capture_and_a_running_null(pattern: str) -> None:
    sim = SimulatedMeter()
    sim.write(b"SENS:PROF STAT;:INIT\n")
    assert errors_after(sim, header_of(pattern, suffix="0")) == [-221]
    sim = SimulatedMeter()
    sim.write(b"CAL:ZERO 2\n")
    assert errors_after(sim, header_of(pattern, suffix="0")) == [112]


@pytest.mark.parametrize(
    "pattern", sorted(p for p, flags, _ in COMMANDS if "C" in flags and p in IMPLEMENTED)
)
def test_flag_c_refuses_while_a_window_that_ended_is_not_closed(pattern: str) -> None:
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, ring_capacity=1024, clock=clock, sleep=clock.sleep)
    sim.write(b"SENS:PROF TRAC;:INIT\n")
    clock.now += 1.0
    assert sim.read(1 << 16) == b""  # a ring streams nothing; it filled meanwhile
    assert errors_after(sim, header_of(pattern, suffix="0")) == [-221]
    sim.write(b"ABOR;:CAL:ZERO 1\n")
    clock.now += 2.0
    assert errors_after(sim, header_of(pattern, suffix="0")) == [112]


@pytest.mark.parametrize(
    ("pattern", "most"), sorted((p, most) for p, _, most in COMMANDS if p in IMPLEMENTED)
)
def test_a_parameter_past_the_commands_count_is_refused_and_says_how_many_it_takes(
    pattern: str, most: int
) -> None:
    sim = SimulatedMeter()
    header = header_of(pattern, suffix="0")
    sim.write(f"{header} {','.join(['1'] * (most + 1))}\nSYST:ERR:ALL?\n".encode())
    answered = sim.read(1 << 20).decode().strip()
    takes = {0: "no parameter", 1: "at most 1 parameter"}.get(most, f"at most {most} parameters")
    assert parse_errors(answered) == [
        ErrorEntry(-108, f"Parameter not allowed;{header} takes {takes}")
    ]


def test_a_unit_of_more_parameters_than_the_parser_keeps_is_refused_by_its_header() -> None:
    sim = SimulatedMeter()
    sim.write(b"SENS:FUNC:ON 1,2,3,4,5,6,7,8,9\nSYST:ERR:ALL?\n")
    assert sim.read(1 << 20).decode().strip() == '-108,"Parameter not allowed;SENS:FUNC:ON"'


def test_a_comma_inside_a_quoted_parameter_does_not_split_it() -> None:
    sim = SimulatedMeter()
    sim.write(b'CAL:TAG "a,b";:CAL:TAG?;:SYST:ERR:ALL?\n')
    refused = "a tag is printable characters with no space, comma, quote, ';' or '='"
    assert sim.read(1 << 20).decode().strip() == f'"";-224,"Illegal parameter value;{refused}"'


@pytest.mark.parametrize(
    "pattern", sorted(p for p, flags, _ in COMMANDS if "D" in flags and p in IMPLEMENTED)
)
def test_flag_d_refuses_without_the_ina228(pattern: str) -> None:
    sim = SimulatedMeter()
    sim.device_ready = False
    assert errors_after(sim, header_of(pattern, suffix="0")) == [-241]


# -- the pin map (0.18.0) -----------------------------------------------------------------


def ask(sim: SimulatedMeter, message: str) -> str:
    """*message* and ``SYSTem:ERRor:ALL?`` after it: the answers, as one line."""
    sim.write((message + ";:SYST:ERR:ALL?\n").encode())
    return sim.read(1 << 20).decode().strip()


STAGE = "use SYSTem:PINS:STAGe SDA|SCL|ALERt|RELay1|RELay2,<gpio>|NONE[,HIGH|LOW|UP|OFF]"
ADDRESS = "use SYSTem:PINS:ADDRess AUTO|<1..255> (the INA228 answers at #H40..#H4F)"


@pytest.mark.parametrize(
    ("stage", "keys", "verdict"),
    [
        ("SDA,45", {"sda": "45"}, "valid=0 problem=reserved at=sda"),
        ("SDA,5", {"sda": "5"}, "valid=0 problem=shared at=scl"),
        ("ALER,39", {"alert": "39"}, "valid=0 problem=too_high at=alert"),
        ("SCL,NONE", {"scl": "none"}, "valid=0 problem=missing at=scl"),
        ("SDA,8,OFF", {"sda": "8", "pull_sda": "off"}, "valid=1 problem=- at=-"),
        ("SCL,#H9,UP", {"scl": "9"}, "valid=1 problem=- at=-"),
        ("ALERT,10,OFF", {"alert": "10", "pull_alert": "off"}, "valid=1 problem=- at=-"),
        ("RELAY1,#Q13,HIGH", {"relay1": "11", "relay1_active": "high"}, "valid=1 problem=- at=-"),
        ("rel2,#B1100,low", {"relay2": "12", "relay2_active": "low"}, "valid=1 problem=- at=-"),
        ("REL1,+8", {"relay1": "8"}, "valid=1 problem=- at=-"),
    ],
)
def test_a_staged_role_is_judged_as_the_firmware_judges_it(
    stage: str, keys: dict[str, str], verdict: str
) -> None:
    sim = SimulatedMeter()
    expected = dict(pair.split("=") for pair in DEFAULT_MAP.text().split()) | keys
    staged = " ".join(f"{k}={v}" for k, v in expected.items())
    answer = ask(sim, f"SYST:PINS:STAG {stage};:SYST:PINS:PEND?")
    assert answer == f'{staged} {verdict};0,"No error"'
    # The live map never moves, and CLEar takes the staging back to it.
    live = f"{DEFAULT_MAP.text()} src=default verified=1 table={DIGEST}"
    assert ask(sim, "SYST:PINS?") == f'{live};0,"No error"'
    cleared = ask(sim, "SYST:PINS:CLE;:SYST:PINS:PEND?")
    assert cleared.startswith(DEFAULT_MAP.text() + " valid=1")


@pytest.mark.parametrize(
    ("unit", "error"),
    [
        ("SYST:PINS:STAG SDX,4", f'-224,"Illegal parameter value;{STAGE}"'),
        ("SYST:PINS:STAG 4,4", f'-104,"Data type error;{STAGE}"'),
        ("SYST:PINS:STAG SDA", f'-109,"Missing parameter;{STAGE}"'),
        ("SYST:PINS:STAG SDA,64", f'-222,"Data out of range;{STAGE}"'),
        ("SYST:PINS:STAG SDA,ZZZ", f'-224,"Illegal parameter value;{STAGE}"'),
        ("SYST:PINS:STAG SDA,-1", f'-222,"Data out of range;{STAGE}"'),
        ("SYST:PINS:STAG SDA,#X1", f'-120,"Numeric data error;{STAGE}"'),
        ("SYST:PINS:STAG SDA,#H", f'-120,"Numeric data error;{STAGE}"'),
        ("SYST:PINS:STAG SDA,1.5", f'-104,"Data type error;{STAGE}"'),
        ("SYST:PINS:STAG SDA,1e3", f'-104,"Data type error;{STAGE}"'),
        ("SYST:PINS:STAG SDA,#H1G", f'-120,"Numeric data error;{STAGE}"'),
        ("SYST:PINS:STAG SDA,99999999999x", f'-222,"Data out of range;{STAGE}"'),
        ("SYST:PINS:STAG SDA,8,HIGH", f'-224,"Illegal parameter value;{STAGE}"'),
        ("SYST:PINS:STAG REL1,8,UP", f'-224,"Illegal parameter value;{STAGE}"'),
        ("SYST:PINS:ADDR SOME", f'-224,"Illegal parameter value;{ADDRESS}"'),
        ("SYST:PINS:ADDR", f'-109,"Missing parameter;{ADDRESS}"'),
        ("SYST:PINS:ADDR 0", f'-222,"Data out of range;{ADDRESS}"'),
        ("SYST:PINS:ADDR 256", f'-222,"Data out of range;{ADDRESS}"'),
        ("SYST:PINS:ADDR 6.5", f'-104,"Data type error;{ADDRESS}"'),
        ("SYST:PINS:CLOC", '-109,"Missing parameter;use SYSTem:PINS:CLOCk <hz>"'),
        ("SENS:AVER MAYBE", '-224,"Illegal parameter value;use SENSe:AVERage[:STATe] ON|OFF"'),
        (
            "SENS:AVER:COUN 6.4",
            '-104,"Data type error;use SENSe:AVERage:COUNt 1|4|16|64|128|256|512|1024"',
        ),
        (
            "SENS:CURR:APER LONG",
            '-104,"Data type error;use SENSe:CURRent:APERture <seconds> (0.00005..0.00412)"',
        ),
    ],
)
def test_a_refused_pin_or_averaging_unit_queues_the_firmwares_error_and_stages_nothing(
    unit: str, error: str
) -> None:
    sim = SimulatedMeter()
    before = ask(sim, "SYST:PINS:PEND?")
    assert ask(sim, unit) == error
    assert ask(sim, "SYST:PINS:PEND?") == before


def test_the_address_and_the_clock_are_staged_and_judged() -> None:
    sim = SimulatedMeter()
    pend = "SYST:PINS:PEND?"
    assert " addr=0x41 " in ask(sim, f"SYST:PINS:ADDR #H41;:{pend}")
    assert ask(sim, f"SYST:PINS:ADDR 255;:{pend}").endswith(
        ' valid=0 problem=address at=addr;0,"No error"'
    )
    assert " addr=auto " in ask(sim, f"SYST:PINS:ADDR AUTO;:{pend}")
    assert ask(sim, f"SYST:PINS:CLOC 49999;:{pend}").endswith(
        ' valid=0 problem=clock at=scl_hz;0,"No error"'
    )
    assert " scl_hz=100000 " in ask(sim, f"SYST:PINS:CLOC 100000;:{pend}")
    # A reboot takes the staging back to the live map; *RST does not.
    sim.write(b"*RST\n")
    assert " scl_hz=100000 " in ask(sim, pend)
    sim.reboot()
    assert ask(sim, pend).startswith(DEFAULT_MAP.text() + " valid=1")


def test_the_reports_carry_the_keys_0_18_0_appends() -> None:
    sim = SimulatedMeter()
    pins = f" pins_src=default pins_table={DIGEST}"
    assert ask(sim, "SYST:INF?").split(";")[0].endswith(pins)
    assert ask(sim, "SYST:HEAL?").split(";")[0].endswith(pins)
    assert (
        ask(sim, "ROUT:STAT?")
        .split(";")[0]
        .endswith(" relay1_gpio=7 relay1_active=low relay2_gpio=15 relay2_active=high")
    )
    assert ask(sim, "SENS:PROF STAT;:SENS:PROF:REP?").startswith(
        "profile=STATE adc_config=0xF140 period_us=1152 vshct_us=1052 avg=1"
        " avg_state=off vbusct_us=50"
    )
    assert ask(sim, "SENS:PROF TRAC;:SENS:PROF:REP?").split(";")[0].endswith(" vbusct_us=0")
    assert ask(sim, "SENS:CURR:APER?;:SENS:AVER:COUN?;:SENS:AVER?") == '0.00028;1;0;0,"No error"'
    assert ask(sim, "SENS:PROF SLE;:SENS:CURR:APER?;:SENS:AVER:COUN?") == '0.00412;16;0,"No error"'
    sim.write(b"SENS:PROF STAT;:INIT\n")
    sim.read(1 << 20)
    assert ask(sim, "ABOR;:FETC:HEAD?").split(";")[0].endswith(" avg_state=off pins_src=default")
