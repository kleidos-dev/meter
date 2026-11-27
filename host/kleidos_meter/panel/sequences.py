# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Run the panel's measurement sequences: one at a time, on a thread, the cleanup always.

:class:`SequenceRunner` runs one :class:`~kleidos_meter.panel.steps.Definition`
at a time on a thread of its own, through the session's executor (every
message waits its turn behind the one in flight) and its recorder (a
capture a sequence takes is a run like any other). Its state, the step it
is on, a question waiting for its answer and each step's outcome, is what
``GET /api/sequences`` answers and what every ``sequence`` event of
``/api/events`` carries.

How a sequence ends (its ``outcome``):

- ``done``: every step held.
- ``failed``: a step did not (an error the instrument queued, a check that
  did not hold, a wait that timed out, a run that was not complete); the
  step names why.
- ``aborted``: :meth:`SequenceRunner.abort`, ``POST /api/abort`` or serve
  stopping (:meth:`SequenceRunner.close`). A wait ends at once; a message
  in flight is answered first.
- ``declined``: a question was answered with anything but the answer that
  goes on, or went unanswered for :attr:`SequenceRunner.prompt_timeout`.

Whichever it is, the definition's cleanup then runs, every step of it even
when one fails (each failure is kept): it stops a capture the sequence
started that still runs, closes a null window it opened, puts the relays
back on the shunt and the I2C clock back as the sequence found it. The
instrument's own hold puts the relays back 120 s after a host that died
before its cleanup could.

A sequence is refused while another runs, while a capture runs (the
recorder's or one serve found) and for a parameter it does not take or out
of its bounds (:class:`SequenceRefused`).
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ..client import MeterError
from ..protocol import parse_fields
from .executor import WINDOW_OPENERS, Executor, analyze
from .recorder import CaptureRefused, Recorder, Run
from .reports import errors_json, typed_fields
from .steps import (
    DEFINITIONS,
    CalibrationStep,
    Check,
    CloseNull,
    Definition,
    MarkStep,
    Prompt,
    Query,
    Send,
    StartCapture,
    Step,
    StopCapture,
    Wait,
    WaitCapture,
    WaitUntil,
)

if TYPE_CHECKING:  # the types are for the checker only
    from .live import LiveHub
    from .settings import Calibration

__all__ = ["OUTCOMES", "SequenceRefused", "SequenceRunner"]

_LOG = logging.getLogger(__name__)

#: How a sequence may end.
OUTCOMES = ("done", "failed", "aborted", "declined")


class SequenceRefused(Exception):
    """A sequence route the runner refuses, with the HTTP status it maps to.

    :param status: 400, 404, 409 or 503.
    :param error: A short machine word for the page.
    :param detail: What went wrong, for a person.
    """

    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


class _StepFailed(Exception):
    """A step that did not hold; the sequence fails, its cleanup runs."""

    def __init__(self, detail: str, errors: list[Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []


class _Ended(Exception):
    """The sequence ends before its last step: aborted, or a question declined.

    :param outcome: ``aborted`` or ``declined``.
    """

    def __init__(self, outcome: str, detail: str) -> None:
        super().__init__(detail)
        self.outcome = outcome
        self.detail = detail


class _Values(dict[str, Any]):
    """The values braces name; a name nothing set is a failed step, never a stray brace."""

    def __missing__(self, key: str) -> str:
        raise _StepFailed(f"nothing has set {{{key}}}")


class _Run:
    """One sequence as it runs: what it was given, what it kept, what it opened."""

    def __init__(self, number: int, definition: Definition, params: dict[str, int]) -> None:
        self.number = number
        self.definition = definition
        self.params = params
        self.values = _Values({**definition.defaults, **{k: str(v) for k, v in params.items()}})
        self.kept: dict[str, str] = {}
        self.results: dict[str, Any] = {}
        self.log: list[dict[str, Any]] = []
        self.cleanup_log: list[dict[str, Any]] = []
        self.state = "running"
        self.outcome: str | None = None
        self.detail = ""
        self.window_open = False  # a window the sequence opened
        self.run_id: str | None = None  # the run of the capture it started
        self.prompt: dict[str, Any] | None = None
        self.answer: str | None = None
        self.answered = threading.Event()
        self.started = 0.0
        self.elapsed = 0.0


class SequenceRunner:
    """Run one sequence at a time: see the module's description.

    :param executor: The line's owner; every message goes through it.
    :param recorder: The recorder a sequence's captures are taken by; its
        session says whether a window serve did not open is open.
    :param calibration: The registry's side, for ``CAL_VERIFY`` and ``CAL_RESTORE``.
    :param live: Where each change of the state is published as a ``sequence`` event.
    :param definitions: The sequences offered.
    :param clock: The monotonic clock waits and time-outs are measured on.
    :param interval: Real seconds between two looks while a step waits.
    :param prompt_timeout: Real seconds a question waits for its answer.
    """

    def __init__(
        self,
        executor: Executor,
        recorder: Recorder,
        *,
        calibration: Calibration | None = None,
        live: LiveHub | None = None,
        definitions: tuple[Definition, ...] = DEFINITIONS,
        clock: Callable[[], float] = time.monotonic,
        interval: float = 0.05,
        prompt_timeout: float = 600.0,
    ) -> None:
        self.executor = executor
        self.recorder = recorder
        self.calibration = calibration
        self.live = live
        self.definitions = {d.name: d for d in definitions}
        self.clock = clock
        self.interval = interval
        self.prompt_timeout = prompt_timeout
        self._lock = threading.Lock()
        self._abort = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._numbers = itertools.count(1)
        self._prompts = itertools.count(1)
        self._run: _Run | None = None
        self._closed = False

    # -- the state ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether a sequence runs (its cleanup included)."""
        return not self._idle.is_set()

    def state_json(self) -> dict[str, Any]:
        """The sequence running, or the last one: its steps, its question, its results.

        ``total`` is how many steps its definition has (its cleanup aside), so
        ``steps``, the ones begun so far, measure its progress.
        """
        with self._lock:
            run = self._run
            if run is None:
                return {"state": "idle", "sequence": None}
            return {
                "state": run.state if self.running else "idle",
                "sequence": run.definition.name,
                "number": run.number,
                "params": dict(run.params),
                "outcome": run.outcome,
                "detail": run.detail,
                "prompt": None if run.prompt is None else dict(run.prompt),
                "total": len(run.definition.steps),
                "steps": [dict(entry) for entry in run.log],
                "cleanup": [dict(entry) for entry in run.cleanup_log],
                "results": dict(run.results),
                "run": run.run_id,
                "elapsed_s": round(
                    (self.clock() - run.started) if self.running else run.elapsed, 3
                ),
            }

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no sequence runs; whether none does."""
        return self._idle.wait(timeout)

    def _publish(self) -> None:
        """Publish the state as a ``sequence`` event."""
        if self.live is not None:
            self.live.publish("sequence", self.state_json())

    # -- the API -------------------------------------------------------------------

    def start(self, name: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Start sequence *name* with *params* on a thread of its own.

        :returns: :meth:`state_json` once it runs.
        :raises SequenceRefused: 404 for an unknown sequence, 400 for a
            parameter it does not take or out of its bounds, 409 while a
            sequence or a capture runs, 503 once serve is stopping.
        """
        definition = self.definitions.get(name)
        if definition is None:
            raise SequenceRefused(
                404,
                "unknown_sequence",
                f"no sequence {name!r}: one of {', '.join(self.definitions)}",
            )
        values = _params(definition, params or {})
        with self._lock:
            if self._closed:
                raise SequenceRefused(503, "closing", "serve is stopping")
            if self.running:
                raise SequenceRefused(409, "sequence_running", "a sequence is running already")
            if self.recorder.state != "idle":
                raise SequenceRefused(409, "capture_running", "a capture is running: stop it first")
            if self.recorder.session.foreign_window is not None:
                raise SequenceRefused(
                    409, "window_open", "a window serve did not open is open: it is left alone"
                )
            run = _Run(next(self._numbers), definition, values)
            run.started = self.clock()
            self._run = run
            self._abort.clear()
            self._idle.clear()
        threading.Thread(
            target=self._main, args=(run,), name="kleidos-meter-sequence", daemon=True
        ).start()
        self._publish()
        return self.state_json()

    def abort(self) -> dict[str, Any]:
        """Abort the sequence running: its wait ends, its cleanup runs.

        :returns: :meth:`state_json`.
        :raises SequenceRefused: 409 when none runs.
        """
        if not self.running:
            raise SequenceRefused(409, "no_sequence", "no sequence is running")
        self._abort.set()
        return self.state_json()

    def answer(self, prompt: object, answer: object) -> dict[str, Any]:
        """Answer the question the running sequence asks.

        :param prompt: The question's ``id``, as the state names it.
        :param answer: One of its ``choices``.
        :returns: :meth:`state_json`.
        :raises SequenceRefused: 409 when no question waits or it is not
            *prompt*, 400 for an answer it does not offer.
        """
        with self._lock:
            run = self._run
            asked = None if run is None or not self.running else run.prompt
            if run is None or asked is None or asked["id"] != prompt:
                raise SequenceRefused(409, "no_prompt", "no such question is waiting")
            if answer not in asked["choices"]:
                raise SequenceRefused(
                    400, "bad_answer", f"answer one of {', '.join(asked['choices'])}"
                )
            run.answer = str(answer)
            run.answered.set()
        return self.state_json()

    def close(self, timeout: float = 120.0) -> None:
        """Serve is stopping: abort the sequence running, wait for its cleanup; again, nothing."""
        with self._lock:
            self._closed = True
        if self.running:
            self._abort.set()
        self._idle.wait(timeout)

    # -- the thread ------------------------------------------------------------------

    def _main(self, run: _Run) -> None:
        """Run the steps, then the cleanup, whatever happened."""
        try:
            outcome, detail = "done", ""
            try:
                for index, step in enumerate(run.definition.steps):
                    self._step(run, run.log, index, step, cleanup=False)
            except _StepFailed as failed:
                outcome, detail = "failed", failed.detail
            except _Ended as ended:
                outcome, detail = ended.outcome, ended.detail
            except Exception as error:  # noqa: BLE001  the line failed under a step, or a step's bug
                outcome, detail = "failed", _why(error)
            with self._lock:
                run.state, run.prompt = "cleanup", None
            self._publish()
            for index, step in enumerate(run.definition.cleanup):
                with contextlib.suppress(Exception):  # logged; the next cleanup step runs
                    self._step(run, run.cleanup_log, index, step, cleanup=True)
            if outcome == "done" and any(e["state"] == "failed" for e in run.cleanup_log):
                outcome, detail = "failed", "its cleanup did not hold"
            with self._lock:
                run.state, run.outcome, run.detail = "ended", outcome, detail
                run.elapsed = self.clock() - run.started
        finally:
            self._idle.set()
            self._publish()

    def _step(
        self, run: _Run, log: list[dict[str, Any]], index: int, step: Step, *, cleanup: bool
    ) -> None:
        """Run one step and log it; a step's failure is logged and raised."""
        entry: dict[str, Any] = {"index": index, "step": step.describe(), "state": "running"}
        began = self.clock()
        with self._lock:
            log.append(entry)
        self._publish()
        try:
            note = self._do(run, step, cleanup=cleanup)
        except _StepFailed as failed:
            self._log(entry, "failed", failed.detail, began, failed.errors)
            raise
        except _Ended as ended:
            self._log(entry, ended.outcome, ended.detail, began)
            raise
        except Exception as error:
            if not isinstance(error, MeterError):
                _LOG.exception("sequence %s: %s", run.definition.name, entry["step"])
            self._log(entry, "failed", _why(error), began, getattr(error, "errors", None))
            raise
        self._log(entry, "ok", note, began)

    def _log(
        self,
        entry: dict[str, Any],
        state: str,
        detail: str,
        began: float,
        errors: list[Any] | None = None,
    ) -> None:
        """Finish a step's log entry and publish it."""
        with self._lock:
            entry.update(state=state, detail=detail, elapsed_s=round(self.clock() - began, 3))
            if errors:
                entry["errors"] = errors_json(errors)
        self._publish()

    def _check_abort(self, *, cleanup: bool) -> None:
        """End the sequence when it was aborted (a cleanup is never aborted)."""
        if self._abort.is_set() and not cleanup:
            raise _Ended("aborted", "aborted")

    def _sleep_until(self, due: float, *, cleanup: bool) -> None:
        """Wait until *due* on the clock, a look every :attr:`interval` real seconds."""
        while self.clock() < due:
            self._check_abort(cleanup=cleanup)
            if cleanup:
                time.sleep(self.interval)  # an abort that is set must not hurry a cleanup
            else:
                self._abort.wait(self.interval)
        self._check_abort(cleanup=cleanup)

    def _do(self, run: _Run, step: Step, *, cleanup: bool) -> str:
        """Run one step; what it found, for its log entry."""
        self._check_abort(cleanup=cleanup)
        handler = _HANDLERS[type(step)]
        return handler(self, run, step, cleanup)

    # -- the steps ---------------------------------------------------------------------

    def _send(self, run: _Run, step: Send, cleanup: bool) -> str:
        """Send the message; a window it opens is the sequence's to close."""
        message = step.message.format_map(run.values)
        reply = self.executor.call(message)
        info = analyze(message)
        if any(p in WINDOW_OPENERS for p in info.patterns) and not reply.errors:
            run.window_open = True
        if "ABORt" in info.patterns:
            run.window_open = False
        if reply.errors:
            raise _StepFailed(f"{message}: the instrument refused it", reply.errors)
        return message

    def _query(self, run: _Run, step: Query, cleanup: bool) -> str:
        """Ask the query, keep its answer, and the value braces may name."""
        answer = self.executor.query_all([step.query])[0]
        fields = _fields(answer)
        with self._lock:
            run.kept[step.keep] = answer
            run.results[step.keep] = typed_fields(fields) if fields else answer
        if step.var is not None:
            value = answer if step.key is None else fields.get(step.key)
            if value is None:
                raise _StepFailed(f"{step.query} reports no {step.key}")
            run.values[step.var] = value
        return answer

    def _check(self, run: _Run, step: Check, cleanup: bool) -> str:
        """Check a field of a kept answer."""
        answer = run.kept.get(step.keep)
        if answer is None:
            raise _StepFailed(f"nothing was kept as {step.keep}")
        found = answer if step.key is None else _fields(answer).get(step.key)
        expected = step.value.format_map(run.values)
        where = step.keep if step.key is None else f"{step.keep} {step.key}"
        if found is None:
            if step.optional:
                return f"{where}: not reported"
            raise _StepFailed(f"{where}: not reported")
        held = found == expected if step.op == "eq" else expected in found.split(",")
        if not held:
            raise _StepFailed(f"{where} is {found}, not {step.op} {expected}")
        return f"{where} is {found}"

    def _wait(self, run: _Run, step: Wait, cleanup: bool) -> str:
        """Let the span pass on the clock."""
        seconds = step.span.seconds(run.params)
        self._sleep_until(self.clock() + seconds, cleanup=cleanup)
        return f"{seconds:g} s"

    def _wait_until(self, run: _Run, step: WaitUntil, cleanup: bool) -> str:
        """Poll the report until its field reads one of the values, or fail at the time-out."""
        timeout = step.timeout.seconds(run.params)
        deadline = self.clock() + timeout
        while True:
            answer = self.executor.query_all([step.query])[0]
            found = _fields(answer).get(step.key)
            if found in step.values:
                return f"{step.key}={found}"
            if self.clock() >= deadline:
                raise _StepFailed(f"{step.key} was still {found} after {timeout:g} s")
            self._sleep_until(min(deadline, self.clock() + step.poll), cleanup=cleanup)

    def _prompt(self, run: _Run, step: Prompt, cleanup: bool) -> str:
        """Ask the question and wait for its answer, an abort or the time-out."""
        text = step.text.format_map(run.values)
        with self._lock:
            run.prompt = {"id": next(self._prompts), "text": text, "choices": list(step.choices)}
            run.answer = None
            run.answered.clear()
            run.state = "prompt"
        self._publish()
        deadline = time.monotonic() + self.prompt_timeout
        try:
            while not run.answered.wait(self.interval):
                self._check_abort(cleanup=cleanup)
                if time.monotonic() >= deadline:
                    raise _Ended("declined", "the question went unanswered")
        finally:
            with self._lock:
                run.prompt, run.state = None, "running"
        if run.answer != step.proceed:
            raise _Ended("declined", f"answered {run.answer}")
        return f"answered {run.answer}"

    def _mark(self, run: _Run, step: MarkStep, cleanup: bool) -> str:
        """Mark the running capture."""
        try:
            answer = self.recorder.mark(step.label)
        except CaptureRefused as refused:
            raise _StepFailed(refused.detail, refused.errors) from None
        if answer["errors"]:
            raise _StepFailed(f"the mark {step.label} was refused")
        return step.label

    def _start_capture(self, run: _Run, step: StartCapture, cleanup: bool) -> str:
        """Start the capture through the recorder; its run is the sequence's."""
        seconds = None if step.seconds is None else step.seconds.seconds(run.params)
        try:
            state = self.recorder.start(
                step.profile, None, seconds, f"sequence {run.definition.name}"
            )
        except CaptureRefused as refused:
            raise _StepFailed(refused.detail, refused.errors) from None
        run.run_id = state["run"]
        with self._lock:
            run.results["run"] = state["run"]
        return f"run {state['run']}"

    def _finished(self, run: _Run) -> str:
        """The sequence's run, finished: its summary kept; fails unless it is complete."""
        meta = self.recorder.last_meta
        if meta is None or meta["id"] != run.run_id:
            raise _StepFailed("the sequence's capture left no run")
        summary = Run.summary_of(meta)
        with self._lock:
            run.results["capture"] = summary
        if not meta["complete"]:
            raise _StepFailed(f"run {meta['id']} is incomplete (ended by {meta['ended_by']})")
        return f"run {meta['id']} complete"

    def _wait_capture(self, run: _Run, step: WaitCapture, cleanup: bool) -> str:
        """Wait for the capture's end, which the recorder sees by itself."""
        timeout = step.timeout.seconds(run.params)
        deadline = self.clock() + timeout
        while not self.recorder.wait_idle(self.interval):
            self._check_abort(cleanup=cleanup)
            if self.clock() >= deadline:
                raise _StepFailed(f"the capture had not ended after {timeout:g} s")
        return self._finished(run)

    def _stop_capture(self, run: _Run, step: StopCapture, cleanup: bool) -> str:
        """Stop the sequence's capture if it still runs; as a step, check its run."""
        state = self.recorder.state_json()
        ours = run.run_id is not None and state["run"] == run.run_id
        if ours and state["state"] == "running":
            with contextlib.suppress(CaptureRefused):  # it ended meanwhile
                self.recorder.stop()
        self.recorder.wait_idle(60.0)
        if cleanup:
            return "stopped" if ours else "no capture of the sequence's runs"
        return self._finished(run)

    def _close_null(self, run: _Run, step: CloseNull, cleanup: bool) -> str:
        """Close a null window the sequence opened that is still open."""
        if not run.window_open:
            return "no window of the sequence's is open"
        status = _fields(self.executor.query_all(["SYST:STAT?"])[0])
        if status.get("sink") != "null":
            run.window_open = False
            return "the window had closed"
        self._send(run, Send("ABOR"), cleanup)
        return "closed it"

    def _calibration(self, run: _Run, step: CalibrationStep, cleanup: bool) -> str:
        """Verify, or restore and verify, the calibration against the registry."""
        if self.calibration is None:
            raise _StepFailed("serve was given no registry")
        if step.action == "verify":
            verdict = self.calibration.verify()["verdict"]
        else:
            _, body = self.calibration.restore()
            verdict = body["after"]
        with self._lock:
            run.results["calibration"] = verdict
        if not verdict["ok"]:
            raise _StepFailed(f"the calibration is {verdict['status']}")
        status: str = verdict["status"]
        return status


def _fields(answer: str) -> dict[str, str]:
    """A report's fields; none for an answer that is not a report."""
    return parse_fields(answer)


def _why(error: Exception) -> str:
    """What an exception says, or its type's name when it says nothing."""
    return str(error) or type(error).__name__


def _params(definition: Definition, given: Mapping[str, Any]) -> dict[str, int]:
    """The sequence's parameters: *given* checked, the defaults for the rest.

    :raises SequenceRefused: 400 for a parameter it does not take, not an
        integer, or out of its bounds.
    """
    known = {param.name: param for param in definition.params}
    unknown = sorted(set(given) - set(known))
    if unknown:
        takes = ", ".join(known) or "no parameter"
        raise SequenceRefused(
            400, "bad_parameter", f"{definition.name} takes {takes}, not {', '.join(unknown)}"
        )
    values: dict[str, int] = {}
    for name, param in known.items():
        value = given.get(name, param.default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise SequenceRefused(400, "bad_parameter", f"{name} is an integer")
        if not param.low <= value <= param.high:
            raise SequenceRefused(
                400, "bad_parameter", f"{name} is {param.low} to {param.high} {param.unit}"
            )
        values[name] = value
    return values


#: Each step's handler.
_HANDLERS: dict[type, Callable[[SequenceRunner, _Run, Any, bool], str]] = {
    Send: SequenceRunner._send,
    Query: SequenceRunner._query,
    Check: SequenceRunner._check,
    Wait: SequenceRunner._wait,
    WaitUntil: SequenceRunner._wait_until,
    Prompt: SequenceRunner._prompt,
    MarkStep: SequenceRunner._mark,
    StartCapture: SequenceRunner._start_capture,
    WaitCapture: SequenceRunner._wait_capture,
    StopCapture: SequenceRunner._stop_capture,
    CloseNull: SequenceRunner._close_null,
    CalibrationStep: SequenceRunner._calibration,
}
