# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's measurement sequences, run through the API on the clock-driven simulator.

Every sequence of the set runs to ``done``; a step that fails, an abort, a
question declined and serve stopping all run the cleanup, which puts the
relays back on the shunt and the I2C clock back as it was. The runner's own
mechanics (each step's failures) run on definitions of the tests' own.
"""

from __future__ import annotations

import http.client
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kleidos_meter import sim as sim_module
from kleidos_meter.panel import sequences as sequences_module
from kleidos_meter.panel.http import Api, ApiResponse
from kleidos_meter.panel.schema import SCHEMA, SEQUENCES, resolve_unit
from kleidos_meter.panel.sequences import OUTCOMES, SequenceRefused, SequenceRunner
from kleidos_meter.panel.steps import (
    DEFINITIONS,
    CalibrationStep,
    Check,
    CloseNull,
    Definition,
    MarkStep,
    Param,
    Prompt,
    Query,
    Send,
    Span,
    StartCapture,
    Step,
    StopCapture,
    Wait,
    WaitCapture,
    WaitUntil,
    message_headers,
    placeholders,
)
from kleidos_meter.protocol import Mark
from test_panel_http import WAIT, Panel, panel_port, running_serve, serving_panel, until
from test_registry import accepted, add_events

#: The ring the tests' simulator keeps: a TRACE fills in 1.1 s, a BURST in 0.4 s.
RING = 4096


@pytest.fixture
def registry(registry_root: Path) -> Path:
    """A registry that files the simulator's calibration."""
    add_events(registry_root, accepted(registry_root, "2026-11-02-01"))
    return registry_root


#: How much faster than real time the tests' simulated time runs. The
#: recorder's timer looks at the clock every few real milliseconds, so a
#: simulated time that runs much faster ends a capture far past its seconds.
PACE = 20.0


@pytest.fixture
def panel(registry: Path) -> Iterator[Panel]:
    with serving_panel(sim_options={"ring_capacity": RING}, registry=registry) as served:
        thaw(served)
        yield served


def run(
    panel: Panel, name: str, params: dict[str, Any] | None = None, answers: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Run one sequence through the API to its end, answering its questions in turn."""
    started = panel.post("/api/sequences/run", {"name": name, "params": params or {}})
    assert started.status == 200, started.body
    pending = list(answers)
    deadline = time.monotonic() + WAIT * 3
    while panel.sequences.running:
        assert time.monotonic() < deadline, f"{name} did not end"
        prompt = panel.sequences.state_json()["prompt"]
        if prompt is not None and pending:
            answer = panel.post(
                "/api/sequences/answer", {"id": prompt["id"], "answer": pending.pop(0)}
            )
            assert answer.status == 200, answer.body
        time.sleep(0.002)
    state: dict[str, Any] = panel.get("/api/sequences").body
    assert state["state"] == "idle"
    return state


def at_rest(panel: Panel) -> None:
    """The relays on the shunt, the bus at 400 kHz, no window open."""
    assert panel.sim.path == "shunt"
    assert panel.sim.scl_hz == 400_000
    assert not panel.sim.window_open


def freeze(panel: Panel) -> None:
    """Stop the simulated time: a window stays open until something closes it."""
    panel.sim.sleep = lambda seconds: time.sleep(0.0002)


def thaw(panel: Panel) -> None:
    """Let the simulated time run again, :data:`PACE` times faster than real time."""
    clock = panel.clock

    def paced(seconds: float) -> None:
        time.sleep(seconds / PACE)
        clock.sleep(seconds)

    panel.sim.sleep = paced


def step_running(panel: Panel, describe: str) -> None:
    """Wait until the running sequence is on the step *describe* names."""

    def on_it() -> bool:
        steps = panel.sequences.state_json().get("steps", [])
        return bool(steps) and steps[-1]["step"] == describe and steps[-1]["state"] == "running"

    until(on_it, describe)


# -- the set ---------------------------------------------------------------------------


def test_the_schema_declares_each_definition_and_everything_it_sends() -> None:
    assert [s.name for s in SEQUENCES] == [d.name for d in DEFINITIONS]
    for info, definition in zip(SEQUENCES, DEFINITIONS, strict=True):
        assert info.uses == definition.uses() and info.params == definition.params
        assert info.title and info.summary
        for header in info.uses:
            assert resolve_unit(header)[0] is not None, header
    assert {s.name for s in SEQUENCES} == {
        "HEALTH",
        "NULL_RELAY",
        "NULL_LEAD",
        "STATE",
        "SLEEP",
        "TRACE",
        "BURST",
        "COLD_BOOT",
        "CAL_VERIFY",
        "CAL_RESTORE",
    }
    served = SCHEMA.to_json()["sequences"]
    assert served[7]["params"][0] == {
        "name": "off_ms",
        "label": "Off for",
        "unit": "ms",
        "default": 2000,
        "low": 1,
        "high": 60000,
    }


def test_every_brace_a_definition_writes_is_a_parameter_or_a_kept_value() -> None:
    for definition in DEFINITIONS:
        names = {p.name for p in definition.params} | set(definition.defaults)
        names |= {s.var for s in definition.steps if isinstance(s, Query) and s.var}
        for step in (*definition.steps, *definition.cleanup):
            text = getattr(step, "message", "") + getattr(step, "text", "")
            text += getattr(step, "value", "")
            assert placeholders(text) <= names, (definition.name, step)
            for span in (getattr(step, "span", None), getattr(step, "timeout", None)):
                assert span is None or span.param is None or span.param in names
        assert set(OUTCOMES) == {"done", "failed", "aborted", "declined"}


def test_a_messages_headers_are_its_units_headers() -> None:
    assert message_headers("CAL:ZERO {seconds};:ABOR; ") == ("CAL:ZERO", "ABOR")
    assert placeholders("a {b} c {d}") == {"b", "d"}
    assert Span(10.0, "off_ms", 0.001).seconds({"off_ms": 2000}) == 12.0


def test_health_runs_to_done_with_every_report_kept(panel: Panel) -> None:
    state = run(panel, "HEALTH")
    assert state["outcome"] == "done", (state["detail"], state["steps"][-1])
    results = state["results"]
    assert results["heap"]["allocs_since_main"] == 0 and results["self_test"] == "0"
    assert results["scan"]["found"] == "0x40" and results["idn"].startswith("KLEIDOS,")
    assert [s["state"] for s in state["steps"]] == ["ok"] * 15
    assert state["total"] == 15  # the page's progress: every step begun of its total
    assert state["steps"][-1]["detail"] == "heap frees_since_main is 0"
    assert state["cleanup"] == [] and state["sequence"] == "HEALTH"


def test_health_on_an_older_image_passes_the_keys_it_does_not_report(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = sim_module._HANDLERS["DIAG:HEAP?"]

    def older(sim: Any, call: Any) -> str:
        answer = original(sim, call)
        assert isinstance(answer, str)
        return answer.split(" pre_main_allocs")[0]  # 0.16.6 and before

    monkeypatch.setitem(sim_module._HANDLERS, "DIAG:HEAP?", older)
    state = run(panel, "HEALTH")
    assert state["outcome"] == "done", (state["detail"], state["steps"][-1])
    assert state["steps"][-1]["detail"] == "heap frees_since_main: not reported"


def test_a_failing_self_test_fails_health(panel: Panel) -> None:
    panel.sim.self_test = 4
    state = run(panel, "HEALTH")
    assert state["outcome"] == "failed"
    assert state["detail"] == "self_test is 4, not eq 0"


@pytest.mark.parametrize("name", ["NULL_RELAY", "NULL_LEAD"])
def test_a_null_runs_to_done_and_ends_on_the_shunt(panel: Panel, name: str) -> None:
    state = run(panel, name, {"seconds": 2}, answers=("continue",))
    assert state["outcome"] == "done", (state["detail"], state["steps"][-1])
    assert state["results"]["null"]["stored"] == 1
    assert state["results"]["route"]["path"] == "shunt"
    assert [entry["state"] for entry in state["cleanup"]] == ["ok"] * 4
    assert state["cleanup"][0]["detail"] == "no window of the sequence's is open"
    at_rest(panel)
    assert panel.sim.nulled[0]


def test_a_null_on_the_relay_path_passes_through_the_bypass(panel: Panel) -> None:
    state = run(panel, "NULL_RELAY", {"seconds": 1})
    assert state["results"]["route_null"]["path"] == "bypass"
    assert "ROUT:NULL ON;:SYST:ERR:ALL?" in panel.sim.messages


@pytest.mark.parametrize(("name", "seconds", "rate"), [("STATE", 2, 868), ("SLEEP", 10, 14.8)])
def test_a_streamed_capture_runs_to_done_as_a_complete_run(
    panel: Panel, name: str, seconds: int, rate: float
) -> None:
    state = run(panel, name, {"seconds": seconds})
    assert state["outcome"] == "done", (state["detail"], state["steps"][-1])
    capture = state["results"]["capture"]
    assert capture["id"] == state["run"] == state["results"]["run"]
    assert capture["complete"] and capture["profile"] == name
    assert int(capture["samples"]) >= rate * seconds * 0.95  # the recorder stops it after
    meta = panel.recorder.run(state["run"]).meta
    assert meta["label"] == f"sequence {name}" and meta["ended_by"] == "seconds"
    assert state["cleanup"][0]["detail"] == "no capture of the sequence's runs"


@pytest.mark.parametrize("name", ["TRACE", "BURST"])
def test_a_ring_runs_to_done_with_its_dump(panel: Panel, name: str) -> None:
    state = run(panel, name)
    assert state["outcome"] == "done", (state["detail"], state["steps"][-1])
    meta = panel.recorder.run(state["run"]).meta
    assert meta["trace"]["count"] == RING and meta["ended_by"] == "ended_by_itself"
    at_rest(panel)


def test_burst_takes_the_bus_to_1_mhz_and_gives_it_back(panel: Panel) -> None:
    panel.post("/api/settings", {"i2c_hz": 800_000})
    state = run(panel, "BURST")
    assert state["outcome"] == "done", (state["detail"], state["steps"][-1])
    assert state["results"]["i2c"] == "800000" and state["results"]["i2c_after"] == "800000"
    assert "SYST:COMM:I2C:FREQ 1000000;:SYST:ERR:ALL?" in panel.sim.messages
    assert panel.sim.scl_hz == 800_000


def test_cold_boot_cycles_the_lead_under_a_marked_capture(panel: Panel) -> None:
    state = run(panel, "COLD_BOOT", {"off_ms": 500, "seconds": 2}, answers=("continue",))
    assert state["outcome"] == "done", (state["detail"], state["steps"][-1])
    run_ = panel.recorder.run(state["run"])
    marks = [record.label for record in run_.records() if isinstance(record, Mark)]
    assert marks == ["cut", "k1:open", "k1:closed", "reclosed"]  # the instrument marks its moves
    assert "ROUT:LEAD:CYCL 500,FORC,LIVE;:SYST:ERR:ALL?" in panel.sim.messages
    assert state["cleanup"][0]["detail"] == "no capture of the sequence's runs"
    at_rest(panel)


def test_the_calibration_is_restored_then_verified(panel: Panel) -> None:
    failed = run(panel, "CAL_VERIFY")
    assert failed["outcome"] == "failed"
    assert failed["detail"] == "the calibration is working_copy_lost"
    restored = run(panel, "CAL_RESTORE", answers=("continue",))
    assert restored["outcome"] == "done", restored
    assert restored["results"]["calibration"]["status"] == "applied"
    assert panel.sim.shunt_measured and panel.sim.tag == "R100_20261120_HR"
    verified = run(panel, "CAL_VERIFY")
    assert verified["outcome"] == "done" and verified["results"]["calibration"]["ok"]


# -- the cleanup -----------------------------------------------------------------------


def test_a_step_that_fails_off_the_shunt_puts_the_path_back(panel: Panel) -> None:
    panel.sim.device_ready = False  # CALibration:ZERO needs the INA228: -241
    state = run(panel, "NULL_RELAY")
    assert state["outcome"] == "failed"
    failed = state["steps"][-1]
    assert failed["step"] == "send CAL:ZERO {seconds}" and failed["state"] == "failed"
    assert failed["errors"][0]["code"] == -241
    assert state["detail"] == "CAL:ZERO 5: the instrument refused it"
    assert [entry["state"] for entry in state["cleanup"]] == ["ok"] * 4
    assert panel.sim.path == "shunt"


def test_a_step_that_fails_at_1_mhz_gives_the_bus_its_clock_back(panel: Panel) -> None:
    panel.post("/api/scpi", "TRIG:SOUR LEV;:SENS:CAPT STAT")  # INITiate refuses a level ring
    state = run(panel, "BURST")
    assert state["outcome"] == "failed"
    assert state["steps"][-1]["step"] == "start a BURST capture"
    assert state["steps"][-1]["errors"][0]["code"] == -221
    assert state["cleanup"][1]["detail"] == "SYST:COMM:I2C:FREQ 400000"
    at_rest(panel)


def test_an_abort_closes_the_null_and_puts_the_path_back(panel: Panel) -> None:
    freeze(panel)
    panel.post("/api/sequences/run", {"name": "NULL_RELAY", "params": {"seconds": 600}})
    step_running(panel, "wait until SYST:STAT? running is 0")
    assert panel.sim.path == "bypass" and panel.sim.zero_open
    answer = panel.post("/api/sequences/abort")
    assert answer.status == 200
    assert panel.sequences.wait_idle(WAIT)
    thaw(panel)
    state = panel.sequences.state_json()
    assert state["outcome"] == "aborted" and state["steps"][-1]["state"] == "aborted"
    assert state["cleanup"][0]["detail"] == "closed it"
    at_rest(panel)
    assert panel.sim.nulled[0]  # an ABORt computes and stores the null


def test_an_abort_under_a_ring_stops_it_and_gives_the_bus_its_clock_back(panel: Panel) -> None:
    freeze(panel)
    panel.post("/api/sequences/run", {"name": "BURST"})
    step_running(panel, "wait for the capture's end")
    assert panel.sim.scl_hz == 1_000_000 and panel.recorder.state == "running"
    answer = panel.post("/api/abort")  # the panel's own abort ends the sequence too
    assert answer.body["sequence"]["sequence"] == "BURST"
    assert panel.sequences.wait_idle(WAIT)
    thaw(panel)
    state = panel.sequences.state_json()
    assert state["outcome"] == "aborted"
    assert [e["detail"] for e in state["cleanup"]] == ["stopped", "SYST:COMM:I2C:FREQ 400000"]
    assert panel.recorder.last_meta is not None and panel.recorder.last_meta["ended_by"] == "stop"
    at_rest(panel)


def test_an_abort_under_a_cold_boot_stops_the_capture_and_recloses_the_lead(
    panel: Panel,
) -> None:
    panel.post("/api/sequences/run", {"name": "COLD_BOOT", "params": {"off_ms": 60000}})
    until(lambda: panel.sequences.state_json()["prompt"] is not None, "the question")
    prompt = panel.sequences.state_json()["prompt"]
    assert "60000 ms" in prompt["text"] and prompt["choices"] == ["continue", "cancel"]
    panel.post("/api/sequences/answer", {"id": prompt["id"], "answer": "continue"})
    step_running(panel, "wait until ROUT:STAT? path is shunt")
    assert panel.sim.path == "none"
    panel.post("/api/sequences/abort")
    assert panel.sequences.wait_idle(WAIT)
    state = panel.sequences.state_json()
    assert state["outcome"] == "aborted"
    assert [e["state"] for e in state["cleanup"]] == ["ok"] * 4
    at_rest(panel)


def test_a_declined_question_moves_nothing(panel: Panel) -> None:
    state = run(panel, "NULL_LEAD", answers=("cancel",))
    assert state["outcome"] == "declined" and state["detail"] == "answered cancel"
    assert not any("ROUT:LEAD" in message for message in panel.sim.messages)
    at_rest(panel)


def test_serve_stopping_aborts_the_sequence_and_runs_its_cleanup(panel: Panel) -> None:
    panel.post("/api/sequences/run", {"name": "NULL_LEAD"})
    until(lambda: panel.sequences.state_json()["prompt"] is not None, "the question")
    panel.sequences.close()
    state = panel.sequences.state_json()
    assert state["outcome"] == "aborted" and state["prompt"] is None
    assert [e["state"] for e in state["cleanup"]] == ["ok"] * 4
    refused = panel.post("/api/sequences/run", {"name": "HEALTH"})
    assert refused.status == 503 and refused.body["error"] == "closing"
    panel.sequences.close()  # again: nothing


# -- the guards ------------------------------------------------------------------------


def test_changes_wait_for_the_sequence_but_reads_and_its_own_routes_do_not(
    panel: Panel,
) -> None:
    panel.post("/api/sequences/run", {"name": "NULL_LEAD"})
    until(lambda: panel.sequences.state_json()["prompt"] is not None, "the question")
    for path, body in (
        ("/api/settings", {"sample_count": 5}),
        ("/api/relays/lead", {"state": "OPEN", "force": True}),
        ("/api/capture/start", {"profile": "STATE"}),
        ("/api/scpi", "ROUT:LEAD OPEN,FORC"),
        ("/api/sequences/run", {"name": "HEALTH"}),
    ):
        answer = panel.post(path, body)
        assert answer.status == 409 and answer.body["error"] == "sequence_running", path
    assert panel.get("/api/settings").status == 200
    assert panel.get("/api/sequences").body["state"] == "prompt"
    wrong = panel.post("/api/sequences/answer", {"id": 999, "answer": "continue"})
    assert wrong.status == 409 and wrong.body["error"] == "no_prompt"
    prompt = panel.sequences.state_json()["prompt"]
    bad = panel.post("/api/sequences/answer", {"id": prompt["id"], "answer": "maybe"})
    assert bad.status == 400 and bad.body["error"] == "bad_answer"
    aborted = panel.post("/api/abort")
    assert aborted.status == 409 and aborted.body["errors"][0]["code"] == 111  # nothing open
    assert panel.sequences.wait_idle(WAIT)
    assert panel.sequences.state_json()["outcome"] == "aborted"
    late = panel.post("/api/sequences/answer", {"id": prompt["id"], "answer": "continue"})
    assert late.status == 409
    assert panel.post("/api/sequences/abort").body["error"] == "no_sequence"


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ({"name": "NOPE"}, 404, "unknown_sequence"),
        ({"name": "HEALTH", "params": {"seconds": 1}}, 400, "bad_parameter"),
        ({"name": "STATE", "params": {"seconds": 0}}, 400, "bad_parameter"),
        ({"name": "STATE", "params": {"seconds": "10"}}, 400, "bad_parameter"),
        ({"name": "STATE", "params": {"seconds": True}}, 400, "bad_parameter"),
        ({"name": "STATE", "params": []}, 400, "bad_body"),
        ({"params": {}}, 400, "bad_body"),
        ({"name": "STATE", "extra": 1}, 400, "bad_body"),
    ],
)
def test_a_sequence_of_the_wrong_form_is_refused_unsent(
    panel: Panel, body: dict[str, Any], status: int, error: str
) -> None:
    answer = panel.post("/api/sequences/run", body)
    assert (answer.status, answer.body["error"]) == (status, error)
    assert panel.sent == []


def test_an_answer_of_the_wrong_form_is_refused(panel: Panel) -> None:
    answer = panel.post("/api/sequences/answer", {"answer": "continue"})
    assert answer.status == 400 and answer.body["error"] == "bad_body"


def test_a_sequence_waits_for_a_capture_or_a_window_it_did_not_open(panel: Panel) -> None:
    assert panel.get("/api/sequences").body == {"state": "idle", "sequence": None}
    panel.post("/api/capture/start", {"profile": "STATE"})
    refused = panel.post("/api/sequences/run", {"name": "HEALTH"})
    assert refused.status == 409 and refused.body["error"] == "capture_running"
    panel.post("/api/capture/stop")
    session = panel.api.session
    panel.executor.remove_listener(session._on_event)  # a poll finding it idle clears it
    panel.executor.call("*OPC?", wait=WAIT)  # the thread is past any event it was handing out
    session.foreign_window = {"sink": "null"}
    refused = panel.post("/api/sequences/run", {"name": "HEALTH"})
    assert refused.status == 409 and refused.body["error"] == "window_open"


def test_without_a_runner_the_routes_answer_503(panel: Panel) -> None:
    bare = Api(panel.api.session)
    host = {"Host": "127.0.0.1", "X-Kleidos-Meter": "1"}
    for method, path in (("GET", "/api/sequences"), ("POST", "/api/sequences/abort")):
        answer = bare.handle(method, path, host)
        assert isinstance(answer, ApiResponse)
        assert answer.status == 503 and answer.body["error"] == "unavailable"


def test_each_change_is_published_as_a_sequence_event(panel: Panel) -> None:
    seen: list[dict[str, Any]] = []
    original = panel.live.publish

    def keep(kind: str, data: dict[str, Any]) -> None:
        if kind == "sequence":
            seen.append(data)
        original(kind, data)

    panel.sequences.live = type("Hub", (), {"publish": staticmethod(keep)})()
    run(panel, "HEALTH")
    assert seen[0]["state"] == "running" and seen[-1]["outcome"] == "done"
    assert len(seen) >= 2 * 15


# -- the runner's mechanics, on definitions of the tests' own ---------------------------


def runner_of(panel: Panel, *definitions: Definition, **options: Any) -> SequenceRunner:
    """A runner of *definitions* on the panel's executor and recorder."""
    return SequenceRunner(
        panel.executor,
        panel.recorder,
        calibration=options.pop("calibration", panel.calibration),
        definitions=definitions,
        clock=panel.clock,
        interval=0.002,
        **options,
    )


def outcome_of(runner: SequenceRunner, name: str, **params: Any) -> dict[str, Any]:
    """Run *name* on *runner* to its end; its state."""
    runner.start(name, params)
    assert runner.wait_idle(WAIT * 3)
    return runner.state_json()


@pytest.mark.parametrize(
    ("steps", "detail"),
    [
        ((Check("x", None, "eq", "1"),), "nothing was kept as x"),
        ((Query("SYST:STAT?", "s"), Check("s", "nope", "eq", "1")), "s nope: not reported"),
        (
            (Query("SYST:STAT?", "s"), Check("s", "sink", "eq", "stream")),
            "s sink is idle, not eq stream",
        ),
        ((Query("SYST:STAT?", "s", var="v", key="nope"),), "SYST:STAT? reports no nope"),
        ((Send("SAMP:COUN {nope}"),), "nothing has set {nope}"),
        ((Query("BOGUS?", "b"),), "'BOGUS?': -113,\"Undefined header;BOGUS?\""),
        (
            (WaitUntil("SYST:STAT?", "sink", ("stream",), Span(1.0)),),
            "sink was still idle after 1 s",
        ),
        ((MarkStep("x"),), "no capture of serve's is running"),
        ((StartCapture("NOPE"),), "the capture takes one of TRACe, STATe, SLEep, BURSt"),
        ((StopCapture(),), "the sequence's capture left no run"),
        ((CalibrationStep("verify"),), "the calibration is working_copy_lost"),
    ],
)
def test_a_step_that_does_not_hold_fails_the_sequence_with_its_reason(
    panel: Panel, steps: tuple[Any, ...], detail: str
) -> None:
    runner = runner_of(panel, Definition("T", "T", "t", steps))
    state = outcome_of(runner, "T")
    assert (state["outcome"], state["detail"]) == ("failed", detail)
    assert state["steps"][-1]["state"] == "failed"


def test_checks_take_parameters_kept_values_and_lists(panel: Panel) -> None:
    steps = (
        Query("DIAG:I2C:SCAN?", "scan", var="found", key="found"),
        Query("SAMP:COUN?", "count", var="count"),
        Check("scan", "found", "contains", "0x40"),
        Check("count", None, "eq", "{count}"),
        Check("scan", "n", "eq", "{n}"),
        Wait(Span(0.5, "n", 0.0)),
    )
    runner = runner_of(
        panel, Definition("T", "T", "t", steps, params=(Param("n", "N", "", 1, 0, 9),))
    )
    state = outcome_of(runner, "T")
    assert state["outcome"] == "done", (state["detail"], state["steps"][-1])
    assert state["params"] == {"n": 1} and state["results"]["count"] == "100"
    assert state["steps"][-1]["detail"] == "0.5 s"


def test_a_mark_the_instrument_refuses_fails_the_step(panel: Panel) -> None:
    steps: tuple[Step, ...] = (
        StartCapture("STATE"),
        *(MarkStep(f"m{n}") for n in range(17)),
        StopCapture(),
    )
    runner = runner_of(panel, Definition("T", "T", "t", steps, (StopCapture(),)))
    state = outcome_of(runner, "T")
    assert state["outcome"] == "failed" and state["detail"] == "the mark m16 was refused"
    assert state["cleanup"][0]["detail"] == "stopped"


def test_a_capture_that_does_not_end_in_time_or_ends_incomplete_fails(panel: Panel) -> None:
    runner = runner_of(
        panel,
        Definition(
            "LATE", "L", "l", (StartCapture("STATE"), WaitCapture(Span(1.0))), (StopCapture(),)
        ),
        Definition("BAD", "B", "b", (StartCapture("TRACE"), WaitCapture(Span(60.0), ring=True))),
    )
    late = outcome_of(runner, "LATE")
    assert late["detail"] == "the capture had not ended after 1 s"
    panel.sim.corrupt_dump = True  # the ring arrives damaged, twice
    bad = outcome_of(runner, "BAD")
    assert bad["outcome"] == "failed" and bad["detail"].startswith("run ")
    assert bad["detail"].endswith("is incomplete (ended by ended_by_itself)")


def test_a_null_window_that_closed_by_itself_meanwhile_is_left(panel: Panel) -> None:
    steps = (Send("CAL:ZERO 1"), Send("ABOR"), Send("CAL:ZERO 1"), Send("SYST:BOGUS"))
    runner = runner_of(panel, Definition("T", "T", "t", steps, (CloseNull(),)))
    panel.sim.zero_open = False  # nothing open before it
    original = panel.executor.query_all

    def closed_meanwhile(units: Any, **kwargs: Any) -> list[str]:
        if list(units) == ["SYST:STAT?"]:
            panel.executor.call("ABOR")  # someone else closed it
        return original(units, **kwargs)

    panel.executor.query_all = closed_meanwhile  # type: ignore[method-assign]  # a planted race
    try:
        state = outcome_of(runner, "T")
    finally:
        panel.executor.query_all = original  # type: ignore[method-assign]  # put back
    assert state["outcome"] == "failed"
    assert state["cleanup"][0]["detail"] == "the window had closed"


def test_a_question_nobody_answers_is_declined(panel: Panel) -> None:
    runner = runner_of(panel, Definition("T", "T", "t", (Prompt("Sure?"),)), prompt_timeout=0.05)
    state = outcome_of(runner, "T")
    assert (state["outcome"], state["detail"]) == ("declined", "the question went unanswered")


def test_a_cleanup_that_does_not_hold_fails_a_sequence_that_did(panel: Panel) -> None:
    cleanup = (Check("nothing", None, "eq", "1"), Wait(Span(0.2)), Send("SYST:BOGUS"))
    runner = runner_of(panel, Definition("T", "T", "t", (Wait(Span(0.1)),), cleanup))
    state = outcome_of(runner, "T")
    assert (state["outcome"], state["detail"]) == ("failed", "its cleanup did not hold")
    assert [e["state"] for e in state["cleanup"]] == ["failed", "ok", "failed"]


def test_a_steps_own_bug_fails_it_logged_and_the_cleanup_still_runs(
    panel: Panel, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("a bug")

    monkeypatch.setattr(panel.recorder, "mark", broken)
    runner = runner_of(
        panel,
        Definition("T", "T", "t", (StartCapture("STATE"), MarkStep("x")), (StopCapture(),)),
    )
    with caplog.at_level(logging.ERROR, logger=sequences_module.__name__):
        state = outcome_of(runner, "T")
    assert (state["outcome"], state["detail"]) == ("failed", "a bug")
    assert state["cleanup"][0]["detail"] == "stopped"
    assert "sequence T: mark x" in caplog.text


def test_a_sequence_without_a_registry_cannot_verify(panel: Panel) -> None:
    runner = runner_of(
        panel, Definition("T", "T", "t", (CalibrationStep("restore"),)), calibration=None
    )
    assert outcome_of(runner, "T")["detail"] == "serve was given no registry"


def test_a_line_that_fails_under_a_step_fails_it(
    panel: Panel, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kleidos_meter.client import MeterError

    def down(*args: Any, **kwargs: Any) -> Any:
        raise MeterError("")

    runner = runner_of(panel, Definition("T", "T", "t", (Send("SYST:STAT?"),)))
    monkeypatch.setattr(panel.executor, "call", down)
    state = outcome_of(runner, "T")
    assert (state["outcome"], state["detail"]) == ("failed", "MeterError")


def test_the_runner_refuses_what_it_cannot_run(panel: Panel) -> None:
    runner = runner_of(panel, Definition("T", "T", "t", (Prompt("Sure?"),)))
    with pytest.raises(SequenceRefused) as unknown:
        runner.start("NOPE")
    assert unknown.value.status == 404 and "one of T" in unknown.value.detail
    runner.start("T")
    with pytest.raises(SequenceRefused) as busy:
        runner.start("T")
    assert busy.value.error == "sequence_running"
    runner.close()
    assert runner.state_json()["outcome"] == "aborted"


def test_serve_aborts_a_running_sequence_on_its_way_out() -> None:
    with running_serve(["--sim", "--http", "0"]) as (out, _, status):
        port = panel_port(out)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=WAIT)
        body = json.dumps({"name": "NULL_LEAD"})
        conn.request(
            "POST",
            "/api/sequences/run",
            body,
            {
                "Host": f"127.0.0.1:{port}",
                "X-Kleidos-Meter": "1",
                "Content-Type": "application/json",
            },
        )
        assert conn.getresponse().status == 200
        conn.close()
    assert status == [0]
    lines = out.getvalue()
    assert "aborting the sequence; its cleanup runs" in lines
    assert lines.index("aborting the sequence") < lines.index("released the port")
