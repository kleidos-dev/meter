# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The pin map's Python port (``kleidos_meter.pins``) against the firmware's verdicts."""

from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from kleidos_meter import pins
from kleidos_meter.pins import (
    BOARD,
    DEFAULT_MAP,
    DIGEST,
    KEYS,
    RIG_WIRING,
    ROLES,
    Boot,
    PinMap,
    Proof,
    Wiring,
    address_of,
    address_text,
    answers,
    boot_map,
    prove,
    relay_check,
    rest_state,
    validate_map,
    verdict_text,
)

#: The verdicts every port of the rules must give, which the firmware's host
#: tests print from ``pinmap::validate()``.
VECTORS = Path(__file__).resolve().parents[2] / "firmware/test/native/golden/pinmap.vectors"


def vector_lines() -> list[str]:
    """The vectors' lines, comments left out, after checking they are of this table."""
    text = VECTORS.read_text(encoding="utf-8")
    assert f"# table {DIGEST}\n" in text, "the vectors are of another table"
    assert f"# columns: {' '.join(KEYS)} | valid problem at\n" in text
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def test_the_python_rules_give_every_vector_the_firmwares_verdict() -> None:
    lines = vector_lines()
    assert len(lines) > 2900
    wrong = []
    for line in lines:
        given, verdict = line.split(" | ")
        columns = given.split()
        pin_map = PinMap.from_words(dict(zip(KEYS, columns, strict=True)))
        # The map reads back as the vector wrote it, word for word.
        assert list(pin_map.words().values()) == columns, line
        valid, problem, at = verdict.split()
        expected = None if valid == "1" else (problem, at)
        found = validate_map(pin_map)
        if found != expected:
            wrong.append(f"{line}: Python says {found}")
        assert verdict_text(found) == f"valid={valid} problem={problem} at={at}"
    assert not wrong, "\n".join(wrong[:20])


def test_the_vectors_reach_every_problem_at_every_role() -> None:
    seen = {tuple(line.split(" | ")[1].split()[1:]) for line in vector_lines()}
    for problem in ("missing", "no_pin", "reserved", "shared"):
        for role in ROLES:
            if (problem, role) == ("shared", "sda"):
                continue  # the first role has no earlier role to share with
            assert (problem, role) in seen, (problem, role)
    assert {("too_high", "alert"), ("address", "addr"), ("clock", "scl_hz")} <= seen


def test_the_default_map_is_the_tables_and_keeps_every_rule() -> None:
    assert validate_map(DEFAULT_MAP) is None
    assert DEFAULT_MAP.text() == (
        "sda=4 scl=5 alert=6 addr=auto scl_hz=400000 pull_sda=up pull_scl=up pull_alert=up"
        " relay1=7 relay1_active=low relay2=15 relay2_active=high"
    )
    assert ROLES == ("sda", "scl", "alert", "relay1", "relay2")
    assert BOARD["digest"] == DIGEST and DIGEST.startswith("0x")
    assert DEFAULT_MAP.bus() == (4, 5, 0)
    assert DEFAULT_MAP.relays() == ((7, "low"), (15, "high"))
    assert DEFAULT_MAP.held(15) and not DEFAULT_MAP.held(16)


def test_addresses_read_and_write_as_the_reports_spell_them() -> None:
    assert address_of("auto") == 0 and address_of("0x4F") == 0x4F and address_of("65") == 65
    assert address_text(0) == "auto" and address_text(0x41) == "0x41"
    with pytest.raises(ValueError):
        address_of("x41")


def test_a_map_reads_from_a_whole_report_and_says_what_is_missing() -> None:
    report = DEFAULT_MAP.words() | {"src": "default", "verified": "1", "table": DIGEST}
    assert PinMap.from_words(report) == DEFAULT_MAP
    with pytest.raises(KeyError):
        PinMap.from_words({"sda": "4"})
    with pytest.raises(ValueError):
        PinMap.from_words(report | {"scl_hz": "fast"})


def test_the_rules_read_a_planted_table() -> None:
    board = {**BOARD, "rows": [row for row in BOARD["rows"] if row["gpio"] != 4]}
    assert validate_map(DEFAULT_MAP, board) == ("no_pin", "sda")


def test_a_roles_limit_is_the_first_gpio_it_cannot_take() -> None:
    """The table's ``below``: 32 for ALERT, whose neighbors on this board are reserved anyway."""
    alert = DEFAULT_MAP.alert
    assert alert is not None

    def limited(below: int) -> dict[str, Any]:
        roles = [{**r, "below": below} if r["key"] == "alert" else r for r in BOARD["roles"]]
        return {**BOARD, "roles": roles}

    assert validate_map(DEFAULT_MAP, limited(alert + 1)) is None
    assert validate_map(DEFAULT_MAP, limited(alert)) == ("too_high", "alert")


# -- a board's wiring -----------------------------------------------------------------------


def test_the_rig_is_the_default_maps_pads_with_the_ina228_at_0x40() -> None:
    assert Wiring(4, 5, 6, 0x40, 7, "low", 15, "high") == RIG_WIRING
    assert Wiring.parse("") == Wiring.parse(" , ") == RIG_WIRING
    assert RIG_WIRING.text() == (
        "sda=4,scl=5,alert=6,address=0x40,relay1=7,relay1_active=low,relay2=15,relay2_active=high"
    )
    assert Wiring.parse(RIG_WIRING.text()) == RIG_WIRING


def test_a_wiring_is_the_rig_with_the_pairs_it_names() -> None:
    wiring = Wiring.parse("SDA=18, addr=0x41,relay1_active=HIGH,relay2=16")
    assert wiring == replace(RIG_WIRING, sda=18, address=0x41, relay1_active="high", relay2=16)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("sda", "not one of"),
        ("pin=4", "not one of"),
        ("relay1_active=up", "high or low"),
        ("sda=x", "invalid literal"),
        ("addr=0x50", "straps 0x40 to 0x4F"),
        ("address=63", "straps 0x40 to 0x4F"),
    ],
)
def test_a_wiring_that_does_not_read_is_refused_with_why(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Wiring.parse(text)


@pytest.mark.parametrize(
    ("changes", "fitted", "proof"),
    [
        ({}, True, Proof(True, 0, "-")),
        ({"addr": 0x40}, True, Proof(True, 0, "-")),
        ({"addr": 0x41}, True, Proof(False, 1, "no_chip")),
        ({"sda": 18}, True, Proof(False, 1, "no_chip")),
        ({"scl": 18}, True, Proof(False, 1, "no_chip")),
        ({"sda": 5, "scl": 4}, True, Proof(False, 1, "no_chip")),
        ({"alert": 16}, True, Proof(True, 32, "alert")),
        ({"relay1": 16, "relay2": 17}, True, Proof(True, 0, "-")),
        ({}, False, Proof(False, 1, "no_chip")),
    ],
)
def test_a_map_is_proved_by_the_ina228s_answer_and_its_alert_wire(
    changes: dict[str, Any], fitted: bool, proof: Proof
) -> None:
    pin_map = replace(DEFAULT_MAP, **changes)
    assert prove(pin_map, RIG_WIRING, fitted=fitted) == proof
    assert answers(pin_map, RIG_WIRING, fitted=fitted) == proof.answered
    assert proof.passed == (proof.tst == 0)


@pytest.mark.parametrize(
    ("changes", "failed"),
    [
        ({}, None),
        ({"relay2": 16}, "bypass"),
        ({"relay2_active": "low"}, "bypass"),
        ({"relay1": 16, "relay2": 17}, "bypass"),
        ({"relay1": 16}, "lead"),
        ({"relay1_active": "high"}, "lead"),
        ({"sda": 18, "alert": 16}, None),
        ({"relay1": 15, "relay1_active": "high", "relay2": 7, "relay2_active": "low"}, "lost"),
    ],
)
def test_a_null_path_check_fails_at_the_relay_on_the_wrong_pad_or_level(
    changes: dict[str, Any], failed: str | None
) -> None:
    assert relay_check(replace(DEFAULT_MAP, **changes), RIG_WIRING) == failed


@pytest.mark.parametrize(
    ("changes", "state"),
    [
        ({}, (False, False)),
        ({"relay1_active": "high"}, (True, False)),
        ({"relay2_active": "low"}, (False, True)),
        ({"relay1": 16, "relay2": 17}, (False, False)),
        (
            {"relay1": 15, "relay1_active": "high", "relay2": 7, "relay2_active": "low"},
            (False, False),
        ),
        ({"relay1": 15, "relay2": 7}, (True, True)),
    ],
)
def test_the_relays_at_rest_energize_a_part_only_at_its_own_level(
    changes: dict[str, Any], state: tuple[bool, bool]
) -> None:
    assert rest_state(replace(DEFAULT_MAP, **changes), RIG_WIRING) == state


# -- the boot -------------------------------------------------------------------------------


def test_a_boot_with_no_record_or_one_the_table_refuses_runs_the_default_map() -> None:
    assert boot_map(None, RIG_WIRING) == Boot(DEFAULT_MAP, "default")
    assert boot_map(replace(DEFAULT_MAP, sda=45), RIG_WIRING) == Boot(DEFAULT_MAP, "default")
    assert boot_map(DEFAULT_MAP, RIG_WIRING) == Boot(DEFAULT_MAP, "default")


def test_a_boot_takes_a_stored_map_whose_bus_answers_whatever_its_alert_and_relays() -> None:
    stored = replace(DEFAULT_MAP, scl_hz=100_000, alert=16, relay1=17, addr=0x40)
    assert boot_map(stored, RIG_WIRING) == Boot(stored, "stored")
    moved = Wiring.parse("sda=18,scl=21")
    on_it = replace(DEFAULT_MAP, sda=18, scl=21)
    assert boot_map(on_it, moved) == Boot(on_it, "stored")


def test_a_stored_bus_that_does_not_answer_falls_back_to_the_default_bus() -> None:
    stored = replace(
        DEFAULT_MAP, sda=18, scl=21, addr=0x41, scl_hz=100_000, pull_sda="off", pull_alert="off"
    )
    found = boot_map(stored, RIG_WIRING)
    assert found.src == "fallback"
    assert found.pins == replace(stored, sda=4, scl=5, addr=0, scl_hz=400_000, pull_sda="up")
    assert found.problem is not None and "sda=18 scl=21 addr=0x41" in found.problem
    assert "the default bus is back" in found.problem
    # Swapped, the default pads are the stored bus's own: they fall back too.
    assert boot_map(replace(DEFAULT_MAP, sda=5, scl=4), RIG_WIRING).src == "fallback"
    # With no INA228 at all the fallback is still taken: the bus it would answer on.
    assert boot_map(stored, RIG_WIRING, fitted=False).src == "fallback"


@pytest.mark.parametrize("role", ["alert", "relay1", "relay2"])
@pytest.mark.parametrize("pad", [4, 5])
def test_a_stored_bus_whose_default_pads_another_role_holds_does_not_fall_back(
    role: str, pad: int
) -> None:
    held: dict[str, Any] = {role: pad}
    stored = replace(DEFAULT_MAP, sda=18, scl=21, **held)
    assert validate_map(stored) is None
    found = boot_map(stored, RIG_WIRING)
    assert (found.pins, found.src) == (stored, "stored")
    assert found.problem is not None and "no INA228" in found.problem


def test_the_module_names_every_problem_the_vectors_use() -> None:
    words = {line.split(" | ")[1].split()[1] for line in vector_lines()} - {"-"}
    assert words == set(pins.PROBLEMS)


@pytest.mark.parametrize(
    ("key", "value", "typed"),
    [
        ("sda", " 18 ", 18),
        ("sda", "0x12", 18),
        ("sda", 18, 18),
        ("sda", -1, -1),  # the form is right; the range is the rules'
        ("relay1", "NONE", None),
        ("relay1", None, None),
        ("addr", "Auto", "auto"),
        ("addr", 0x41, 0x41),
        ("scl_hz", "400000", 400000),
        ("pull_alert", "OFF", "off"),
        ("relay2_active", "High", "high"),
    ],
)
def test_a_change_is_typed_from_a_persons_word_or_a_number(
    key: str, value: object, typed: int | str | None
) -> None:
    assert pins.typed_change(key, value) == typed


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("sda", "auto"),
        ("sda", ""),
        ("sda", True),
        ("sda", 4.0),
        ("addr", "none"),
        ("addr", None),
        ("scl_hz", "fast"),
        ("pull_sda", "down"),
        ("pull_sda", None),
        ("relay1_active", 1),
    ],
)
def test_a_change_of_another_form_is_refused_saying_what_the_field_takes(
    key: str, value: object
) -> None:
    with pytest.raises(ValueError, match=rf"^{key} (takes a number|is \w+ or \w+)$"):
        pins.typed_change(key, value)


def test_changes_are_staged_each_with_its_roles_other_half_as_it_is_staged() -> None:
    staged = replace(DEFAULT_MAP, pull_scl="off", relay2=16)
    assert pins.stage_units(staged, {}) == []
    assert pins.stage_units(staged, {"scl": 18, "relay2_active": "low", "addr": 0}) == [
        "SYST:PINS:STAG SCL,18,OFF",
        "SYST:PINS:STAG REL2,16,LOW",
        "SYST:PINS:ADDR AUTO",
    ]
    assert pins.stage_units(staged, {"alert": None, "addr": 0x4A, "scl_hz": 50000}) == [
        "SYST:PINS:STAG ALER,NONE,UP",
        "SYST:PINS:ADDR 74",
        "SYST:PINS:CLOC 50000",
    ]


def test_a_command_lines_wiring_reads_or_says_why_as_argparse_shows_it() -> None:
    assert pins.wiring_argument("relay2=16") == replace(RIG_WIRING, relay2=16)
    with pytest.raises(argparse.ArgumentTypeError, match="straps 0x40 to 0x4F"):
        pins.wiring_argument("addr=0x50")
