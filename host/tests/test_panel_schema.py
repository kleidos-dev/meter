# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's schema: held to the firmware's ``kCommands``, and its values checked."""

from __future__ import annotations

import dataclasses
import json
import re

import pytest
from conftest import firmware_commands
from kleidos_meter.panel import recorder, schema, settings, shaping
from kleidos_meter.panel.http import relay_unit
from kleidos_meter.panel.live import LiveHub
from kleidos_meter.panel.schema import (
    SCHEMA,
    SETTINGS,
    Argument,
    Relay,
    Route,
    SequenceInfo,
    Setting,
    get_refusal,
    problems,
    resolve_unit,
    short_form,
)
from kleidos_meter.sim_table import resolve
from test_panel_http import serving_panel


def planted(**changes: object) -> schema.Schema:
    """The schema with some of its declarations replaced."""
    return dataclasses.replace(SCHEMA, **changes)  # type: ignore[arg-type]  # Schema's fields


def setting(name: str) -> Setting:
    found = SCHEMA.setting(name)
    assert found is not None
    return found


# -- held to the firmware's table ------------------------------------------------------


def test_every_header_and_flag_the_schema_uses_is_the_firmwares() -> None:
    table = firmware_commands()
    assert len(table) > 100
    assert problems(table) == []


def test_a_planted_unknown_header_fails_the_check() -> None:
    table = firmware_commands()
    bogus = dataclasses.replace(setting("profile"), query="SENS:BOGUS?")
    found = problems(table, planted(settings=(bogus, *SETTINGS[1:])))
    assert found == ["setting profile: SENS:BOGUS? is not in the table"]


def test_each_kind_of_declaration_is_checked() -> None:
    table = firmware_commands()
    profile = setting("profile")
    cases: list[tuple[schema.Schema, str]] = [
        (
            planted(settings=(dataclasses.replace(profile, flags="R"),)),
            "setting profile: SENSe:PROFile has flags 'DRC', not 'R'",
        ),
        (
            planted(settings=(dataclasses.replace(profile, pattern="SENSe:PROFile:REPort"),)),
            "setting profile: SENS:PROF? is SENSe:PROFile?, not SENSe:PROFile:REPort?",
        ),
        (
            planted(
                settings=(dataclasses.replace(profile, changed_by=frozenset({"SENSe:BOGus"})),)
            ),
            "setting profile: changed by SENSe:BOGus, which is not in the table",
        ),
        (
            planted(relays=(Relay("x", "X", "ROUT:BOG", "ROUTe:BOGus", ("ON",), ()),)),
            "relay x: ROUT:BOG is not in the table",
        ),
        (
            planted(relays=(Relay("x", "X", "ROUT:LEAD", "ROUTe:BYPass", ("ON",), ()),)),
            "relay x: ROUT:LEAD is ROUTe:LEAD, not ROUTe:BYPass",
        ),
        (
            planted(sequences=(SequenceInfo("S", "S", "s", ("SENS:BOG",)),)),
            "sequence S: SENS:BOG is not in the table",
        ),
        (
            planted(routes=(Route("GET", "/x", False, "x", ("SYST:BOG?",)),)),
            "route GET /x: SYST:BOG? is not in the table",
        ),
        (
            planted(clearing_queries=frozenset({"SYSTem:BOGus?"})),
            "clearing query SYSTem:BOGus? is not in the table",
        ),
        (
            planted(pin_orders=frozenset({"SYSTem:PINS:BOGus"})),
            "pin order SYSTem:PINS:BOGus is not in the table",
        ),
    ]
    for declared, problem in cases:
        assert problem in problems(table, declared)


def test_a_command_the_firmware_renames_fails_the_check_against_its_table() -> None:
    table = [row for row in firmware_commands() if row[0] != "SENSe:PROFile"]
    table.append(("SENSe:SHAPe", "DRC", 1))  # a table the simulator never compiled
    assert "setting profile: SENS:PROF is not in the table" in problems(table)


def test_the_shaping_guard_restores_the_settings_the_schema_declares() -> None:
    assert shaping.SETTINGS is SETTINGS
    assert shaping.ShapingSetting is Setting
    for item in SETTINGS:
        assert item.pattern in item.changed_by, item.name  # a write marks its own setting


def test_the_schema_is_served_as_json_with_the_mutation_header() -> None:
    served = json.loads(json.dumps(SCHEMA.to_json()))
    assert served["schema"] == "kleidos.meter.panel/1"
    assert served["mutation_header"] == {"name": "X-Kleidos-Meter", "value": "1"}
    assert [s["name"] for s in served["settings"]] == [s.name for s in SETTINGS]
    assert served["settings"][0]["changed_by"] == ["SYSTem:COMMunicate:I2C:FREQuency"]
    assert {r["name"] for r in served["relays"]} >= {"lead", "bypass", "nullpath"}
    assert [s["name"] for s in served["sequences"]][:2] == ["HEALTH", "NULL_RELAY"]
    assert "*ESR?" in served["clearing_queries"]
    assert "elf" in served["text_keys"]
    assert SCHEMA.setting("nope") is None and SCHEMA.relay("nope") is None


# -- what a route takes ----------------------------------------------------------------


def _taken(method: str, path: str) -> dict[str, Argument]:
    """What the route declares it takes, by name."""
    found = next(r for r in SCHEMA.routes if (r.method, r.path) == (method, path))
    return {argument.name: argument for argument in found.takes}


def test_every_route_says_what_it_takes_and_where_and_what_it_answers() -> None:
    for route in SCHEMA.routes:
        where = f"{route.method} {route.path}"
        assert route.answers, where
        in_path = {a.name for a in route.takes if a.where == "path"}
        assert in_path == set(re.findall(r"\{(\w+)\}", route.path)), where
        places = {a.where for a in route.takes} - {"path"}
        assert places <= ({"query"} if route.method == "GET" else {"json", "text"}), where
        assert len({a.name for a in route.takes}) == len(route.takes), where


@pytest.mark.parametrize(
    ("path", "order"),
    [
        ("/api/capture/start", {"profile": "NOPE"}),
        ("/api/sequences/run", {"name": "NOPE"}),
        ("/api/sequences/answer", {"id": 0, "answer": "no"}),
    ],
)
def test_a_body_takes_the_keys_the_schema_declares_and_no_other(
    path: str, order: dict[str, object]
) -> None:
    taken = _taken("POST", path)
    given = {**dict.fromkeys(taken), **order}
    if "params" in given:
        given["params"] = {}
    with serving_panel() as panel:
        declared = panel.post(path, given)
        assert declared.status in (400, 404, 409) and declared.body["error"] != "bad_body"
        assert panel.post(path, {**given, "another": 1}).body["error"] == "bad_body"
        required = {name for name, argument in taken.items() if argument.required}
        for name in required:
            short = {key: value for key, value in given.items() if key != name}
            assert panel.post(path, short).body["error"] == "bad_body", name


def test_an_override_takes_the_keys_the_schema_declares() -> None:
    preview = _taken("GET", "/api/settings/shunt/preview")
    written = _taken("POST", "/api/settings/shunt")
    assert set(written) == {*preview, "confirm"}
    uncertainty = {name for name in preview if name.startswith(("expanded_", "tolerance_"))}
    assert uncertainty == {key for key, _, _ in settings._UNCERTAINTY_KEYS}
    for name in uncertainty:
        for value in ("ohms", "uohm"):
            order = {"range": 0, value: 1.0, name: 1.0, "note": ""}
            settings.parse_override(order)  # every declared combination is taken
            with pytest.raises(settings.SettingsRefused, match="takes no another"):
                settings.parse_override({**order, "another": 1})


def test_a_relay_order_takes_the_keys_the_schema_declares() -> None:
    taken = {n for n, a in _taken("POST", "/api/relays/{relay}").items() if a.where == "json"}
    lead = SCHEMA.relay("lead")
    cycle = SCHEMA.relay("lead_cycle")
    assert lead is not None and cycle is not None
    flags = {"force": True, "live": True}
    assert relay_unit(lead, {"state": "OPEN", **flags}) == "ROUT:LEAD OPEN,FORC,LIVE"
    assert relay_unit(cycle, {"ms": 5, **flags}) == "ROUT:LEAD:CYCL 5,FORC,LIVE"
    assert taken == {"state", "ms", *flags}


def test_the_figures_the_schema_states_are_the_ones_the_code_holds() -> None:
    start = _taken("POST", "/api/capture/start")
    assert f"at most {recorder.MAX_SECONDS:.0f} " in start["seconds"].takes
    assert start["label"].takes.endswith(f"at most {recorder._MAX_LABEL}")
    decimated = _taken("GET", "/api/runs/{run}/decimated")
    assert f"1 to {recorder.MAX_BUCKETS} " in decimated["buckets"].takes
    for column in recorder._CHANNEL_OF:
        assert f"`{column}`" in decimated["column"].takes
    for kind in recorder.EXPORTS:
        assert f"`{kind}`" in _taken("GET", "/api/runs/{run}/export")["format"].takes
    mark = _taken("POST", "/api/capture/mark")["label"].takes
    assert mark.startswith("1 to 15 printable characters, no space")
    assert recorder._MARK_LABEL.fullmatch("x" * 15) and not recorder._MARK_LABEL.fullmatch("x" * 16)
    assert recorder._MARK_LABEL.fullmatch("a b") is None
    events = next(r for r in SCHEMA.routes if r.path == "/api/events")
    assert LiveHub().max_streams == 8 and "eight others" in events.answers


# -- what a GET may send ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "refused"),
    [
        ("SYST:STAT?", None),
        ("SENS:PROF?", None),
        ("FETC:STAT?", None),
        ("ROUT:LEAD", "ROUTe:LEAD changes the instrument: POST it to /api/scpi"),
        ("MEAS:CURR?", "MEASure[:SCALar]:CURRent[:DC]? acts on the device: POST it to /api/scpi"),
        ("*TST?", "*TST? acts on the device: POST it to /api/scpi"),
        ("TRAC:DATA?", "TRACe:DATA? acts on the device: POST it to /api/scpi"),
        ("*ESR?", "reading *ESR? clears it: POST it to /api/scpi"),
        ("SYST:ERR?", "reading SYSTem:ERRor[:NEXT]? clears it: POST it to /api/scpi"),
        ("SYST:BOGUS?", "not a header of the instrument's table"),
    ],
)
def test_a_get_sends_only_queries_that_read_without_acting(
    header: str, refused: str | None
) -> None:
    assert get_refusal(resolve_unit(header)[0]) == refused


def test_a_unit_resolves_on_the_path_the_previous_one_left() -> None:
    resolved, path = resolve_unit("SENS:CURR:RANG:IND 1")
    assert resolved is not None and resolved.pattern == "SENSe:CURRent:RANGe:INDex"
    assert path == "SENS:CURR:RANG:"
    resolved, path = resolve_unit("REP?", path)
    assert resolved is not None and resolved.pattern == "SENSe:CURRent:RANGe:REPort?"
    resolved, kept = resolve_unit(" *IDN?", path)
    assert resolved is not None and resolved.pattern == "*IDN?" and kept == path
    resolved, path = resolve_unit(":SYST:STAT?", path)
    assert resolved is not None and resolved.pattern == "SYSTem:STATus?" and path == "SYST:"
    assert resolve_unit("FORM?")[1] == ""


def test_the_resolver_takes_another_table() -> None:
    table = [("SYSTem:BOGus?", "R", 2)]
    found = resolve("SYST:BOG?", table)
    assert found is not None and found.key == "SYST:BOG?"
    assert (found.flags, found.most) == ("R", 2)
    assert resolve("SYST:STAT?", table) is None


# -- values ----------------------------------------------------------------------------


def test_short_forms_are_the_leading_capitals_and_digits() -> None:
    assert short_form("STATe") == "STAT"
    assert short_form("IMMediate") == "IMM"
    assert short_form("0") == "0"
    assert short_form("lower") == "LOWER"


@pytest.mark.parametrize(
    ("name", "value", "parameter"),
    [
        ("profile", "STATe", "STAT"),
        ("profile", "state", "STAT"),
        ("profile", "STAT", "STAT"),
        ("profile", " burst ", "BURS"),
        ("range", "1", "1"),
        ("range", 0, "0"),
        ("capture", "STAT", "STAT"),
        ("trigger_source", "LEVel", "LEV"),
        ("i2c_hz", 1_000_000, "1000000"),
        ("i2c_hz", "400000", "400000"),
        ("sample_count", "+100", "+100"),
        ("hysteresis", 0.001, "0.001"),
        ("hysteresis", 0, "0"),
        ("trigger_level", "-1.5E-1", "-1.5E-1"),
        ("trigger_level", ".25", ".25"),
        ("threshold", "OFF", "OFF"),
        ("threshold", "off", "OFF"),
        ("threshold", 0.1, "0.1"),
        ("limit", True, "ON"),
        ("limit", False, "OFF"),
        ("limit", "1", "ON"),
        ("threshold_marks", "off", "OFF"),
        ("channels", ["CURR", "volt"], '"CURR","VOLT"'),
        ("channels", "DEFault", '""'),
        ("channels", "def", '""'),
    ],
)
def test_a_value_becomes_its_parameter(name: str, value: object, parameter: str) -> None:
    assert setting(name).parameter(value) == parameter


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("profile", "STATE;:ROUT:LEAD OPEN,FORC"),  # nothing rides along
        ("profile", "TRACER"),
        ("profile", 1),
        ("range", True),
        ("range", "2"),
        ("i2c_hz", True),
        ("i2c_hz", 1.5),
        ("i2c_hz", "4e5"),
        ("i2c_hz", "1;*RST"),
        ("hysteresis", float("inf")),
        ("hysteresis", "1 A"),
        ("hysteresis", None),
        ("threshold", "ON"),
        ("limit", "yes"),
        ("limit", 1),
        ("channels", []),
        ("channels", ["CURR", "CURR"]),
        ("channels", ["CURR", 'VOLT"']),
        ("channels", ["CURR", 3]),
        ("channels", '"CURR"'),
    ],
)
def test_a_value_of_the_wrong_form_is_refused(name: str, value: object) -> None:
    with pytest.raises(ValueError):
        setting(name).parameter(value)


def test_the_channels_of_a_ring_profile_are_written_back_as_the_default() -> None:
    channels = setting("channels")
    assert channels.write_unit('""') == "SENS:FUNC:ON:DEF"
    assert channels.write_unit('"CURR"') == 'SENS:FUNC:ON "CURR"'


def test_a_relay_takes_its_words_or_a_duration() -> None:
    lead, cycle = SCHEMA.relay("lead"), SCHEMA.relay("lead_cycle")
    assert lead is not None and cycle is not None
    assert lead.parameter("close") == "CLOS"
    assert cycle.parameter(500) == "500"
    with pytest.raises(ValueError):
        lead.parameter("SHUT")
    with pytest.raises(ValueError):
        cycle.parameter("500 ms")
