# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's pin routes: staged, applied or reverted, refused, the relays checked."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from kleidos_meter import pins
from kleidos_meter.client import MeterTimeout
from kleidos_meter.panel import pins as panel_pins
from kleidos_meter.panel import schema
from kleidos_meter.panel.executor import TIMEOUTS, analyze
from kleidos_meter.panel.settings import SettingsRefused
from kleidos_meter.panel.shaping import SETTINGS
from kleidos_meter.protocol import ErrorEntry
from test_panel_http import Panel, serving_panel, until


@pytest.fixture
def panel() -> Iterator[Panel]:
    with serving_panel() as served:
        yield served


@contextmanager
def wired(**changes: Any) -> Iterator[Panel]:
    """A panel on a simulated board wired with *changes* over the rig's."""
    wiring = replace(pins.RIG_WIRING, **changes)
    with serving_panel(sim_options={"wiring": wiring}) as panel:
        yield panel


def staged(panel: Panel, **changes: Any) -> None:
    """Stage *changes* through the API."""
    answer = panel.post("/api/pins/stage", changes)
    assert answer.status == 200 and answer.body["errors"] == [], answer.body
    panel.sent.clear()


def orders(panel: Panel) -> list[str]:
    """The messages the panel sent that are not queries only."""
    return [i.message for i in panel.sent if any(not (p or "?").endswith("?") for p in i.patterns)]


# -- reading and staging -----------------------------------------------------------------


def test_the_board_the_maps_and_the_result_are_read_as_the_instrument_says(panel: Panel) -> None:
    answer = panel.get("/api/pins")
    assert answer.status == 200
    body = answer.body
    assert list(body) == ["board", "table", "live", "pending", "result", "differs"]
    assert body["board"] == pins.BOARD
    digest = pins.DIGEST
    assert body["table"] == {"package": digest, "instrument": digest, "matches": True}
    assert body["live"]["sda"] == 4 and body["live"]["src"] == "default"
    assert body["live"]["verified"] == 1 and body["live"]["addr"] == "auto"
    assert body["pending"]["valid"] == 1 and body["differs"] == []
    assert body["result"]["result"] == "none" and body["result"]["mfg"] is None


def test_fields_are_staged_over_the_staged_map_and_judged(panel: Panel) -> None:
    first = panel.post("/api/pins/stage", {"sda": 18, "relay1": None, "addr": "0x41"})
    assert first.status == 200, first.body
    assert first.body["sent"] == [
        "SYST:PINS:STAG SDA,18,UP",
        "SYST:PINS:STAG REL1,NONE,LOW",
        "SYST:PINS:ADDR 65",
    ]
    assert (first.body["pending"]["valid"], first.body["pending"]["problem"]) == (0, "missing")
    assert first.body["pending"]["at"] == "relay1"
    then = panel.post("/api/pins/stage", {"relay1": 16, "pull_sda": "off", "scl_hz": 100000})
    assert then.body["sent"] == [
        "SYST:PINS:STAG SDA,18,OFF",
        "SYST:PINS:STAG REL1,16,LOW",
        "SYST:PINS:CLOC 100000",
    ]
    assert then.body["pending"]["valid"] == 1
    body = panel.get("/api/pins").body
    assert body["differs"] == ["sda", "addr", "scl_hz", "pull_sda", "relay1"]
    assert body["live"]["sda"] == 4  # nothing on the board changed
    cleared = panel.post("/api/pins/stage", {"clear": True, "addr": "auto", "alert": 8})
    assert cleared.body["sent"] == [
        "SYST:PINS:CLE",
        "SYST:PINS:STAG ALER,8,UP",
        "SYST:PINS:ADDR AUTO",
    ]
    assert panel.get("/api/pins").body["differs"] == ["alert"]
    panel.post("/api/pins/stage", {"pull_sda": "off"})
    again = panel.post("/api/pins/stage", {"clear": True, "sda": 17})  # over the live pull
    assert again.body["sent"] == ["SYST:PINS:CLE", "SYST:PINS:STAG SDA,17,UP"]
    assert panel.post("/api/pins/stage", {"clear": True}).body["sent"] == ["SYST:PINS:CLE"]
    assert panel.get("/api/pins").body["differs"] == []


def test_a_value_the_instrument_refuses_answers_409_with_its_error(panel: Panel) -> None:
    answer = panel.post("/api/pins/stage", {"sda": 999})
    assert answer.status == 409 and answer.body["errors"][0]["code"] in (-222, -224)
    assert answer.body["pending"]["sda"] == 4


@pytest.mark.parametrize(
    ("body", "error", "said"),
    [
        ({}, "bad_body", "at least one field"),
        ({"clear": False}, "bad_body", "at least one field"),
        ({"clear": "yes"}, "bad_value", "true or false"),
        ({"bogus": 1}, "unknown_field", "'bogus'"),
        ({"sda": True}, "bad_value", "sda takes a number"),
        ({"sda": 4.5}, "bad_value", "sda takes a number"),
        ({"sda": "4;:ROUT:LEAD OPEN,FORC"}, "bad_value", "sda takes a number"),
        ({"addr": [1]}, "bad_value", "addr takes a number"),
        ({"scl_hz": None}, "bad_value", "scl_hz takes a number"),
        ({"pull_sda": "down"}, "bad_value", "pull_sda is up or off"),
        ({"relay2_active": 1}, "bad_value", "relay2_active is high or low"),
        ([1], "bad_body", "JSON object"),
    ],
)
def test_a_stage_of_the_wrong_form_is_refused_unsent(
    panel: Panel, body: object, error: str, said: str
) -> None:
    answer = panel.post("/api/pins/stage", body)
    assert (answer.status, answer.body["error"]) == (400, error)
    assert said in answer.body["detail"]
    assert panel.sent == []


def test_a_change_without_the_header_is_refused_on_every_pin_route(panel: Panel) -> None:
    for path in ("stage", "apply", "defaults"):
        answer = panel.request("POST", f"/api/pins/{path}", {"sda": 18})
        assert (answer.status, answer.body["error"]) == (403, "header_missing"), path
    assert panel.sent == []


def test_another_table_is_said_and_refuses_staging_and_applying_but_not_the_defaults(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged(panel, sda=18)
    planted = SimpleNamespace(**{**vars(pins), "DIGEST": "0x00000001"})
    monkeypatch.setattr(panel_pins, "pins", planted)
    assert panel.get("/api/pins").body["table"] == {
        "package": "0x00000001",
        "instrument": pins.DIGEST,
        "matches": False,
    }
    panel.sent.clear()
    for path in ("stage", "apply"):
        answer = panel.post(f"/api/pins/{path}", {"sda": 17})
        assert (answer.status, answer.body["error"]) == (409, "table_differs"), path
        assert pins.DIGEST in answer.body["detail"] and "0x00000001" in answer.body["detail"]
    assert orders(panel) == []
    assert panel.post("/api/pins/defaults").status == 200


# -- applying ------------------------------------------------------------------------------


def test_a_map_proved_is_kept_and_the_clock_it_runs_is_the_one_serve_leaves() -> None:
    with wired(sda=18) as panel:
        staged(panel, sda=18, scl_hz=100000)
        answer = panel.post("/api/pins/apply")
        assert answer.status == 200, answer.body
        body = answer.body
        assert body["order"] == "SYST:PINS:APPL" and body["errors"] == []
        assert body["result"]["result"] == "ok" and body["result"]["saved"] == 1
        assert (body["live"]["sda"], body["live"]["src"]) == (18, "stored")
        assert body["pending"]["sda"] == 18
        assert body["check"] is None  # no relay moved
        assert orders(panel) == ["SYST:PINS:APPL;:SYST:PINS?;:SYST:PINS:PEND?;:SYST:PINS:RES?"]
        assert panel.api.session.shaping.snapshot["i2c_hz"] == "100000"
        assert panel.post("/api/settings", {"i2c_hz": 400000}).status == 200
        notes, clean = panel.api.session.close()
        assert clean and notes == ["wrote back i2c_hz 100000 (it was 400000)"]
        assert panel.sim.pins_live.sda == 18 and panel.sim.pins_live.scl_hz == 100000


def test_a_map_that_is_not_proved_is_reverted_and_answers_409_with_the_one_error(
    panel: Panel,
) -> None:
    assert panel.post("/api/settings", {"i2c_hz": 200000}).status == 200
    staged(panel, sda=18)
    answer = panel.post("/api/pins/apply")
    assert answer.status == 409
    assert panel.api.session.shaping.snapshot["i2c_hz"] == "400000"  # still the one to leave
    body = answer.body
    assert [e["code"] for e in body["errors"]] == [117]
    assert (body["result"]["result"], body["result"]["reason"]) == ("reverted", "no_chip")
    assert (body["live"]["sda"], body["pending"]["sda"]) == (4, 18)
    assert body["check"] is None


def test_a_map_the_rules_refuse_changes_nothing(panel: Panel) -> None:
    staged(panel, relay1=45)
    answer = panel.post("/api/pins/apply")
    assert answer.status == 409 and [e["code"] for e in answer.body["errors"]] == [-221]
    assert answer.body["result"]["result"] == "refused"
    assert answer.body["live"]["relay1"] == 7


def test_the_defaults_come_back_with_no_ina228(panel: Panel) -> None:
    staged(panel, sda=18)
    panel.sim.device_ready = False
    answer = panel.post("/api/pins/defaults")
    assert answer.status == 200, answer.body
    assert answer.body["order"] == "SYST:PINS:DEF"
    assert answer.body["result"]["reason"] == "no_chip"
    assert (answer.body["live"]["src"], answer.body["pending"]["sda"]) == ("default", 4)


# -- the check -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wiring", "stage"),
    [
        ({"relay1": 16}, {"relay1": 16}),
        ({"relay2": 16}, {"relay2": 16}),
        ({"relay1_active": "high"}, {"relay1_active": "high"}),
        ({"relay2_active": "low"}, {"relay2_active": "low"}),
    ],
)
def test_a_change_that_moved_a_relay_makes_the_null_path_and_reports_it(
    wiring: dict[str, Any], stage: dict[str, Any]
) -> None:
    with wired(**wiring) as panel:
        staged(panel, **stage)
        answer = panel.post("/api/pins/apply")
        assert answer.status == 200, answer.body
        check = answer.body["check"]
        assert check["passed"] is True and check["errors"] == [] and check["detail"] == ""
        assert check["result"]["path"] == "bypass" and check["state"]["path"] == "shunt"
        assert answer.body["live"]["verified"] == 1 and answer.body["live"]["src"] == "stored"
        assert orders(panel)[1:] == [
            "ROUT:NULL ON;:ROUT:RES?",
            "ROUT:NULL OFF;:ROUT:STAT?;:SYST:PINS?",
        ]
        assert panel.sim.path == "shunt"
        # The way back moves it again, and is checked too.
        back = panel.post("/api/pins/defaults")
        assert back.status == 200 and back.body["check"]["passed"] is False


def test_a_relay_on_a_pad_that_is_not_its_own_fails_its_check_and_the_map_stays_unverified(
    panel: Panel,
) -> None:
    staged(panel, relay1=16)
    answer = panel.post("/api/pins/apply")
    assert answer.status == 200  # the map is applied and kept: only its check failed
    check = answer.body["check"]
    assert check["passed"] is False and [e["code"] for e in check["errors"]] == [104]
    assert check["detail"] == "the null path was not seen to work: the relays stay unverified"
    assert check["state"]["path"] == "shunt" and panel.sim.path == "shunt"
    assert (answer.body["live"]["relay1"], answer.body["live"]["verified"]) == (16, 0)


@pytest.mark.parametrize("fails", ["ROUT:NULL ON", "ROUT:NULL OFF"])
def test_a_line_that_fails_during_the_check_is_reported_with_what_was_applied(fails: str) -> None:
    with wired(relay1=16) as panel:
        staged(panel, relay1=16)
        real = panel.executor.call

        def call(message: str, **kwargs: Any) -> Any:
            if message.startswith(fails):
                raise MeterTimeout("no answer within 10 s")
            return real(message, **kwargs)

        panel.api.pins.executor = SimpleNamespace(  # type: ignore[assignment]  # a stand-in
            call=call, query_all=panel.executor.query_all
        )
        answer = panel.post("/api/pins/apply")
        assert answer.status == 200 and answer.body["result"]["result"] == "ok"
        check = answer.body["check"]
        assert check["passed"] is False
        assert f"{fails}: no answer within 10 s" in check["detail"]
        if fails == "ROUT:NULL ON":
            assert check["result"] is None and check["state"]["path"] == "shunt"
        else:
            assert (
                check["state"] is None and "may be left as ROUT:NULL ON made it" in check["detail"]
            )
        assert not panel.api.pins.running


def test_a_path_that_is_released_with_an_error_is_not_a_check_passed() -> None:
    with wired(relay1=16) as panel:
        staged(panel, relay1=16)
        real = panel.executor.call

        def call(message: str, **kwargs: Any) -> Any:
            reply = real(message, **kwargs)
            stuck = [ErrorEntry(109, "Relay not seen to move;k1")]
            return replace(reply, errors=stuck) if message.startswith("ROUT:NULL OFF") else reply

        panel.api.pins.executor = SimpleNamespace(  # type: ignore[assignment]  # a stand-in
            call=call, query_all=panel.executor.query_all
        )
        check = panel.post("/api/pins/apply").body["check"]
        assert check["passed"] is False and [e["code"] for e in check["errors"]] == [109]


# -- the states that refuse it -------------------------------------------------------------


def refused(panel: Panel, status: int, error: str, said: str) -> None:
    """Both orders are refused with *status* and *error*, and neither is sent."""
    for path in ("apply", "defaults"):
        answer = panel.post(f"/api/pins/{path}")
        assert (answer.status, answer.body["error"]) == (status, error), answer.body
        assert said in answer.body["detail"]
    assert not any(
        "SYST:PINS:APPL" in i.message or "SYST:PINS:DEF" in i.message for i in panel.sent
    )


def test_an_order_is_refused_while_a_capture_runs(panel: Panel) -> None:
    panel.recorder.start("STAT")
    refused(panel, 409, "capture_running", "a capture is running: stop it first")
    panel.recorder.stop()


def test_an_order_is_refused_while_a_sequence_runs(panel: Panel) -> None:
    assert panel.post("/api/sequences/run", {"name": "NULL_LEAD"}).status == 200
    until(lambda: panel.sequences.state_json()["state"] == "prompt", "the question")
    refused(panel, 409, "sequence_running", "a sequence is running")
    with pytest.raises(SettingsRefused) as seen:  # past the route's own check
        panel.api.pins.apply()
    assert (seen.value.status, seen.value.error) == (409, "sequence_running")
    panel.sequences.abort()
    until(lambda: not panel.sequences.running, "the sequence's end")


def test_an_order_is_refused_while_another_controller_holds_the_line(panel: Panel) -> None:
    panel.api.hold("a passthrough client (127.0.0.1:1)")
    refused(panel, 423, "held", "held by a passthrough client (127.0.0.1:1)")
    with pytest.raises(SettingsRefused) as seen:  # held between the route's check and the order
        panel.api.pins.apply()
    assert (seen.value.status, seen.value.error) == (423, "held")
    assert seen.value.body()["holder"] == "a passthrough client (127.0.0.1:1)"
    panel.api.release()


@pytest.mark.parametrize(
    ("order", "path"),
    [("ROUT:BYP ON", "both"), ("ROUT:NULL ON", "bypass"), ("ROUT:LEAD OPEN,FORC", "none")],
)
def test_an_order_is_refused_while_a_relay_is_not_at_rest(
    panel: Panel, order: str, path: str
) -> None:
    assert panel.executor.call(order).errors == []
    with panel.sim._lock:  # the executor's reads service the hold meanwhile
        panel.sim.hold_due = None  # the fake clock runs ahead: the hold must not release
    refused(panel, 409, "relays_not_at_rest", f"path={path}")
    assert panel.post("/api/pins/apply").body["state"]["path"] == path
    assert not panel.api.pins.running


def test_an_order_is_refused_while_a_window_serve_did_not_open_is_open(panel: Panel) -> None:
    assert panel.executor.call("SENS:PROF STAT;:INIT").errors == []
    panel.api.session.note_window({"sink": "stream"})
    refused(panel, 409, "window_open", "left alone")
    assert panel.post("/api/abort").status == 200


def test_a_window_opened_by_hand_is_the_instruments_to_refuse(panel: Panel) -> None:
    assert panel.post("/api/scpi", "SENS:PROF STAT;:INIT").status == 200
    answer = panel.post("/api/pins/apply")
    assert answer.status == 409 and [e["code"] for e in answer.body["errors"]] == [-221]
    assert answer.body["check"] is None
    assert panel.post("/api/abort").status == 200


def test_while_one_is_made_every_other_change_is_refused_but_an_abort(panel: Panel) -> None:
    with panel.api.pins._changing():
        assert panel.api.pins.running
        for path in ("/api/settings", "/api/relays/bypass", "/api/pins/apply", "/api/scpi"):
            answer = panel.post(path, "{}")
            assert (answer.status, answer.body["error"]) == (409, "pins_changing"), path
            assert answer.body["detail"] == panel_pins.CHANGING
        assert panel.post("/api/abort").status == 409  # the instrument's: nothing to abort
        assert panel.get("/api/pins").status == 200
        with pytest.raises(SettingsRefused) as seen:  # two that passed the route's check
            panel.api.pins.apply()
        assert (seen.value.status, seen.value.error) == (409, "pins_changing")
    assert not panel.api.pins.running


def test_closing_waits_for_the_order_made_and_takes_no_other(panel: Panel) -> None:
    changes = panel.api.pins
    closer = threading.Thread(target=changes.close)
    with changes._changing():
        closer.start()
        until(lambda: changes._closed, "the close to begin")
        assert closer.is_alive()
    closer.join(10)
    assert not closer.is_alive()
    answer = panel.post("/api/pins/apply")
    assert (answer.status, answer.body["error"]) == (503, "closing")


def test_a_serve_with_no_recorder_and_no_sequences_applies(panel: Panel) -> None:
    bare = panel_pins.PinChanges(panel.api.session)
    status, body = bare.apply(defaults=True)
    assert status == 200 and body["check"] is None


# -- what is declared ------------------------------------------------------------------------


def test_the_pin_map_is_no_capture_shaping_setting(panel: Panel) -> None:
    assert not any(s.pattern.startswith("SYSTem:PINS") for s in SETTINGS)
    changed_by = frozenset().union(*(s.changed_by for s in SETTINGS))
    assert changed_by.isdisjoint(schema.PIN_ORDERS)
    assert not set(pins.KEYS) & set(panel.api.session.shaping.snapshot)
    served = panel.get("/api/schema").body
    assert served["pin_orders"] == ["SYSTem:PINS:APPLy", "SYSTem:PINS:DEFault"]


def test_a_pin_order_waits_as_long_as_a_self_test() -> None:
    for order in ("SYST:PINS:APPL", "SYST:PINS:DEF"):
        assert TIMEOUTS[analyze(order).kind] == TIMEOUTS["test"]


@pytest.mark.parametrize("ok", [True, False])
def test_a_map_applied_by_hand_takes_its_clock_too_when_it_was_applied(ok: bool) -> None:
    with wired(sda=18 if ok else 4) as panel:
        if not ok:  # serve's own change stays serve's to put back
            assert panel.post("/api/settings", {"i2c_hz": 200000}).status == 200
        order = "SYST:PINS:STAG SDA,18,UP;:SYST:PINS:CLOC 100000\nSYST:PINS:APPL"
        answer = panel.post("/api/scpi", order)
        assert answer.status == 200 and (answer.body["replies"][1]["errors"] == []) is ok
        clock = panel.api.session.shaping.snapshot["i2c_hz"]
        assert clock == ("100000" if ok else "400000")
