# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's HTTP API: every route and every guard, over real sockets on port 0.

The instrument is the clock-driven simulator on a fake clock, behind serve's
own session and executor, so a route's messages go through the same FIFO
as the panel's and the polls'.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import http.client
import io
import json
import logging
import re
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import SIM_SERIAL, FakeClock
from kleidos_meter.client import MeterError, MeterTimeout
from kleidos_meter.panel import http as panel_http
from kleidos_meter.panel.executor import (
    Executor,
    ExecutorBusy,
    ExecutorClosed,
    LineHeld,
    LinkDown,
    MessageInfo,
    MessageRefused,
    Reply,
)
from kleidos_meter.panel.http import (
    Api,
    ApiResponse,
    PanelServer,
    errors_json,
    relay_unit,
    report_json,
    status_for,
    typed_fields,
)
from kleidos_meter.panel.live import LiveHub
from kleidos_meter.panel.recorder import Recorder
from kleidos_meter.panel.schema import SCHEMA, get_refusal, resolve_unit
from kleidos_meter.panel.sequences import SequenceRunner
from kleidos_meter.panel.serve import Session, main
from kleidos_meter.panel.settings import Calibration, OverrideStore
from kleidos_meter.panel.static import Page
from kleidos_meter.protocol import ErrorEntry, split_units
from kleidos_meter.sim import SimulatedMeter

#: Real seconds a test waits for anything before it fails instead of hanging.
WAIT = 10.0

#: The header every change carries.
CHANGE = {"X-Kleidos-Meter": "1"}


@dataclass
class Answer:
    """A response as the client saw it."""

    status: int
    headers: dict[str, str]
    body: Any
    raw: bytes = b""


@dataclass
class Panel:
    """A served panel on the simulator, and what its executor wrote for callers."""

    server: PanelServer
    api: Api
    executor: Executor
    sim: SimulatedMeter
    live: LiveHub
    recorder: Recorder
    clock: FakeClock
    calibration: Calibration
    sequences: SequenceRunner
    sent: list[MessageInfo] = field(default_factory=list)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | str | dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        host: str | None = "default",
        connection: http.client.HTTPConnection | None = None,
    ) -> Answer:
        """Send one request with exactly the headers given (and a Host unless told not to)."""
        conn = connection or http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=WAIT)
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host is not None:
            conn.putheader("Host", f"127.0.0.1:{self.server.port}" if host == "default" else host)
        data = (
            json.dumps(body).encode()
            if isinstance(body, dict | list)
            else body.encode()
            if isinstance(body, str)
            else body
        )
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if data is not None:
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data)
        response = conn.getresponse()
        raw = response.read()
        headers_seen = {k.lower(): v for k, v in response.getheaders()}
        is_json = headers_seen.get("content-type", "").startswith("application/json")
        answer = Answer(
            response.status, headers_seen, json.loads(raw) if raw and is_json else None, raw
        )
        if connection is None:
            conn.close()
        return answer

    def get(self, path: str, **kwargs: Any) -> Answer:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: Any = b"", **kwargs: Any) -> Answer:
        headers = {**CHANGE, **kwargs.pop("headers", {})}
        return self.request("POST", path, body, headers=headers, **kwargs)


def until(condition: Callable[[], bool], what: str) -> None:
    """Wait in real time until *condition* holds."""
    deadline = time.monotonic() + WAIT
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.002)


@contextmanager
def serving_panel(
    *,
    remote: bool = False,
    bind: str = "127.0.0.1",
    data_dir: Path | None = None,
    live_options: dict[str, Any] | None = None,
    sim_options: dict[str, Any] | None = None,
    registry: Path | None = None,
    today: dt.date = dt.date(2026, 11, 22),
    page: Page | None = None,
) -> Iterator[Panel]:
    """A panel on port 0 over the clock-driven simulator: live events, recorder, sequences.

    *registry* is the calibration registry the shunt is judged against
    (none by default), on *today*; the overrides go in a scratch directory.
    *page* is the page served at ``/`` (none by default: those paths are the API's).
    """
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep, **(sim_options or {}))
    executor = Executor(sim, clock=clock)
    session = Session(executor)
    live = LiveHub(**(live_options or {}))
    executor.add_listener(live.on_event)
    executor.on_sent(live.on_sent)
    with tempfile.TemporaryDirectory() as scratch:
        recorder = Recorder(session, data_dir or Path(scratch) / "runs", live=live, clock=clock)
        calibration = Calibration(
            executor, registry, OverrideStore(Path(scratch) / "overrides"), today=lambda: today
        )
        sequences = SequenceRunner(
            executor, recorder, calibration=calibration, live=live, clock=clock, interval=0.002
        )
        executor.start()
        live.start()
        server: PanelServer | None = None
        try:
            executor.wait_ready(WAIT)
            session.open()
            api = Api(
                session,
                remote=remote,
                live=live,
                recorder=recorder,
                calibration=calibration,
                sequences=sequences,
                page=page,
            )
            server = PanelServer(api, bind, 0)
            server.start()
            panel = Panel(server, api, executor, sim, live, recorder, clock, calibration, sequences)
            executor.on_sent(panel.sent.append)
            yield panel
        finally:
            if server is not None:
                server.stop()
            sequences.close()
            recorder.close()
            live.close()
            executor.close()


@pytest.fixture
def panel() -> Iterator[Panel]:
    with serving_panel() as served:
        yield served


def no_cors(answer: Answer) -> None:
    assert not [name for name in answer.headers if name.startswith("access-control-")]


# -- every route -------------------------------------------------------------------------


def test_every_route_of_the_schema_has_a_handler_and_no_handler_is_undeclared(
    panel: Panel,
) -> None:
    assert set(panel.api.handlers) == {(route.method, route.path) for route in SCHEMA.routes}


def test_the_schema_is_served_as_declared(panel: Panel) -> None:
    answer = panel.get("/api/schema")
    assert answer.status == 200
    assert answer.body == json.loads(json.dumps(SCHEMA.to_json()))
    assert answer.headers["content-type"] == "application/json; charset=utf-8"
    assert answer.headers["cache-control"] == "no-store"
    assert answer.headers["x-content-type-options"] == "nosniff"
    assert answer.headers["content-security-policy"].startswith("default-src 'none'")
    no_cors(answer)
    assert panel.sent == []


def test_the_status_is_what_the_polls_keep_and_sends_nothing(panel: Panel) -> None:
    until(lambda: panel.executor.status is not None, "the first status poll")
    answer = panel.get("/api/status")
    assert answer.status == 200
    body = answer.body
    assert body["link"] == {"up": True, "down": False}
    assert body["polling"] is True and body["held"] is None and body["reboots"] == 0
    assert body["instrument"]["serial"] == SIM_SERIAL
    assert body["instrument"]["idn"].startswith("KLEIDOS,KLEIDOS-METER,")
    assert body["instrument"]["elf"] == "5e1f0ba47c2291d3"
    assert body["status"]["sink"] == "idle" and isinstance(body["status"]["period_us"], int)
    assert isinstance(body["health"]["uptime_us"], int)
    assert body["foreign_window"] is None
    assert panel.sent == []


def test_a_window_serve_found_open_is_in_the_status() -> None:
    with serving_panel() as panel:
        session = panel.api.session
        # A poll finding the line idle clears it: no poll runs, none is being routed
        # (the thread hands each event to a copy of the listeners), then none is heard.
        panel.executor.polling = False
        until(lambda: panel.executor.idle, "the poll in flight to end")
        panel.executor.remove_listener(session._on_event)
        panel.executor.call("*OPC?", wait=WAIT)  # the thread is past any event it was handing out
        panel.api.session.foreign_window = {"sink": "stream", "samples": "12"}
        answer = panel.get("/api/status")
        assert answer.body["foreign_window"] == {"sink": "stream", "samples": 12}


def test_health_carries_the_heap_counts_and_their_new_keys(panel: Panel) -> None:
    answer = panel.get("/api/health")
    assert answer.status == 200
    assert set(answer.body) == {"health", "heap", "watchdog", "info", "calibration", "local_elf"}
    assert answer.body["local_elf"]["path"] is None and answer.body["local_elf"]["matches"] is None
    heap = answer.body["heap"]
    assert heap["allocs_since_main"] == 0 and heap["frees_since_main"] == 0
    assert heap["pre_main_allocs"] > 0
    assert answer.body["health"]["reset"] == "poweron"
    assert answer.body["info"]["fw"] == "0.18.0"  # a version stays text
    assert answer.body["calibration"]["cal_tag"] is None  # "-": no value yet
    assert answer.body["watchdog"]["timeout_s"] == 5


def test_a_query_reads_without_acting_and_answers_its_fields(panel: Panel) -> None:
    message = "SYST:STAT?;:SENS:PROF?;:SENS:CURR:RANG:IND?;REP?"
    answer = panel.get("/api/query?q=" + quote(message))
    assert answer.status == 200
    assert answer.body["units"][1:3] == ["TRACE", "0"]
    assert answer.body["fields"][0]["sink"] == "idle"
    assert answer.body["fields"][3]["range"] == 0  # REP? continued the range's path
    assert answer.body["errors"] == []
    assert answer.body["message"] == message


def test_a_query_the_instrument_refuses_maps_its_error(panel: Panel) -> None:
    answer = panel.get("/api/query?q=" + quote("SENS:CURR:SHUN:RANG2?"))
    assert answer.status == 400
    assert answer.body["errors"][0]["code"] == -114


@pytest.mark.parametrize(
    "message",
    [
        "ROUT:LEAD OPEN,FORC",
        "SYST:STAT?;:ROUT:LEAD OPEN,FORC",
        "MEAS:CURR?",
        "*TST?",
        "*ESR?",
        "TRAC:DATA?",
        "SYST:BOGUS?",
        'SYST:STAT?;:TRAC:MARK "a;b"',
    ],
)
def test_a_get_that_would_mutate_is_refused_and_sends_nothing(panel: Panel, message: str) -> None:
    answer = panel.get("/api/query?q=" + quote(message))
    assert answer.status == 400
    assert answer.body["error"] == "get_mutates"
    assert panel.sent == []


def test_a_quote_left_open_is_refused_unsent_on_both_routes(panel: Panel) -> None:
    """Its appended error query would be quoted: no answer would ever match it (P2)."""
    answer = panel.get("/api/query?q=" + quote('SYST:STAT? "x'))
    assert answer.status == 400 and answer.body["error"] == "refused_unsent", answer.body
    raw = panel.post("/api/scpi", 'SYST:STAT?\nSYST:STAT? "x')
    assert raw.status == 400 and raw.body["error"] == "refused_unsent", raw.body
    assert panel.sent == []


def test_a_query_route_needs_one_message(panel: Panel) -> None:
    assert panel.get("/api/query").body["error"] == "bad_query"
    assert panel.get("/api/query?q=SYST:STAT%3F&q=FORM%3F").body["error"] == "bad_query"
    long = panel.get("/api/query?q=" + quote("SYST:STAT?" + " " * 250))
    assert long.status == 400 and long.body["error"] == "refused_unsent"
    assert panel.sent == []


def test_every_get_route_sends_only_queries_that_read_without_acting(panel: Panel) -> None:
    panel.recorder.start("STAT", seconds=0.2)  # a run for the run routes
    until(lambda: panel.recorder.state == "idle", "the run's end")
    run = panel.recorder.runs()[0]["id"]
    panel.sent.clear()
    for route in SCHEMA.routes:
        if route.method == "GET" and route.path != "/api/events":  # a stream: test_panel_live
            target = route.path.replace("{run}", run)
            target += "?q=SYST%3AINF%3F" if route.path == "/api/query" else ""
            if route.path == "/api/settings/shunt/preview":
                target += "?range=1&ohms=0.1002&tolerance_ppm=1000"
            assert panel.get(target).status == 200, route.path
    assert panel.sent
    for info in panel.sent:
        path = ""
        for unit in (u for u in split_units(info.message) if u.strip()):
            resolved, path = resolve_unit(unit, path)
            assert get_refusal(resolved) is None, unit


def test_raw_scpi_answers_each_message_with_its_errors(panel: Panel) -> None:
    answer = panel.post("/api/scpi", "SENS:PROF STAT;:SENS:PROF?\r\n\nSYST:BOGUS\n")
    assert answer.status == 200
    first, second = answer.body["replies"]
    assert first["message"] == "SENS:PROF STAT;:SENS:PROF?"
    assert first["units"] == ["STATE"] and first["errors"] == []
    assert isinstance(first["elapsed_s"], float)
    assert second["errors"] == [{"code": -113, "text": "Undefined header;SYST:BOGUS"}]
    assert [info.message for info in panel.sent] == ["SENS:PROF STAT;:SENS:PROF?", "SYST:BOGUS"]


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ("", "bad_body"),
        ("\n \n", "bad_body"),
        ("SYST:STAT?\nSENS:PROF " + "X" * 260, "refused_unsent"),
        ("SYST:STAT?\nTRAC:DATA? RAW", "dump_refused"),
        ("SYST:STAT?\x0cROUT:LEAD OPEN,FORC", "refused_unsent"),  # a form feed ends nothing
    ],
)
def test_raw_scpi_refuses_unsent_what_it_cannot_send(panel: Panel, body: str, error: str) -> None:
    answer = panel.post("/api/scpi", body)
    assert answer.status == 400 and answer.body["error"] == error
    assert panel.sent == []


def test_raw_scpi_refuses_a_body_that_is_not_ascii(panel: Panel) -> None:
    answer = panel.post("/api/scpi", "SENS:PROF µ".encode())
    assert answer.status == 400 and answer.body["detail"] == "the body is not ASCII (byte 10)"


def test_settings_are_read_as_the_instrument_answers_them(panel: Panel) -> None:
    answer = panel.get("/api/settings")
    assert answer.status == 200
    settings = answer.body["settings"]
    assert list(settings) == [s.name for s in SCHEMA.settings]
    assert settings["profile"] == "TRACE" and settings["i2c_hz"] == "400000"
    assert settings["format"] == "TEXT" and settings["threshold"] == "OFF"


def test_a_written_setting_is_read_back_from_the_instrument(panel: Panel) -> None:
    answer = panel.post("/api/settings", {"profile": "state", "sample_count": 250})
    assert answer.status == 200
    assert answer.body["written"] == ["SENS:PROF STAT", "SAMP:COUN 250"]
    settings = answer.body["settings"]
    assert settings["profile"] == "STATE" and settings["sample_count"] == "250"
    assert settings["channels"] == panel.get("/api/settings").body["settings"]["channels"]
    assert list(settings) == [  # the profile reset the channels, and has its own averaging
        "profile",
        "channels",
        "sample_count",
        "average_count",
        "average_aperture",
        "average",
    ]
    assert panel.api.session.shaping.dirty >= {"profile", "sample_count"}  # serve puts them back


def test_the_averaging_is_written_on_the_profile_asked_once_that_is_live(panel: Panel) -> None:
    order = {"average": True, "average_aperture": "0.00028", "average_count": 64, "profile": "STAT"}
    answer = panel.post("/api/settings", order)
    assert answer.status == 200, answer.body
    assert answer.body["written"] == [
        "SENS:PROF STAT",
        "SENS:AVER:COUN 64",
        "SENS:CURR:APER 0.00028",
        "SENS:AVER ON",
    ]
    settings = answer.body["settings"]
    assert (settings["average_count"], settings["average_aperture"]) == ("64", "0.00028")
    assert settings["average"] == "1" and settings["profile"] == "STATE"
    # What STATE had is read between the two, so that serve puts it back.
    sent = [info.message for info in panel.sent]
    assert sent[0] == "SENS:PROF STAT" and sent[1].startswith("SENS:PROF?;:SENS:AVER:COUN?")
    assert panel.api.session.shaping._found["STATE"]["average"] == "0"
    assert panel.api.session.close()[1]
    assert not panel.sim.averaging["STATE"].on and panel.sim.profile == "TRACE"


def test_an_averaging_the_instrument_refuses_answers_its_reason(panel: Panel) -> None:
    panel.post("/api/settings", {"i2c_hz": 1000000, "profile": "BURS"})
    answer = panel.post("/api/settings", {"average": True})
    assert answer.status == 409 and answer.body["errors"][0]["code"] == -221
    assert "BURSt" in answer.body["errors"][0]["text"]
    assert answer.body["settings"] == {"average": "0"}


def test_no_averaging_is_written_once_the_profile_asked_is_refused(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    call, written = panel.executor.call, []

    def refusing(message: str, **options: Any) -> Reply:
        written.append(message)
        if message.startswith("SENS:PROF "):
            return Reply(message, [], [ErrorEntry(-221, "Settings conflict;not now")])
        return call(message, **options)

    monkeypatch.setattr(panel.executor, "call", refusing)
    answer = panel.post("/api/settings", {"profile": "STATe", "sample_count": 5, "average": True})
    assert answer.status == 409
    assert answer.body["written"] == ["SENS:PROF STAT", "SAMP:COUN 5"]
    assert [entry["code"] for entry in answer.body["errors"]] == [-221]
    assert not any("SENS:AVER " in message for message in written)
    assert (answer.body["settings"]["profile"], answer.body["settings"]["average"]) == (
        "TRACE",
        "0",
    )
    assert not panel.sim.averaging["TRACE"].on  # the live profile's, which nobody asked to change


@pytest.mark.parametrize(
    "body",
    [{"average": "maybe"}, {"average_count": 3}, {"average_aperture": "5;:ROUT:LEAD OPEN"}],
)
def test_an_averaging_of_the_wrong_form_is_refused_unsent(panel: Panel, body: object) -> None:
    answer = panel.post("/api/settings", body)
    assert (answer.status, answer.body["error"]) == (400, "bad_value")
    assert panel.sent == []


@pytest.mark.parametrize("message", ["SENS:AVER ON", "*RST"])
def test_raw_scpi_that_changes_the_averaging_reads_what_the_profile_had_first(
    panel: Panel, message: str
) -> None:
    with panel.sim._lock:
        panel.sim.averaging["STATE"] = dataclasses.replace(
            panel.sim.averaging["STATE"], on=True, count=16
        )
    answer = panel.post("/api/scpi", f"SENS:PROF STAT\n{message}")
    assert [reply["errors"] for reply in answer.body["replies"]] == [[], []]
    assert panel.api.session.shaping._found["STATE"]["average_count"] == "16"


def test_a_setting_the_instrument_refuses_answers_409_and_its_reading(panel: Panel) -> None:
    answer = panel.post("/api/settings", {"sample_count": "0"})
    assert answer.status == 409
    assert answer.body["errors"][0]["code"] == -222
    assert answer.body["settings"] == {"sample_count": "100"}
    panel.post("/api/scpi", "SENS:PROF STAT;:INIT")
    under = panel.post("/api/settings", {"range": 1})
    assert under.status == 409 and under.body["errors"][0]["code"] == -221  # flag R
    assert panel.post("/api/abort").status == 200


@pytest.mark.parametrize(
    ("body", "error"),
    [
        ({}, "bad_body"),
        (b"", "bad_body"),
        ({"profile": "TRACER"}, "bad_value"),
        ({"profile": "STAT;:ROUT:LEAD OPEN,FORC"}, "bad_value"),
        ({"format": "CSV"}, "read_only"),
        ({"bogus": 1}, "unknown_setting"),
        ([1, 2], "bad_body"),
        ("{not json", "bad_body"),
        (b"\xff", "bad_body"),
    ],
)
def test_a_setting_of_the_wrong_form_is_refused_unsent(
    panel: Panel, body: object, error: str
) -> None:
    answer = panel.post("/api/settings", body)
    assert answer.status == 400 and answer.body["error"] == error
    assert panel.sent == []


def test_the_relays_are_read_as_the_instrument_reports_them(panel: Panel) -> None:
    answer = panel.get("/api/relays")
    assert answer.status == 200
    assert answer.body["state"]["path"] == "shunt"
    assert answer.body["state"]["hold_s"] is None
    assert answer.body["result"]["moved"] is None  # no order since boot


def test_a_relay_order_carries_only_the_flags_the_caller_set(panel: Panel) -> None:
    refused = panel.post("/api/relays/lead", {"state": "open"})
    assert refused.status == 409
    assert refused.body["order"] == "ROUT:LEAD OPEN"  # no FORCe added
    assert refused.body["errors"][0]["code"] == 109
    assert refused.body["state"]["path"] == "shunt"
    forced = panel.post("/api/relays/lead", {"state": "OPEN", "force": True, "live": False})
    assert forced.status == 200 and forced.body["order"] == "ROUT:LEAD OPEN,FORC"
    assert forced.body["state"]["path"] == "none" and forced.body["state"]["hold_s"] == 120
    closed = panel.post("/api/relays/lead", {"state": "CLOSe"})
    assert closed.status == 200 and closed.body["state"]["path"] == "shunt"
    cycle = panel.post("/api/relays/lead_cycle", {"ms": 100, "force": True})
    assert cycle.status == 200 and cycle.body["order"] == "ROUT:LEAD:CYCL 100,FORC"
    spare = panel.post("/api/relays/relay3", {"state": "ON", "live": True})
    assert spare.status == 409 and spare.body["order"] == "ROUT:REL3 ON,LIVE"
    assert spare.body["errors"][0]["code"] == -241


def test_the_null_path_writes_live_before_force(panel: Panel) -> None:
    answer = panel.post("/api/relays/nullpath", {"state": "ON", "force": True, "live": True})
    assert answer.body["order"] == "ROUT:NULL ON,LIVE,FORC"
    lead = SCHEMA.relay("lead")
    assert lead is not None
    assert relay_unit(lead, {"state": "OPEN", "live": True, "force": True}) == (
        "ROUT:LEAD OPEN,FORC,LIVE"
    )


@pytest.mark.parametrize(
    ("path", "body", "error"),
    [
        ("/api/relays/lead", {}, "bad_order"),
        ("/api/relays/lead", {"state": "SHUT"}, "bad_order"),
        ("/api/relays/lead", {"state": "OPEN", "force": "yes"}, "bad_order"),
        ("/api/relays/lead", {"state": "OPEN", "hold": True}, "bad_order"),
        ("/api/relays/lead_cycle", {"ms": "100;*RST", "force": True}, "bad_order"),
        ("/api/relays/relay3", {"state": "ON", "force": True}, "bad_order"),
    ],
)
def test_a_relay_order_of_the_wrong_form_is_refused_unsent(
    panel: Panel, path: str, body: dict[str, Any], error: str
) -> None:
    answer = panel.post(path, body)
    assert answer.status == 400 and answer.body["error"] == error
    assert panel.sent == []


def test_an_unknown_relay_is_not_found(panel: Panel) -> None:
    answer = panel.post("/api/relays/k9", {"state": "ON"})
    assert answer.status == 404 and answer.body["error"] == "unknown_relay"
    assert "lead, lead_cycle, bypass" in answer.body["detail"]


def test_abort_with_nothing_open_answers_the_instruments_code(panel: Panel) -> None:
    answer = panel.post("/api/abort")
    assert answer.status == 409
    assert answer.body["sent"] is True and answer.body["errors"][0]["code"] == 111


def test_abort_goes_ahead_of_an_acquisition_and_is_always_served(panel: Panel) -> None:
    assert panel.post("/api/settings", {"sample_count": 500_000}).status == 200  # 576 s
    replies: list[Answer] = []
    acquiring = threading.Thread(
        target=lambda: replies.append(panel.post("/api/scpi", "SENS:PROF STAT;:MEAS:CURR?"))
    )
    acquiring.start()
    until(
        lambda: (info := panel.executor.in_flight) is not None and info.acquisition,
        "the acquisition to hold the console",
    )
    slots = panel.server._server.slots
    taken = 0
    while slots.acquire(blocking=False):  # every request slot taken
        taken += 1
    assert panel.get("/api/settings").status == 503  # the next request is turned away
    answer = panel.post("/api/abort")  # but an abort is served
    for _ in range(taken):
        slots.release()
    acquiring.join(WAIT)
    assert answer.status == 200 and answer.body["sent"] is True
    assert replies[0].body["replies"][0]["errors"][0]["code"] == -230


def test_an_abort_that_found_the_acquisition_ended_sends_nothing(panel: Panel) -> None:
    class Ended:
        def result(self) -> Any:
            return Reply("ABOR", [], [], sent=False)

    panel.api.executor = type("Fake", (), {"abort": lambda self: Ended()})()
    answer = panel.post("/api/abort")
    assert answer.status == 200 and answer.body == {
        "sent": False,
        "detail": "the acquisition had ended",
        "sequence": None,  # none ran
    }


# -- the guards --------------------------------------------------------------------------


def test_a_change_without_the_header_is_refused_and_sends_nothing(panel: Panel) -> None:
    for path, body in (
        ("/api/scpi", "ROUT:LEAD OPEN,FORC"),
        ("/api/settings", {"profile": "STAT"}),
        ("/api/relays/lead", {"state": "OPEN", "force": True}),
        ("/api/abort", b""),
    ):
        answer = panel.request("POST", path, body)
        assert answer.status == 403 and answer.body["error"] == "header_missing", path
        no_cors(answer)
    assert panel.sent == []


def test_a_foreign_host_is_misdirected_on_every_route(panel: Panel) -> None:
    for host in ("evil.example:8040", "192.168.0.10:8040", None):
        get = panel.get("/api/schema", host=host)
        assert get.status == 421 and get.body["error"] == "misdirected"
        post = panel.post("/api/scpi", "ROUT:LEAD OPEN,FORC", host=host)
        assert post.status == 421
        assert panel.get("/nowhere", host=host).status == 421  # before anything else
    assert panel.sent == []
    assert panel.get("/api/schema", host=f"localhost:{panel.server.port}").status == 200


def test_a_change_from_another_origin_is_refused(panel: Panel) -> None:
    answer = panel.post("/api/abort", headers={"Origin": "https://evil.example"})
    assert answer.status == 403 and answer.body["error"] == "foreign_origin"
    assert panel.sent == []


def test_a_get_on_a_route_that_changes_is_not_allowed(panel: Panel) -> None:
    for path in ("/api/scpi", "/api/abort", "/api/relays/lead"):
        answer = panel.get(path)
        assert answer.status == 405 and answer.headers["allow"] == "POST", path
        assert answer.body["detail"] == "a GET never changes the instrument"
    assert panel.sent == []


def test_a_preflight_is_never_approved(panel: Panel) -> None:
    answer = panel.request(
        "OPTIONS",
        "/api/scpi",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-kleidos-meter",
        },
    )
    assert answer.status == 405
    assert answer.body["detail"] == "/api/scpi takes POST"
    no_cors(answer)


def test_other_methods_are_not_allowed(panel: Panel) -> None:
    for method in ("PUT", "DELETE", "PATCH"):
        answer = panel.request(method, "/api/settings", headers=CHANGE)
        assert answer.status == 405 and answer.headers["allow"] == "GET, POST", method
    head = panel.request("HEAD", "/api/schema")
    assert head.status == 405 and head.raw == b""


def test_an_unknown_path_is_not_found(panel: Panel) -> None:
    answer = panel.get("/api/nowhere")
    assert answer.status == 404 and answer.body["error"] == "not_found"
    assert panel.get("/").status == 404  # an API built without a page


def test_a_held_line_answers_423_on_every_route_that_talks_to_the_instrument(
    panel: Panel,
) -> None:
    panel.api.hold("a passthrough client (127.0.0.1:50123)")
    assert panel.api.holder == "a passthrough client (127.0.0.1:50123)"
    held = panel.post("/api/scpi", "SYST:STAT?")
    assert held.status == 423
    assert held.body == {
        "error": "held",
        "holder": "a passthrough client (127.0.0.1:50123)",
        "detail": "the line is held by a passthrough client (127.0.0.1:50123)",
    }
    for path in ("/api/settings", "/api/health", "/api/relays", "/api/query?q=FORM%3F"):
        assert panel.get(path).status == 423, path
    assert panel.post("/api/abort").status == 423
    assert panel.get("/api/schema").status == 200
    status = panel.get("/api/status")
    assert status.status == 200 and status.body["held"].startswith("a passthrough client")
    assert panel.sent == []
    panel.api.release()
    assert panel.api.holder is None
    assert panel.post("/api/scpi", "SYST:STAT?").status == 200


def test_a_body_beyond_the_limit_or_of_unknown_length_is_refused(panel: Panel) -> None:
    big = panel.post("/api/scpi", "X" * (panel_http.MAX_BODY + 1))
    assert big.status == 413 and big.body["error"] == "too_large"
    bad = panel.request("POST", "/api/scpi", headers={**CHANGE, "Content-Length": "-1"})
    assert bad.status == 413
    chunked = panel.request("POST", "/api/scpi", headers={**CHANGE, "Transfer-Encoding": "chunked"})
    assert chunked.status == 411 and chunked.body["error"] == "length_required"
    assert panel.sent == []


def test_a_connection_is_kept_for_the_next_request(panel: Panel) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", panel.server.port, timeout=WAIT)
    try:
        for _ in range(3):
            assert panel.get("/api/schema", connection=conn).status == 200
    finally:
        conn.close()


def test_a_routes_own_failure_is_answered_500_and_the_server_kept(panel: Panel) -> None:
    def broken(**_: Any) -> ApiResponse:
        raise RuntimeError("a bug")

    panel.api.handlers[("GET", "/api/relays")] = broken
    answer = panel.get("/api/relays")
    assert answer.status == 500 and answer.body["error"] == "internal"
    assert panel.get("/api/schema").status == 200


# -- the executor's failures ---------------------------------------------------------------


class Failing:
    """An executor whose every call raises *error*."""

    status = None
    in_flight = None

    def __init__(self, error: Exception) -> None:
        self.error = error

    def call(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error

    def query_all(self, *args: Any, **kwargs: Any) -> Any:
        raise self.error


@pytest.mark.parametrize(
    ("error", "status", "kind", "retry"),
    [
        (MessageRefused("too long"), 400, "refused_unsent", None),
        (ExecutorBusy("64 waiting"), 503, "busy", "1"),
        (LinkDown("not answering"), 503, "link_down", "2"),
        (ExecutorClosed("stopped"), 503, "closing", None),
        (MeterTimeout("no answer"), 504, "timeout", None),
        (MeterError("refused", [ErrorEntry(-221, "Settings conflict")]), 409, "instrument", None),
        (MeterError("refused", [ErrorEntry(-113, "Undefined header")]), 400, "instrument", None),
        (MeterError("2 queries answered 1 units"), 502, "instrument", None),
    ],
)
def test_the_executors_failures_map_to_statuses(
    panel: Panel, error: Exception, status: int, kind: str, retry: str | None
) -> None:
    panel.api.executor = Failing(error)  # type: ignore[assignment]  # a fake executor
    answer = panel.get("/api/settings")
    assert answer.status == status and answer.body["error"] == kind
    assert answer.headers.get("retry-after") == retry


class FailingAfter:
    """An executor that runs its first *ok* calls on *inner* and raises *error* on the next."""

    status = None
    in_flight = None

    def __init__(self, inner: Executor, ok: int, error: Exception) -> None:
        self.inner, self.ok, self.error = inner, ok, error
        self.calls: list[str] = []

    def call(self, message: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(message)
        if len(self.calls) > self.ok:
            raise self.error
        return self.inner.call(message, *args, **kwargs)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (MeterTimeout("no answer within 5 s"), "timeout"),
        (ExecutorBusy("64 waiting"), "busy"),
        (LinkDown("not answering"), "link_down"),
        (ExecutorClosed("stopped"), "closing"),
        (LineHeld("handed over"), "held"),
        (MeterError("2 queries answered 1 units"), "instrument"),
    ],
)
def test_raw_scpi_answers_the_lines_that_ran_when_a_later_one_fails(
    panel: Panel, error: Exception, kind: str
) -> None:
    """The lines before a failure changed the instrument: the answer says so, and asks
    for no retry that would run them twice (P4)."""
    fake = FailingAfter(panel.executor, 1, error)
    panel.api.executor = fake  # type: ignore[assignment]  # a fake executor
    answer = panel.post("/api/scpi", "SENS:PROF STAT\nSYST:STAT?\nSENS:PROF?")
    assert answer.status == 200, answer.body
    assert "retry-after" not in answer.headers
    assert [reply["message"] for reply in answer.body["replies"]] == ["SENS:PROF STAT"]
    assert answer.body["failed"]["message"] == "SYST:STAT?"
    assert answer.body["failed"]["error"] == kind and answer.body["failed"]["detail"]
    assert answer.body["unsent"] == ["SENS:PROF?"]
    assert fake.calls == ["SENS:PROF STAT", "SYST:STAT?"]


def test_raw_scpi_whose_first_line_fails_answers_as_before(panel: Panel) -> None:
    panel.api.executor = FailingAfter(panel.executor, 0, ExecutorBusy("64 waiting"))  # type: ignore[assignment]  # a fake
    answer = panel.post("/api/scpi", "SENS:PROF STAT\nSYST:STAT?")
    assert answer.status == 503 and answer.headers.get("retry-after") == "1"


# -- the JSON ------------------------------------------------------------------------------


def test_a_report_keeps_its_keys_and_its_integer_units() -> None:
    assert report_json(
        "i_na=88066019 vbus_uv=-12 n=0 flags=0x0 mean_na=88700813.464 q=cal hold_s=-"
        " elf=0123456789012345 serial=KM-1 fw=0.18.0 big=9007199254740993 lead=007"
    ) == {
        "i_na": 88066019,
        "vbus_uv": -12,
        "n": 0,
        "flags": "0x0",
        "mean_na": "88700813.464",  # thousandths stay the device's text
        "q": "cal",
        "hold_s": None,
        "elf": "0123456789012345",
        "serial": "KM-1",
        "fw": "0.18.0",
        "big": "9007199254740993",  # beyond what a browser holds exactly
        "lead": "007",
    }
    assert typed_fields({"elf": "1234567890123456"}) == {"elf": "1234567890123456"}
    assert typed_fields({"elf": "1234"}, text_keys=()) == {"elf": 1234}


def test_errors_map_to_400_for_commands_and_409_for_the_rest() -> None:
    assert status_for([]) == 200
    assert status_for([0]) == 200
    assert status_for([-113]) == 400
    assert status_for([-199, -221]) == 400
    assert status_for([-221]) == 409
    assert status_for([-240]) == 409
    assert status_for([-363]) == 409
    assert status_for([109]) == 409
    assert status_for([-100]) == 400 and status_for([-200]) == 409
    assert errors_json([ErrorEntry(-113, 'Undefined header;"X"')]) == [
        {"code": -113, "text": 'Undefined header;"X"'}
    ]


# -- serve ---------------------------------------------------------------------------------


@contextmanager
def running_serve(
    argv: list[str], **kwargs: Any
) -> Iterator[tuple[io.StringIO, io.StringIO, list[int]]]:
    """``serve`` in a thread until the block ends: its output, its errors, its status."""
    stop = threading.Event()
    out, err = io.StringIO(), io.StringIO()
    status: list[int] = []
    thread = threading.Thread(
        target=lambda: status.append(main(argv, stop=stop, out=out, err=err, **kwargs))
    )
    thread.start()
    try:
        until(
            lambda: "holding the port" in out.getvalue() or bool(status), "serve to hold the port"
        )
        yield out, err, status
    finally:
        stop.set()
        thread.join(WAIT)


def panel_port(out: io.StringIO) -> int:
    """The port serve printed its panel at."""
    match = re.search(r"panel API at http://[^:]+:(\d+)/api/", out.getvalue())
    assert match is not None, out.getvalue()
    return int(match.group(1))


def fetch(port: int, path: str, host: str | None = None) -> int:
    """GET *path* from 127.0.0.1:*port*: the status."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
    try:
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{port}")
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def test_serve_serves_the_api_and_stops_serving_it_on_the_way_out() -> None:
    with running_serve(["--sim", "--http", "0"]) as (out, _, status):
        port = panel_port(out)
        assert fetch(port, "/api/schema") == 200
        assert status == []
    assert status == [0]
    with pytest.raises(OSError):
        fetch(port, "/api/schema")


def test_serve_refuses_an_address_beyond_the_loopback_without_allow_remote(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--sim", "--bind", "0.0.0.0", "--http", "0"])  # noqa: S104  refused, never bound
    assert raised.value.code == 2
    assert "add --allow-remote" in capsys.readouterr().err


def test_serve_beyond_the_loopback_warns_that_the_panel_is_unauthenticated() -> None:
    # S104: the warning under test is the one this bind prints.
    with running_serve(["--sim", "--bind", "0.0.0.0", "--http", "0", "--allow-remote"]) as (  # noqa: S104
        out,
        err,
        _,
    ):
        assert err.getvalue().startswith("# warning: the panel is unauthenticated;")
        port = panel_port(out)
        assert fetch(port, "/api/schema", host=f"192.0.2.7:{port}") == 200  # an IP literal
        assert fetch(port, "/api/schema", host=f"evil.example:{port}") == 421  # never a name


def test_serve_exits_1_when_its_address_is_taken() -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        err = io.StringIO()
        assert main(["--sim", "--http", str(port)], out=io.StringIO(), err=err) == 1
    assert err.getvalue().startswith(f"# cannot listen on 127.0.0.1 port {port}: ")


def test_the_panel_listens_on_the_ipv6_loopback() -> None:
    try:
        probe = socket.socket(socket.AF_INET6)
        probe.bind(("::1", 0))
        probe.close()
    except OSError:
        pytest.skip("no IPv6 loopback on this host")
    with serving_panel(bind="::1") as panel:
        assert panel.server.url == f"http://[::1]:{panel.server.port}/"
        conn = http.client.HTTPConnection("::1", panel.server.port, timeout=WAIT)
        try:
            conn.request("GET", "/api/schema")
            assert conn.getresponse().status == 200
        finally:
            conn.close()


def test_serve_notes_the_panels_address_for_the_next_opener(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kleidos_meter.panel import lockinfo

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    def opener(port: str) -> SimulatedMeter:
        clock = FakeClock()
        return SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep)

    argv = ["--port", "/dev/cu.fake-meter", "--http", "0"]
    with running_serve(argv, open_transport=opener) as (out, _, _):
        note = json.loads(lockinfo.info_path("/dev/cu.fake-meter").read_text())
        assert note["panel"] == f"http://127.0.0.1:{panel_port(out)}/"


def quote(text: str) -> str:
    """*text* percent-encoded for a query string."""
    from urllib.parse import quote as url_quote

    return url_quote(text, safe="")


def test_a_failing_connection_is_logged_never_printed(
    panel: Panel, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    server = panel.server._server
    caplog.set_level(logging.DEBUG, logger="kleidos_meter.panel.http")
    for error in (ConnectionResetError("reset"), BrokenPipeError("pipe"), ValueError("bug")):
        try:
            raise error
        except Exception:  # noqa: BLE001  each of the three, as the server meets them
            server.handle_error(None, ("127.0.0.1", 50000))
    assert capsys.readouterr().err == ""
    levels = [(record.levelname, record.getMessage()) for record in caplog.records]
    assert levels == [
        ("DEBUG", "('127.0.0.1', 50000) went away: reset"),
        ("DEBUG", "('127.0.0.1', 50000) went away: pipe"),
        ("ERROR", "serving ('127.0.0.1', 50000)"),
    ]
