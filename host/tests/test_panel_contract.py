# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's HTTP API end to end: ``kleidos-meter serve --sim`` as a user runs it.

Every route of the schema has a contract here (:data:`CONTRACTS`), checked
against one serve on the clock-driven simulator, on the real clock, with its
passthrough open and a registry that files the simulator's calibration of
today: what the route answers, in which shape, and what it leaves the
instrument in. A route added to the schema without a contract fails
:func:`test_every_route_of_the_schema_has_a_contract`.

The same serve then holds the error mapping (each class of refusal and the
status it answers), the hold (``423`` on every route that talks to the
instrument while a passthrough client has the line, the routes that send
nothing still served) and the line coming back when the client leaves.
The passthrough's transparency is held by ``test_conformance.py``, which
runs the whole client suite through it.

The routes' unit tests (``test_panel_http.py`` and the others) drive the
same code on a fake clock; these drive serve's ``main()`` and the
simulator's real-time stream.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from conftest import SIM_SERIAL, build_registry
from kleidos_meter.panel.paths import shown_path
from kleidos_meter.panel.schema import SCHEMA
from panel_harness import EventReader, Served, serving, until
from test_registry import accepted, add_events


@dataclass
class Context:
    """The serve every contract runs against, and the runs made for the run routes.

    :ivar served: The serve.
    :ivar data_dir: Its data directory.
    :ivar stream_run: A streamed run's id (STATE, 1 s, one mark).
    :ivar ring_run: A ring run's id (TRACE, 1 s).
    """

    served: Served
    data_dir: Path
    stream_run: str = ""
    ring_run: str = ""
    seen: list[str] = field(default_factory=list)


@pytest.fixture(scope="module")
def context(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Context]:
    """One ``serve --sim`` for the module, with a run of each sink already recorded."""
    base = tmp_path_factory.mktemp("contract")
    registry = build_registry(base)
    add_events(
        registry, accepted(registry, f"{dt.datetime.now().astimezone().date().isoformat()}-01")
    )
    data_dir = base / "state" / "runs"
    argv = ["--sim", "--data-dir", str(data_dir), "--registry", str(registry)]
    with serving([*argv, "--scpi-passthrough", "0"]) as served:
        restored = served.post("/api/calibration/restore")
        assert restored.status == 200, restored.body
        ctx = Context(served, data_dir)
        ctx.stream_run = capture(served, {"profile": "STATE", "channels": ["CURR", "VOLT"]}, "m")
        ctx.ring_run = capture(served, {"profile": "TRACE", "seconds": 1})
        yield ctx
    assert served.status == [0], served.err.getvalue()


def capture(served: Served, order: dict[str, Any], mark: str | None = None) -> str:
    """Record one capture through the API: started, marked, stopped; its run id."""
    started = served.post("/api/capture/start", order)
    assert started.status == 200, started.body
    run: str = started.body["run"]
    if "seconds" not in order:
        until(lambda: served.get("/api/capture").body.get("samples", 0) > 100, "samples")
        if mark is not None:
            marked = served.post("/api/capture/mark", {"label": mark})
            assert marked.status == 200, marked.body
        stopped = served.post("/api/capture/stop")
        assert stopped.status == 200, stopped.body
    served.capture_idle()
    return run


def idle_on_the_shunt(served: Served) -> None:
    """The state every contract leaves: no sequence, no capture, the relays on the shunt."""
    assert served.get("/api/sequences").body["state"] == "idle"
    assert served.get("/api/capture").body["state"] == "idle"
    assert served.get("/api/relays").body["state"]["path"] == "shunt"


# -- one contract per route ---------------------------------------------------------------


def _schema(ctx: Context) -> None:
    answer = ctx.served.get("/api/schema")
    assert answer.status == 200
    body = answer.body
    assert body["schema"] == "kleidos.meter.panel/1"
    assert body["mutation_header"] == {"name": "X-Kleidos-Meter", "value": "1"}
    assert [(r["method"], r["path"]) for r in body["routes"]] == [
        (r.method, r.path) for r in SCHEMA.routes
    ]
    assert [s["name"] for s in body["sequences"]] == [s.name for s in SCHEMA.sequences]


def _status(ctx: Context) -> None:
    body = ctx.served.get("/api/status").body
    assert body["link"]["up"] is True and body["held"] is None
    assert body["passthrough"]["url"] == ctx.served.passthrough
    assert body["passthrough"]["state"] == "listening"
    assert {"running", "profile", "samples"} <= set(body["status"])


def _health(ctx: Context) -> None:
    answer = ctx.served.get("/api/health")
    assert answer.status == 200
    body = answer.body
    assert {"health", "heap", "watchdog", "info", "calibration", "local_elf"} <= set(body)
    heap = body["heap"]
    for key in ("allocs_after_init", "frees_after_init", "allocs_since_main", "frees_since_main"):
        assert heap[key] == 0, key
    assert body["info"]["serial"] == SIM_SERIAL
    assert body["watchdog"]["console"] == 1 and body["watchdog"]["sampler"] == 1


def _query(ctx: Context) -> None:
    answer = ctx.served.get("/api/query?" + urlencode({"q": "*IDN?;SENS:PROF:REP?"}))
    assert answer.status == 200, answer.body
    assert answer.body["units"][0] == f"KLEIDOS,KLEIDOS-METER,{SIM_SERIAL},0.18.0"
    assert set(answer.body["fields"][1]) == {
        "profile",
        "adc_config",
        "period_us",
        "vshct_us",
        "avg",
        "avg_state",
        "vbusct_us",
    }
    assert answer.body["errors"] == []


def _scpi(ctx: Context) -> None:
    answer = ctx.served.post("/api/scpi", "*IDN?\nSYST:VERS?\nKLEIDOS:NOTHING")
    assert answer.status == 200
    replies = answer.body["replies"]
    assert [r["message"] for r in replies] == ["*IDN?", "SYST:VERS?", "KLEIDOS:NOTHING"]
    assert replies[1]["units"] == ["1999.0"] and replies[1]["errors"] == []
    assert [e["code"] for e in replies[2]["errors"]] == [-113]


def _settings_get(ctx: Context) -> None:
    body = ctx.served.get("/api/settings").body
    assert set(body["settings"]) == {s.name for s in SCHEMA.settings}
    assert body["settings"]["format"] == "TEXT"


def _settings_post(ctx: Context) -> None:
    written = ctx.served.post("/api/settings", {"range": 1})
    assert written.status == 200, written.body
    assert written.body["written"] == ["SENS:CURR:RANG:IND 1"]
    assert written.body["settings"] == {"range": "1"}
    back = ctx.served.post("/api/settings", {"range": 0})
    assert back.body["settings"] == {"range": "0"}


def _shunt_preview(ctx: Context) -> None:
    query = {"range": 0, "uohm": 100010, "expanded_ppm": 200}
    answer = ctx.served.get("/api/settings/shunt/preview?" + urlencode(query))
    assert answer.status == 200, answer.body
    assert answer.body["unit"] == "SENS:CURR:SHUN:RANG0 0.100010"
    assert answer.body["uncertainty"]["k"] == 2
    assert answer.body["uncertainty"]["expanded_ppm"] == 200.0
    assert answer.body["registry"] is not None


def _shunt_post(ctx: Context) -> None:
    order = {"range": 0, "uohm": 100010, "expanded_ppm": 200, "confirm": True}
    answer = ctx.served.post("/api/settings/shunt", order)
    assert answer.status == 200, answer.body
    assert answer.body["written"] == "SENS:CURR:SHUN:RANG0 0.100010"
    verdict = ctx.served.get("/api/calibration").body["verdict"]
    assert verdict["status"] != "applied", "an override is not the registry's copy"
    assert ctx.served.post("/api/calibration/restore").status == 200


def _calibration_get(ctx: Context) -> None:
    body = ctx.served.get("/api/calibration").body
    assert body["verdict"]["status"] == "applied"
    assert body["verdict"]["serial"] == SIM_SERIAL


def _calibration_restore(ctx: Context) -> None:
    answer = ctx.served.post("/api/calibration/restore")
    assert answer.status == 200, answer.body
    assert answer.body["after"]["status"] == "applied"


def _sequences_get(ctx: Context) -> None:
    body = ctx.served.get("/api/sequences").body
    assert body["state"] == "idle"


def _sequences_run(ctx: Context) -> None:
    state = ctx.served.sequence("HEALTH")
    assert state["outcome"] == "done", state
    assert {"idn", "info", "health", "heap", "watchdog", "self_test", "scan"} <= set(
        state["results"]
    )


def _sequences_answer(ctx: Context) -> None:
    started = ctx.served.post("/api/sequences/run", {"name": "NULL_LEAD", "params": {"seconds": 1}})
    assert started.status == 200, started.body
    until(lambda: ctx.served.get("/api/sequences").body.get("prompt") is not None, "the question")
    prompt = ctx.served.get("/api/sequences").body["prompt"]
    assert prompt["choices"] == ["continue", "cancel"]
    answer = ctx.served.post("/api/sequences/answer", {"id": prompt["id"], "answer": "cancel"})
    assert answer.status == 200, answer.body
    until(lambda: ctx.served.get("/api/sequences").body["state"] == "idle", "the sequence's end")
    state = ctx.served.get("/api/sequences").body
    assert (state["outcome"], state["detail"]) == ("declined", "answered cancel")


def _sequences_abort(ctx: Context) -> None:
    started = ctx.served.post("/api/sequences/run", {"name": "STATE", "params": {"seconds": 60}})
    assert started.status == 200, started.body
    until(lambda: ctx.served.get("/api/capture").body["state"] == "running", "the capture")
    aborted = ctx.served.post("/api/sequences/abort")
    assert aborted.status == 200, aborted.body
    until(lambda: ctx.served.get("/api/sequences").body["state"] == "idle", "the sequence's end")
    assert ctx.served.get("/api/sequences").body["outcome"] == "aborted"
    ctx.served.capture_idle()


def _relays_get(ctx: Context) -> None:
    body = ctx.served.get("/api/relays").body
    assert body["state"]["path"] == "shunt" and body["result"]["path"] == "shunt"
    assert body["state"]["k3"] == "spare:unwired"


def _relays_post(ctx: Context) -> None:
    on = ctx.served.post("/api/relays/bypass", {"state": "ON"})
    assert on.status == 200, on.body
    assert (on.body["order"], on.body["state"]["path"]) == ("ROUT:BYP ON", "both")
    off = ctx.served.post("/api/relays/bypass", {"state": "OFF"})
    assert (off.body["order"], off.body["state"]["path"]) == ("ROUT:BYP OFF", "shunt")


def _abort(ctx: Context) -> None:
    nothing = ctx.served.post("/api/abort")
    assert nothing.status == 409
    assert [e["code"] for e in nothing.body["errors"]] == [111]
    started = ctx.served.post("/api/capture/start", {"profile": "STATE"})
    assert started.status == 200, started.body
    aborted = ctx.served.post("/api/abort")
    assert aborted.status == 200, aborted.body
    assert aborted.body["sent"] is True and aborted.body["errors"] == []
    ctx.served.capture_idle()


def _events(ctx: Context) -> None:
    reader = EventReader(ctx.served)
    try:
        until(lambda: bool(reader.events("status")), "the hello")
        hello = reader.events("status")[0]
        assert hello["kind"] == "hello"
        assert {"poll", "capture", "passthrough"} <= set(hello)
    finally:
        reader.close()


def _capture_get(ctx: Context) -> None:
    body = ctx.served.get("/api/capture").body
    assert body["state"] == "idle"
    # Named as the page shows a path, never by the absolute one.
    assert body["data_dir"] == shown_path(ctx.data_dir) and not body["data_dir"].startswith("/")


def _capture_start(ctx: Context) -> None:
    started = ctx.served.post("/api/capture/start", {"profile": "STATE", "seconds": 0.5})
    assert started.status == 200, started.body
    assert (started.body["state"], started.body["profile"]) == ("running", "STATE")
    last = ctx.served.capture_idle()["last"]
    assert (last["state"], last["complete"], last["ended_by"]) == ("complete", True, "seconds")


def _capture_stop(ctx: Context) -> None:
    meta = ctx.served.get(f"/api/runs/{ctx.stream_run}").body
    assert meta["ended_by"] == "stop" and meta["complete"] is True
    stopped = ctx.served.post("/api/capture/stop")
    assert stopped.status == 409 and stopped.body["error"] == "no_capture"


def _capture_mark(ctx: Context) -> None:
    meta = ctx.served.get(f"/api/runs/{ctx.stream_run}").body
    assert meta["stream"]["marks"] == 1
    marked = ctx.served.post("/api/capture/mark", {"label": "late"})
    assert marked.status == 409 and marked.body["error"] == "no_capture"


def _runs(ctx: Context) -> None:
    runs = ctx.served.get("/api/runs?limit=100").body["runs"]
    ids = [run["id"] for run in runs]
    assert {ctx.stream_run, ctx.ring_run} <= set(ids)
    assert ids == sorted(ids, reverse=True)
    assert len(ctx.served.get("/api/runs?limit=1").body["runs"]) == 1


def _run(ctx: Context) -> None:
    meta = ctx.served.get(f"/api/runs/{ctx.ring_run}").body
    assert meta["schema"] == "kleidos.meter.run/1"
    assert meta["profile"] == "TRACE" and meta["trace"]["file"] == "trace.raw"
    assert meta["complete"] is True
    assert ctx.served.get("/api/runs/nothing").status == 404


def _export(ctx: Context) -> None:
    base = f"/api/runs/{ctx.stream_run}/export?format="
    text = ctx.served.get(base + "text")
    assert text.status == 200 and text.headers["content-type"] == "text/plain; charset=utf-8"
    assert text.raw.startswith(b"METER t=")
    table = ctx.served.get(base + "csv")
    assert table.headers["content-type"] == "text/csv; charset=utf-8"
    assert table.headers["content-disposition"] == f'attachment; filename="{ctx.stream_run}.csv"'
    rows = list(csv.reader(io.StringIO(table.raw.decode())))
    samples = [row for row in rows if row[0] == "S"]
    meta = ctx.served.get(f"/api/runs/{ctx.stream_run}").body
    assert len(samples) == meta["stream"]["samples"] > 0
    as_json = json.loads(ctx.served.get(base + "json").raw)
    assert as_json["schema"] == "kleidos.meter.export/1" and as_json["run"] == ctx.stream_run


def _decimated(ctx: Context) -> None:
    query = urlencode({"column": "i_na", "buckets": 8})
    body = ctx.served.get(f"/api/runs/{ctx.ring_run}/decimated?{query}").body
    assert body["column"] == "i_na"
    columns = ("t_us", "min", "max", "n", "mean", "rms")
    assert len({len(body[name]) for name in columns}) == 1 and len(body["t_us"]) <= 8
    for lo, hi, mean, rms in zip(body["min"], body["max"], body["mean"], body["rms"], strict=True):
        assert lo <= mean <= hi and abs(mean) <= rms <= max(abs(lo), abs(hi))
    meta = ctx.served.get(f"/api/runs/{ctx.ring_run}").body
    assert sum(body["n"]) == meta["trace"]["count"]


#: Each route's contract, by method and path.
def _pins_get(ctx: Context) -> None:
    answer = ctx.served.get("/api/pins")
    assert answer.status == 200, answer.body
    body = answer.body
    assert body["table"]["matches"] is True and body["board"]["digest"] == body["live"]["table"]
    assert body["live"]["src"] in ("default", "stored") and body["differs"] == []


def _pins_stage(ctx: Context) -> None:
    answer = ctx.served.post("/api/pins/stage", {"sda": 18})
    assert answer.status == 200, answer.body
    assert answer.body["sent"] == ["SYST:PINS:STAG SDA,18,UP"]
    assert answer.body["pending"]["sda"] == 18 and answer.body["pending"]["valid"] == 1
    assert ctx.served.get("/api/pins").body["live"]["sda"] == 4
    assert ctx.served.post("/api/pins/stage", {"clear": True}).status == 200


def _pins_apply(ctx: Context) -> None:
    assert ctx.served.post("/api/pins/stage", {"addr": "0x40"}).status == 200
    answer = ctx.served.post("/api/pins/apply")
    assert answer.status == 200, answer.body
    assert (answer.body["order"], answer.body["errors"]) == ("SYST:PINS:APPL", [])
    assert answer.body["result"]["result"] == "ok" and answer.body["check"] is None
    assert (answer.body["live"]["addr"], answer.body["live"]["src"]) == ("0x40", "stored")


def _pins_defaults(ctx: Context) -> None:
    answer = ctx.served.post("/api/pins/defaults")
    assert answer.status == 200, answer.body
    assert (answer.body["live"]["addr"], answer.body["live"]["src"]) == ("auto", "default")


CONTRACTS: dict[tuple[str, str], Callable[[Context], None]] = {
    ("GET", "/api/schema"): _schema,
    ("GET", "/api/status"): _status,
    ("GET", "/api/health"): _health,
    ("GET", "/api/query"): _query,
    ("POST", "/api/scpi"): _scpi,
    ("GET", "/api/settings"): _settings_get,
    ("POST", "/api/settings"): _settings_post,
    ("GET", "/api/settings/shunt/preview"): _shunt_preview,
    ("POST", "/api/settings/shunt"): _shunt_post,
    ("GET", "/api/calibration"): _calibration_get,
    ("POST", "/api/calibration/restore"): _calibration_restore,
    ("GET", "/api/sequences"): _sequences_get,
    ("POST", "/api/sequences/run"): _sequences_run,
    ("POST", "/api/sequences/answer"): _sequences_answer,
    ("POST", "/api/sequences/abort"): _sequences_abort,
    ("GET", "/api/relays"): _relays_get,
    ("POST", "/api/relays/{relay}"): _relays_post,
    ("GET", "/api/pins"): _pins_get,
    ("POST", "/api/pins/stage"): _pins_stage,
    ("POST", "/api/pins/apply"): _pins_apply,
    ("POST", "/api/pins/defaults"): _pins_defaults,
    ("POST", "/api/abort"): _abort,
    ("GET", "/api/events"): _events,
    ("GET", "/api/capture"): _capture_get,
    ("POST", "/api/capture/start"): _capture_start,
    ("POST", "/api/capture/stop"): _capture_stop,
    ("POST", "/api/capture/mark"): _capture_mark,
    ("GET", "/api/runs"): _runs,
    ("GET", "/api/runs/{run}"): _run,
    ("GET", "/api/runs/{run}/export"): _export,
    ("GET", "/api/runs/{run}/decimated"): _decimated,
}


def test_every_route_of_the_schema_has_a_contract() -> None:
    assert list(CONTRACTS) == [(route.method, route.path) for route in SCHEMA.routes]


@pytest.mark.parametrize("route", list(CONTRACTS), ids=lambda r: f"{r[0]} {r[1]}")
def test_each_route_answers_its_contract(context: Context, route: tuple[str, str]) -> None:
    CONTRACTS[route](context)
    idle_on_the_shunt(context.served)


# -- the error mapping --------------------------------------------------------------------


@dataclass(frozen=True)
class Refusal:
    """One request and the refusal it must meet.

    :ivar method: ``GET``, ``POST`` or another.
    :ivar path: The target.
    :ivar body: What a ``POST`` carries.
    :ivar status: The status it answers.
    :ivar error: The body's ``error``, or the instrument's codes when a tuple.
    :ivar headers: Headers sent beside the change header (or instead, for ``raw``).
    :ivar raw: Send *headers* alone, without the change header.
    """

    method: str
    path: str
    body: object
    status: int
    error: str | tuple[int, ...]
    headers: dict[str, str] = field(default_factory=dict)
    raw: bool = False


#: A request of each class of refusal, and what it answers.
REFUSALS: dict[str, Refusal] = {
    "a command error (-1xx) is 400": Refusal(
        "GET", "/api/query?q=SYST:ERR:COUN%3F%201", None, 400, (-108,)
    ),
    "an execution error (-2xx) is 409": Refusal(
        "GET", "/api/query?q=CAL:SHUN:RES%3F", None, 409, (-230,)
    ),
    "a device code (100 and up) is 409": Refusal(
        "POST", "/api/relays/lead", {"state": "OPEN"}, 409, (109,)
    ),
    "a message the instrument would discard is refused unsent": Refusal(
        "POST", "/api/scpi", "*IDN?;" * 60, 400, "refused_unsent"
    ),
    "a query that acts is not a GET": Refusal(
        "GET", "/api/query?q=*TST%3F", None, 400, "get_mutates"
    ),
    "a body that is not JSON": Refusal("POST", "/api/settings", "{", 400, "bad_body"),
    "a setting that does not exist": Refusal(
        "POST", "/api/settings", {"nothing": 1}, 400, "unknown_setting"
    ),
    "a change without the header": Refusal(
        "POST", "/api/abort", b"", 403, "header_missing", raw=True
    ),
    "a change from another origin": Refusal(
        "POST", "/api/abort", b"", 403, "foreign_origin", {"Origin": "http://elsewhere.example"}
    ),
    "a foreign host": Refusal(
        "GET", "/api/status", None, 421, "misdirected", {"Host": "elsewhere.example"}, raw=True
    ),
    "an unknown path": Refusal("GET", "/api/nothing", None, 404, "not_found"),
    "an unknown relay": Refusal("POST", "/api/relays/k9", {"state": "ON"}, 404, "unknown_relay"),
    "an unknown sequence": Refusal(
        "POST", "/api/sequences/run", {"name": "NOTHING"}, 404, "unknown_sequence"
    ),
    "a method a route does not take": Refusal(
        "PUT", "/api/status", None, 405, "method_not_allowed"
    ),
    "a GET on a route that changes": Refusal("GET", "/api/abort", None, 405, "method_not_allowed"),
    "a body beyond the limit": Refusal("POST", "/api/scpi", "x" * 20_000, 413, "too_large"),
    "a field the pin map does not have": Refusal(
        "POST", "/api/pins/stage", {"sdb": 18}, 400, "unknown_field"
    ),
    "an answer to no question": Refusal(
        "POST", "/api/sequences/answer", {"id": 99, "answer": "cancel"}, 409, "no_prompt"
    ),
    "an abort of no sequence": Refusal("POST", "/api/sequences/abort", b"", 409, "no_sequence"),
}


@pytest.mark.parametrize("name", list(REFUSALS))
def test_each_class_of_refusal_answers_its_status(context: Context, name: str) -> None:
    refusal = REFUSALS[name]
    served = context.served
    if refusal.method == "POST" and not refusal.raw:
        answer = served.post(refusal.path, refusal.body, headers=refusal.headers)
    else:
        answer = served.request(refusal.method, refusal.path, refusal.body, headers=refusal.headers)
    assert answer.status == refusal.status, answer.body
    if isinstance(refusal.error, tuple):
        assert tuple(e["code"] for e in answer.body["errors"]) == refusal.error
    else:
        assert answer.body["error"] == refusal.error
    assert not [h for h in answer.headers if h.startswith("access-control-")]
    idle_on_the_shunt(served)


# -- the hold -----------------------------------------------------------------------------


def _attach(served: Served) -> socket.socket:
    """A passthrough client, once serve has put the panel on hold for it."""
    assert served.passthrough is not None
    host, port = served.passthrough.removeprefix("socket://").split(":")
    client = socket.create_connection((host, int(port)), timeout=10)
    until(
        lambda: served.get("/api/status").body["passthrough"]["state"] == "attached",
        "the client to have the line",
    )
    return client


def _sample_request(route_path: str) -> str:
    """A concrete target for a route's path pattern."""
    return route_path.replace("{relay}", "bypass").replace("{run}", "nothing")


def test_while_a_client_has_the_line_every_route_that_sends_answers_423(context: Context) -> None:
    served = context.served
    client = _attach(served)
    try:
        holder = served.get("/api/status").body["held"]
        assert "passthrough" in holder
        for route in SCHEMA.routes:
            if route.path == "/api/events":
                continue
            target = _sample_request(route.path)
            answer = served.post(target, {}) if route.method == "POST" else served.get(target)
            if route.sends:
                assert answer.status == 423, (route.path, answer.status, answer.body)
                assert answer.body["holder"] == holder, route.path
            else:
                assert answer.status != 423, route.path
        # What the instrument says goes to the client alone, byte for byte.
        client.sendall(b"*IDN?;:SYST:ERR:ALL?\n")
        data = b""
        while not data.endswith(b"\n"):
            data += client.recv(4096)
        assert data == f'KLEIDOS,KLEIDOS-METER,{SIM_SERIAL},0.18.0;0,"No error"\n'.encode()
    finally:
        client.close()
    until(
        lambda: (
            served.get("/api/status").body["passthrough"]["state"] == "listening"
            and served.get("/api/status").body["held"] is None
        ),
        "the line back",
    )
    assert served.get("/api/query?q=*IDN%3F").status == 200
    idle_on_the_shunt(served)
