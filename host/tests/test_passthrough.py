# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The SCPI passthrough: one client at a time on the line serve holds, byte for byte.

Most tests run serve's parts (the executor, the session, the API, the
passthrough) on the deterministic simulator, whose state is a function of
the bytes written to it alone, so a session through the passthrough can be
compared byte for byte with the same session on a second simulator brought
to the same state. The stalled client runs on the clock-driven simulator,
which streams; the process test runs ``kleidos-meter serve --sim`` itself.
"""

from __future__ import annotations

import io
import json
import logging
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import FakeClock
from kleidos_meter.client import Meter, SerialTransport
from kleidos_meter.panel import lockinfo, passthrough, serve
from kleidos_meter.panel.executor import (
    TIMEOUTS,
    Executor,
    ExecutorBusy,
    ExecutorClosed,
    LineHeld,
    LinkDown,
)
from kleidos_meter.panel.http import Api
from kleidos_meter.panel.live import LiveHub
from kleidos_meter.panel.passthrough import Passthrough, _Client
from kleidos_meter.panel.serve import Session
from kleidos_meter.protocol import Sample, parse_fields
from kleidos_meter.sim import SimulatedMeter

#: Real seconds a test waits for anything before it fails instead of hanging.
WAIT = 10.0

#: The name of the executor's thread: its writes are serve's, every other the client's.
EXECUTOR_THREAD = "kleidos-meter-executor"

#: What every change to the panel carries, and the loopback's Host.
HEADERS = {"Host": "127.0.0.1", "X-Kleidos-Meter": "1"}


def until(condition: Callable[[], bool], what: str) -> None:
    """Wait in real time until *condition* holds."""
    deadline = time.monotonic() + WAIT
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.002)


class Line:
    """A transport around a simulator that records who wrote what and what was read.

    An empty read of the deterministic simulator waits a millisecond, as a
    port's timeout would, so the executor's thread does not spin.
    """

    def __init__(self, sim: SimulatedMeter, *, swallow: tuple[str, ...] = ()) -> None:
        self.sim = sim
        #: Messages the instrument never answers: a write holding one is dropped.
        self.swallow = swallow
        self.writes: list[tuple[str, bytes]] = []
        self.read_bytes = bytearray()
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        with self._lock:
            self.writes.append((threading.current_thread().name, data))
        if any(text.encode() in data for text in self.swallow):
            return len(data)
        return self.sim.write(data)

    def read(self, size: int) -> bytes:
        chunk = self.sim.read(size)
        if chunk:
            with self._lock:
                self.read_bytes += chunk
        elif not self.sim.clocked:
            time.sleep(0.001)
        return chunk

    def close(self) -> None:
        self.sim.close()

    def written(self, *, by_executor: bool) -> list[bytes]:
        """The writes of the executor's thread, or of every other thread."""
        with self._lock:
            return [data for name, data in self.writes if (name == EXECUTOR_THREAD) == by_executor]


class Recording:
    """A client's transport that keeps every byte it wrote and read."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.wrote = bytearray()
        self.got = bytearray()

    def write(self, data: bytes) -> int:
        self.wrote += data
        written: int = self.inner.write(data)
        return written

    def read(self, size: int) -> bytes:
        chunk: bytes = self.inner.read(size)
        self.got += chunk
        return chunk

    def close(self) -> None:
        self.inner.close()


@dataclass
class Stack:
    """Serve's parts on a simulator: the executor, the session, the API, the passthrough."""

    line: Line
    executor: Executor
    session: Session
    api: Api
    live: LiveHub
    passthrough: Passthrough
    said: list[str] = field(default_factory=list)

    def ask(self, method: str, path: str, body: bytes = b"") -> Any:
        """One request to the API, as the panel's server hands it over."""
        return self.api.handle(method, path, HEADERS, body)


@contextmanager
def stack_on(
    sim: SimulatedMeter,
    *,
    clock: Callable[[], float] = time.monotonic,
    swallow: tuple[str, ...] = (),
    timeouts: dict[str, float] | None = None,
    **options: Any,
) -> Iterator[Stack]:
    """Serve's parts on *sim*, opened, with the passthrough listening on any free port.

    *swallow* names messages the instrument never answers once the session
    is open, *timeouts* the executor's, and *options* go to the passthrough.
    """
    line = Line(sim)
    executor = Executor(line, clock=clock, timeouts=timeouts)
    session = Session(executor)
    live = LiveHub()
    executor.add_listener(live.on_event)
    api = Api(session, live=live)
    said: list[str] = []
    executor.start()
    live.start()
    try:
        executor.wait_ready(WAIT)
        session.open()
        line.swallow = swallow
        tap = Passthrough(session, api, live=live, port=0, say=said.append, **options)
        api.passthrough = tap
        tap.start()
        try:
            yield Stack(line, executor, session, api, live, tap, said)
        finally:
            tap.close()
    finally:
        live.close()
        executor.close()


def attached(stack: Stack) -> bool:
    return stack.passthrough.state_json()["state"] == "attached" and stack.executor.handed_over


def released(stack: Stack) -> bool:
    return (
        stack.passthrough.state_json()["state"] == "listening"
        and stack.api.holder is None
        and not stack.executor.handed_over
    )


def client(stack: Stack) -> tuple[Meter, Recording]:
    """A ``Meter`` on the passthrough, as a QA run opens it."""
    transport = Recording(SerialTransport(stack.passthrough.url))
    return Meter(transport), transport


def session_of(meter: Meter) -> None:
    """What a QA run does: the readiness check, identity, a capture with a mark, its stop."""
    meter.wait_ready(timeout=WAIT)
    assert meter.idn().startswith("KLEIDOS,KLEIDOS-METER,")
    meter.set_profile("STATE")
    header = meter.start(["i_ua", "v_mv"])
    assert header
    meter.status()
    meter.mark("m1")
    stats = meter.stop()
    assert int(stats["samples"]) > 0
    meter.drain(quiet=0.2)


# -- transparency ---------------------------------------------------------------------


def test_a_client_gets_byte_for_byte_what_a_direct_session_gets() -> None:
    with stack_on(SimulatedMeter()) as stack:
        meter, through = client(stack)
        until(lambda: attached(stack), "the client to attach")
        session_of(meter)
        meter.close()
        until(lambda: released(stack), "the client to detach")
        serves = stack.line.written(by_executor=True)
        clients = stack.line.written(by_executor=False)
        assert b"".join(clients) == bytes(through.wrote)  # client to instrument, unchanged

    # The same session on a second simulator, brought to the first one's state
    # by the bytes serve wrote before the client attached.
    twin = SimulatedMeter()
    writes = stack.line.writes
    first_client = next(i for i, (name, _) in enumerate(writes) if name != EXECUTOR_THREAD)
    for _, data in writes[:first_client]:
        twin.write(data)
    twin.read(1 << 20)
    direct = Recording(Line(twin))
    session_of(Meter(direct))
    assert bytes(through.got) == bytes(direct.got)  # instrument to client, unchanged
    assert bytes(through.wrote) == bytes(direct.wrote)
    assert serves[-1] != clients[-1]  # serve wrote again once the client had gone


def test_the_line_is_taken_back_with_a_newline_and_a_sync_and_the_state_read() -> None:
    with stack_on(SimulatedMeter()) as stack:
        before = len(stack.line.written(by_executor=True))
        meter, _ = client(stack)
        until(lambda: attached(stack), "the client to attach")
        meter.send("*IDN?")
        meter.close()
        until(lambda: released(stack), "the client to detach")
        after = stack.line.written(by_executor=True)[before:]
        newline = after.index(b"\n")
        assert after[newline - 1] == b"SYST:STAT?;:SYST:ERR:ALL?\n"  # the attach's look
        assert after[newline + 1].startswith(b"KMSYNC")  # the sync that drains the line
        assert after[newline + 2] == b"SYST:STAT?;:SYST:ERR:ALL?\n"  # the state read again
        until(lambda: stack.executor.status is not None and stack.executor.polling, "a poll")
        assert stack.passthrough.last is not None
        assert stack.passthrough.last == {
            "state": "detached",
            "client": stack.passthrough.last["client"],
            "dropped": False,
            "lost_bytes": 0,
            "window": None,
        }
        assert stack.passthrough.attaches == 1
        assert any("attached: the panel watches" in line for line in stack.said)
        assert any("detached: the panel has the line again" in line for line in stack.said)


# -- the hold -------------------------------------------------------------------------


def test_while_a_client_holds_the_line_the_panel_answers_423_and_polls_nothing() -> None:
    with stack_on(SimulatedMeter()) as stack:
        meter, _ = client(stack)
        until(lambda: attached(stack), "the client to attach")
        holder = stack.api.holder
        assert holder is not None and holder.startswith("a passthrough client (127.0.0.1:")
        assert stack.executor.polling is False
        quiet = len(stack.line.written(by_executor=True))
        for method, path in (
            ("POST", "/api/scpi"),
            ("POST", "/api/abort"),
            ("POST", "/api/relays/lead"),
            ("GET", "/api/health"),
        ):
            answer = stack.ask(method, path, b"SYST:STAT?")
            assert answer.status == 423, path
            assert answer.body["error"] == "held" and answer.body["holder"] == holder
        status = stack.ask("GET", "/api/status")
        assert status.status == 200 and status.body["held"] == holder
        state = status.body["passthrough"]
        assert state["state"] == "attached" and state["client"] == holder[22:-1]
        assert state["url"] == stack.passthrough.url and state["queued_bytes"] == 0
        meter.send("*IDN?")
        time.sleep(0.3)
        assert len(stack.line.written(by_executor=True)) == quiet  # serve wrote nothing
        assert stack.ask("GET", "/api/status").body["passthrough"]["to_client_bytes"] > 0
        meter.close()
        until(lambda: released(stack), "the client to detach")
        assert stack.ask("GET", "/api/status").body["passthrough"]["client"] is None


def test_a_message_sent_while_the_line_is_handed_over_is_refused_423() -> None:
    # The hand-over and the API's hold are two steps: a route that passed the
    # hold's check before the hand-over still meets the executor's refusal.
    with stack_on(SimulatedMeter()) as stack:
        stack.executor.polling = False
        until(lambda: stack.executor.idle, "the line to go quiet")
        stack.executor.hand_over(lambda chunk: None)
        answer = stack.ask("GET", "/api/health")
        assert answer.status == 423 and answer.body["holder"] == "a passthrough client"
        with pytest.raises(LineHeld):
            stack.executor.submit("*IDN?")
        stack.api.hold("a passthrough client (127.0.0.1:1)")
        answer = stack.ask("POST", "/api/abort")
        assert (
            answer.status == 423 and answer.body["holder"] == "a passthrough client (127.0.0.1:1)"
        )
        stack.executor.take_back()
        stack.api.release()
        stack.executor.polling = True
        assert stack.executor.call("*IDN?", wait=WAIT).units[0].startswith("KLEIDOS")


def test_a_second_client_is_refused_and_the_first_keeps_the_line() -> None:
    with stack_on(SimulatedMeter()) as stack:
        meter, _ = client(stack)
        until(lambda: attached(stack), "the first client to attach")
        second = socket.create_connection(("127.0.0.1", stack.passthrough.port), timeout=WAIT)
        try:
            assert second.recv(1024) == b""  # closed with nothing written
        finally:
            second.close()
        until(lambda: stack.passthrough.refusals == 1, "the refusal to be counted")
        assert any("holds the line" in line for line in stack.said)
        assert meter.idn().startswith("KLEIDOS,KLEIDOS-METER,")
        meter.close()
        until(lambda: released(stack), "the client to detach")


# -- refusals -------------------------------------------------------------------------


def refused(stack: Stack, reason: str) -> None:
    """A client is closed with nothing written, for *reason*, and the panel has the line."""
    conn = socket.create_connection(("127.0.0.1", stack.passthrough.port), timeout=WAIT)
    try:
        assert conn.recv(1024) == b""
    finally:
        conn.close()
    until(lambda: stack.passthrough.refusals == 1, "the refusal")
    assert any(reason in line for line in stack.said), stack.said
    until(lambda: released(stack), "the panel to have the line")
    assert stack.executor.polling is True


def test_no_client_attaches_while_a_sequence_runs() -> None:
    with stack_on(SimulatedMeter()) as stack:
        stack.passthrough.sequences = SimpleNamespace(running=True)  # type: ignore[assignment]  # a stand-in
        refused(stack, "a measurement sequence is running")


def test_no_client_attaches_while_a_pin_map_is_applied() -> None:
    with stack_on(SimulatedMeter()) as stack, stack.api.pins._changing():
        refused(stack, "a pin map is being applied")


def test_no_client_attaches_while_serves_capture_runs() -> None:
    with stack_on(SimulatedMeter()) as stack:
        stack.passthrough.recorder = SimpleNamespace(state="running")  # type: ignore[assignment]  # a stand-in
        refused(stack, "serve's capture is running")


def test_no_client_attaches_while_a_window_serve_opened_is_open() -> None:
    with stack_on(SimulatedMeter()) as stack:
        assert stack.executor.call("SENS:PROF STATE;:INIT", wait=WAIT).errors == []
        refused(stack, "a stream window serve opened is open")


def test_a_window_serve_found_open_lets_a_client_attach() -> None:
    sim = SimulatedMeter()
    sim.write(b"SENS:PROF STATE;:INIT\n")
    sim.read(1 << 20)
    with stack_on(sim) as stack:
        assert stack.session.foreign_window is not None
        meter, _ = client(stack)
        until(lambda: attached(stack), "the client to attach")
        meter.close()
        until(lambda: released(stack), "the client to detach")


def test_an_attach_waits_for_the_message_in_flight_and_is_refused_after_its_wait() -> None:
    clock = FakeClock()
    with stack_on(SimulatedMeter(), clock=clock, swallow=("*OPC?",), attach_wait=0.2) as stack:
        stack.executor.submit("*OPC?", timeout=1.0)
        until(lambda: stack.executor.in_flight is not None, "the message in flight")
        refused(stack, "'*OPC?' did not go quiet within 0.2 s")
        clock.sleep(2.0)  # it times out, and the line is in step again
        until(lambda: stack.executor.link_up, "the sync")


def test_an_attach_is_refused_when_the_instrument_does_not_answer() -> None:
    with stack_on(SimulatedMeter(), timeouts={**TIMEOUTS, "default": 0.5}) as stack:
        stack.executor.polling = False
        until(lambda: stack.executor.idle, "the line to go quiet")
        stack.line.swallow = ("SYST:STAT?",)
        refused(stack, "the instrument could not be readied")


def test_an_attach_is_refused_when_the_line_is_down() -> None:
    clock = FakeClock()
    with stack_on(SimulatedMeter(), clock=clock, swallow=("*OPC?", "KMSYNC")) as stack:
        stack.executor.submit("*OPC?", timeout=0.5)  # times out, and no sync is answered
        until(lambda: stack.executor.in_flight is not None, "the message in flight")
        clock.sleep(1.0)
        until(lambda: not stack.executor.link_up, "the sync to start")
        clock.sleep(20.0)
        until(lambda: stack.executor.link_down, "the line to go down")
        refused(stack, "the instrument is not answering")
        stack.line.swallow = ()
        clock.sleep(2.0)  # the sync in flight times out, and the next is answered
        until(lambda: stack.executor.link_up, "the sync")


def test_the_settings_serve_changed_are_written_back_before_the_client_attaches() -> None:
    with stack_on(SimulatedMeter()) as stack:
        found = stack.session.shaping.snapshot["profile"]
        other = "SLEEP" if found != "SLEEP" else "STATE"
        assert stack.executor.call(f"SENS:PROF {other}", wait=WAIT).errors == []
        meter, _ = client(stack)
        until(lambda: attached(stack), "the client to attach")
        assert meter.query("SENS:PROF?") == found
        assert any(f"wrote back profile {found} (it was {other})" in line for line in stack.said)
        meter.close()
        until(lambda: released(stack), "the client to detach")


# -- what the client leaves -------------------------------------------------------------


def test_a_window_the_client_left_open_is_shown_and_left_open_on_the_way_out() -> None:
    with stack_on(SimulatedMeter()) as stack:
        meter, _ = client(stack)
        until(lambda: attached(stack), "the client to attach")
        meter.set_profile("STATE")
        meter.start()
        meter.close()
        until(lambda: released(stack), "the client to detach")
        last = stack.passthrough.last
        assert last is not None and last["window"]["sink"] == "stream"
        assert stack.session.foreign_window is not None
        assert any("the client left a stream window open" in line for line in stack.said)
        notes, clean = stack.session.close()
        assert clean and "left the stream window open, as serve found it" in notes


def test_a_client_state_that_cannot_be_read_is_reported_and_the_panel_gets_the_line() -> None:
    with stack_on(SimulatedMeter(), timeouts={**TIMEOUTS, "default": 0.3}) as stack:
        meter, _ = client(stack)
        until(lambda: attached(stack), "the client to attach")
        stack.line.swallow = ("SYST:STAT?",)
        meter.close()
        until(lambda: any("could not read the state" in line for line in stack.said), "it")
        until(lambda: released(stack), "the panel to have the line")
        stack.line.swallow = ()


def test_a_stalled_client_is_dropped_and_the_reader_goes_on() -> None:
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep)
    with stack_on(sim, clock=clock, max_queued=64 * 1024) as stack:
        samples = [0]

        def count(event: object) -> None:
            if isinstance(event, Sample):
                samples[0] += 1

        stack.executor.add_listener(count)
        stalled = socket.socket()
        stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stalled.connect(("127.0.0.1", stack.passthrough.port))
        try:
            until(lambda: attached(stack), "the client to attach")
            stalled.sendall(b"SENS:PROF STATE\nINIT\n")  # and never reads
            until(lambda: stack.passthrough.drops == 1, "the stalled client to be dropped")
            seen = samples[0]
            until(lambda: samples[0] > seen + 100, "the reader to go on")
            until(lambda: released(stack), "the panel to have the line")
        finally:
            stalled.close()
        last = stack.passthrough.last
        assert last is not None and last["dropped"] is True and last["lost_bytes"] > 64 * 1024
        assert last["window"]["sink"] == "stream"
        assert any("dropped passthrough client" in line for line in stack.said)
        assert stack.ask("GET", "/api/status").body["passthrough"]["drops"] == 1


def test_the_drop_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=passthrough.__name__)
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep)
    with stack_on(sim, clock=clock, max_queued=16 * 1024) as stack:
        stalled = socket.socket()
        stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stalled.connect(("127.0.0.1", stack.passthrough.port))
        try:
            until(lambda: attached(stack), "the client to attach")
            stalled.sendall(b"SENS:PROF STATE\nINIT\n")
            until(lambda: stack.passthrough.drops == 1, "the drop")
        finally:
            stalled.close()
        until(lambda: released(stack), "the panel to have the line")
    assert any("more than 16384 bytes pile up" in record.message for record in caplog.records)


def test_the_live_view_watches_the_clients_capture() -> None:
    with stack_on(SimulatedMeter()) as stack:
        seen: list[object] = []
        stack.executor.add_listener(seen.append)
        meter, _ = client(stack)
        until(lambda: attached(stack), "the client to attach")
        meter.set_profile("STATE")
        meter.start()
        meter.status()
        meter.stop()
        assert any(isinstance(event, Sample) for event in seen)
        assert not [e for e in seen if type(e).__name__ == "StrayLine"]  # responses are its own
        meter.close()
        until(lambda: released(stack), "the client to detach")
        kinds = [
            e.data.get("state") for e in _events(stack.live) if e.data.get("kind") == "passthrough"
        ]
        assert kinds == ["attached", "detached"]


@dataclass
class _Event:
    kind: str
    data: dict[str, Any]


def _events(hub: LiveHub) -> list[_Event]:
    """Every event the hub's ring holds, decoded."""
    hub.flush()
    events = []
    for entry in list(hub._ring):
        text = entry.frame.decode()
        kind = re.search(r"^event: (\w+)$", text, re.M)
        data = re.search(r"^data: (.*)$", text, re.M)
        assert kind is not None and data is not None
        events.append(_Event(kind.group(1), json.loads(data.group(1))))
    return events


# -- stopping ---------------------------------------------------------------------------


def test_closing_the_passthrough_detaches_its_client_and_twice_does_nothing() -> None:
    with stack_on(SimulatedMeter()) as stack:
        conn = socket.create_connection(("127.0.0.1", stack.passthrough.port), timeout=WAIT)
        try:
            until(lambda: attached(stack), "the client to attach")
            stack.passthrough.close()
            assert conn.recv(1024) == b""
            assert released(stack)
            stack.passthrough.close()
        finally:
            conn.close()
        late = socket.socket()
        with pytest.raises(OSError):
            late.connect(("127.0.0.1", stack.passthrough.port))
        late.close()


def test_a_client_that_arrives_as_serve_stops_is_refused() -> None:
    """Serve stops while the client waits for the line: the wait refuses it."""
    clock = FakeClock()
    with stack_on(SimulatedMeter(), clock=clock, swallow=("*OPC?",), attach_wait=WAIT) as stack:
        waiting = threading.Event()
        wait_quiet = stack.passthrough._wait_quiet

        def entered() -> None:
            waiting.set()
            wait_quiet()

        # Closed before the wait began, the attach's own check would refuse the
        # client instead, and the wait's refusal would go untested.
        stack.passthrough._wait_quiet = entered  # type: ignore[method-assign]  # the wait seen
        stack.executor.submit("*OPC?", timeout=1.0)
        until(lambda: stack.executor.in_flight is not None, "the message in flight")
        conn = socket.create_connection(("127.0.0.1", stack.passthrough.port), timeout=WAIT)
        try:
            until(waiting.is_set, "the client waiting for the line")
            stack.passthrough.close()
            assert conn.recv(1024) == b""
        finally:
            conn.close()
        assert any("serve is stopping" in line for line in stack.said)
        clock.sleep(2.0)
        until(lambda: stack.executor.link_up, "the sync")


def test_a_stop_between_the_accept_and_the_attach_refuses_the_client() -> None:
    """Serve began to stop after the client was taken: it is refused before the line is touched."""
    with stack_on(SimulatedMeter()) as stack:
        hold = stack.api.hold

        def stopping(holder: str) -> None:
            stack.passthrough._closing.set()
            hold(holder)

        stack.api.hold = stopping  # type: ignore[method-assign]  # a stop staged
        conn = socket.create_connection(("127.0.0.1", stack.passthrough.port), timeout=WAIT)
        try:
            assert conn.recv(1024) == b""
        finally:
            conn.close()
        until(lambda: released(stack), "the panel to have the line")
        assert stack.passthrough.attaches == 0 and stack.passthrough.refusals == 1
        assert any(line.endswith(": serve is stopping") for line in stack.said), stack.said
        assert stack.line.written(by_executor=False) == []
        stack.passthrough._closing.clear()  # the stop was staged: the close at the end is real


def test_a_line_with_no_end_is_judged_and_forwarded_once_it_is_longer_than_a_message() -> None:
    """What a client writes with no newline is held to 4096 bytes, then goes as it came: the
    instrument says what is wrong with it, and serve does not hold it for ever."""
    with stack_on(SimulatedMeter()) as stack:
        conn = socket.create_connection(("127.0.0.1", stack.passthrough.port), timeout=WAIT)
        try:
            until(lambda: attached(stack), "the client to attach")
            conn.sendall(b"*IDN?" + b" " * 4091)  # 4096 bytes: held, waiting for its end
            until(
                lambda: stack.passthrough.state_json().get("from_client_bytes") == 4096,
                "the bytes to be held",
            )
            assert stack.line.written(by_executor=False) == []
            conn.sendall(b" ")
            until(lambda: stack.line.written(by_executor=False) != [], "them to be forwarded")
            assert stack.line.written(by_executor=False) == [b"*IDN?" + b" " * 4092]
            conn.sendall(b"\nSYST:ERR?\n")
            assert conn.recv(1024).startswith(b'-363,"Input buffer overrun')
        finally:
            conn.close()


def test_a_client_whose_bytes_cannot_be_written_is_detached() -> None:
    with stack_on(SimulatedMeter()) as stack:
        conn = socket.create_connection(("127.0.0.1", stack.passthrough.port), timeout=WAIT)
        try:
            until(lambda: attached(stack), "the client to attach")
            original = stack.line.write

            def broken(data: bytes) -> int:
                if threading.current_thread().name != EXECUTOR_THREAD:
                    raise OSError("the port failed")
                return original(data)

            stack.line.write = broken  # type: ignore[method-assign]  # a fault planted
            conn.sendall(b"*IDN?\n")
            assert conn.recv(1024) == b""
            until(lambda: released(stack), "the panel to have the line")
        finally:
            conn.close()


# -- the parts ------------------------------------------------------------------------


def test_the_executor_hands_over_only_a_quiet_line_and_routes_records_only() -> None:
    line = Line(SimulatedMeter())
    executor = Executor(line)
    executor.start()
    try:
        executor.wait_ready(WAIT)
        executor.polling = False
        until(lambda: executor.idle, "the line to go quiet")
        line.swallow = ("*OPC?",)
        executor.submit("*OPC?", timeout=0.3)
        with pytest.raises(ExecutorBusy):
            executor.hand_over(lambda chunk: None)
        until(lambda: executor.idle, "the time-out and the sync after it")
        chunks: list[bytes] = []

        def failing(chunk: bytes) -> None:
            chunks.append(chunk)
            raise RuntimeError("a bug in the tap")

        executor.hand_over(failing)
        assert executor.handed_over
        with pytest.raises(ExecutorBusy):
            executor.hand_over(chunks.append)
        executor.forward(b"*IDN?\n")
        until(lambda: bool(chunks), "the answer to reach the tap")
        assert b"".join(chunks).startswith(b"KLEIDOS,KLEIDOS-METER,")
        executor.take_back()
        executor.take_back()  # once is enough
        with pytest.raises(LineHeld):
            executor.forward(b"*IDN?\n")
        executor.polling = True
        assert executor.call("*IDN?", wait=WAIT).units[0].startswith("KLEIDOS")
    finally:
        executor.close()
    with pytest.raises(ExecutorClosed):
        executor.hand_over(chunks.append)


class GatedLine(Line):
    """A :class:`Line` whose next read, once armed, waits until the test lets it go.

    It stands for a port's read that was waiting for bytes when the line was
    handed over: the answer to the client's first message arrives in it.
    """

    def __init__(self, sim: SimulatedMeter) -> None:
        super().__init__(sim)
        self.armed = threading.Event()
        self.waiting = threading.Event()
        self.go = threading.Event()

    def read(self, size: int) -> bytes:
        if self.armed.is_set():
            self.armed.clear()
            self.waiting.set()
            self.go.wait(WAIT)
        return super().read(size)


def test_an_answer_read_across_the_hand_over_reaches_the_client() -> None:
    line = GatedLine(SimulatedMeter())
    executor = Executor(line)
    executor.start()
    try:
        executor.wait_ready(WAIT)
        executor.polling = False
        until(lambda: executor.idle, "the line to go quiet")
        line.armed.set()
        assert line.waiting.wait(WAIT), "the thread never read again"
        chunks: list[bytes] = []
        executor.hand_over(chunks.append)  # while the thread's read waits
        executor.forward(b"*IDN?\n")
        line.go.set()
        until(lambda: b"\n" in b"".join(chunks), "the client's answer")
        assert b"".join(chunks).startswith(b"KLEIDOS,KLEIDOS-METER,")
    finally:
        executor.close()


def test_a_line_that_is_down_is_never_handed_over() -> None:
    executor = Executor(Line(SimulatedMeter()))
    executor._down = True
    with pytest.raises(LinkDown):
        executor.hand_over(lambda chunk: None)


def test_a_clients_queue_drops_it_past_its_limit_and_counts_what_it_lost() -> None:
    left, right = socket.socketpair()
    try:
        queue = _Client(left, "127.0.0.1:1", 10)
        queue.tap(b"12345")
        assert queue.queued == 5 and not queue.dropped
        queue.tap(b"123456")
        assert queue.dropped and queue.lost == 11 and queue.queued == 0
        queue.tap(b"xyz")
        assert queue.lost == 14
        queue.write_loop()  # ended: returns at once
        assert right.recv(16) == b""
    finally:
        left.close()
        right.close()


def test_a_clients_writer_stops_when_its_socket_fails() -> None:
    left, right = socket.socketpair()
    queue = _Client(left, "127.0.0.1:1", 1 << 20)
    queue.tap(b"ab")
    right.close()
    left.close()
    queue.write_loop()
    queue.tap(b"cd")  # ended, not dropped: nothing is counted
    assert queue.lost == 0 and not queue.dropped


# -- serve ----------------------------------------------------------------------------


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runtime directory of the test's own, for the notes of who holds a port."""
    directory = tmp_path / "run"
    directory.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(directory))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return directory


@contextmanager
def serving(argv: list[str], **kwargs: Any) -> Iterator[tuple[io.StringIO, list[int]]]:
    """``serve`` running in a thread until the block ends; its output and status."""
    stop = threading.Event()
    out, err = io.StringIO(), io.StringIO()
    status: list[int] = []
    thread = threading.Thread(
        target=lambda: status.append(serve.main(argv, stop=stop, out=out, err=err, **kwargs))
    )
    thread.start()
    try:
        until(
            lambda: "holding the port" in out.getvalue() or bool(status), "serve to hold the port"
        )
        yield out, status
    finally:
        stop.set()
        thread.join(WAIT)


def post(url: str, body: bytes) -> int:
    headers = {"X-Kleidos-Meter": "1"}
    request = urllib.request.Request(url, body, headers, method="POST")  # noqa: S310  see below
    try:
        # S310: the test's own server on the loopback, over http.
        with urllib.request.urlopen(request, timeout=WAIT) as response:  # noqa: S310
            status: int = response.status
            return status
    except urllib.error.HTTPError as error:
        error.close()
        return error.code


def test_serve_gives_the_line_to_a_meter_on_its_passthrough(runtime: Path) -> None:
    argv = ["--sim", "--http", "0", "--scpi-passthrough", "0"]
    with serving(argv) as (out, status):
        text = out.getvalue()
        url = re.search(r"SCPI passthrough at (socket://127\.0\.0\.1:\d+)", text)
        api = re.search(r"panel API at (http://127\.0\.0\.1:\d+/api/)", text)
        assert url is not None and api is not None
        meter = Meter(SerialTransport(url.group(1)))
        try:
            meter.wait_ready(timeout=WAIT)
            assert meter.idn().startswith("KLEIDOS,KLEIDOS-METER,")
            assert post(api.group(1) + "capture/start", b'{"profile": "STATE"}') == 423
            meter.set_profile("STATE")
            meter.start(["i_ua"])
            time.sleep(0.3)
            meter.status()
            stats = meter.stop()
            assert int(stats["samples"]) > 0 and meter.capture.channel("i_ua")
        finally:
            meter.close()
        until(lambda: "detached" in out.getvalue(), "the client to detach")
        assert post(api.group(1) + "scpi", b"SYST:STAT?") == 200
    assert status == [0]
    lines = out.getvalue()
    assert "attached: the panel watches until it leaves" in lines


def test_the_passthrough_port_defaults_to_5025_and_is_checked() -> None:
    parser = serve.build_parser()
    assert parser.parse_args(["--sim"]).scpi_passthrough is None
    assert parser.parse_args(["--sim", "--scpi-passthrough"]).scpi_passthrough == 5025
    assert parser.parse_args(["--scpi-passthrough", "6000", "--sim"]).scpi_passthrough == 6000
    with pytest.raises(SystemExit):
        parser.parse_args(["--sim", "--scpi-passthrough", "70000"])


def test_a_passthrough_port_that_is_taken_ends_serve_with_status_1(runtime: Path) -> None:
    taken = socket.create_server(("127.0.0.1", 0))
    try:
        port = taken.getsockname()[1]
        err = io.StringIO()
        status = serve.main(
            ["--sim", "--http", "0", "--scpi-passthrough", str(port)], out=io.StringIO(), err=err
        )
    finally:
        taken.close()
    assert status == 1
    assert err.getvalue().startswith(f"# cannot listen on 127.0.0.1 port {port}:")


def test_the_note_a_held_port_leaves_names_the_passthrough(runtime: Path) -> None:
    """Whoever finds the port held is told the URL the line can be had at."""
    port = "/dev/cu.fake-meter"

    def opened(_: str) -> SimulatedMeter:
        return SimulatedMeter(clocked=True)

    argv = ["--port", port, "--http", "0", "--scpi-passthrough", "0"]
    with serving(argv, open_transport=opened) as (out, status):
        url = re.search(r"SCPI passthrough at (socket://\S+)", out.getvalue())
        assert url is not None
        note = lockinfo.read_holder(port)
        assert note is not None and note["passthrough"] == url.group(1)
        assert url.group(1) in lockinfo.describe(note)
    assert status == [0]


def test_the_holders_note_names_the_passthrough() -> None:
    record = {"pid": 7, "panel": "http://127.0.0.1:8040/", "passthrough": "socket://127.0.0.1:5025"}
    assert lockinfo.describe(record) == (
        "another process (pid 7, panel http://127.0.0.1:8040/, passthrough socket://127.0.0.1:5025)"
    )


def test_a_hello_carries_the_last_passthrough_state() -> None:
    hub = LiveHub(epoch="abc")
    hub.publish("status", {"kind": "passthrough", "state": "attached", "client": "127.0.0.1:9"})
    first = hub.subscribe()._first.decode()
    hello = json.loads(first.split("data: ")[1].split("\n")[0])
    assert hello["passthrough"] == {
        "kind": "passthrough",
        "state": "attached",
        "client": "127.0.0.1:9",
    }


def test_a_client_that_resets_its_connection_is_detached() -> None:
    with stack_on(SimulatedMeter()) as stack:
        conn = socket.create_connection(("127.0.0.1", stack.passthrough.port), timeout=WAIT)
        until(lambda: attached(stack), "the client to attach")
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        conn.close()  # a reset, not an orderly close
        until(lambda: released(stack), "the panel to have the line")


def test_a_listener_that_fails_is_logged_and_stops_accepting(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=passthrough.__name__)
    with stack_on(SimulatedMeter()) as stack:
        accepter = stack.passthrough._accepter
        assert accepter is not None
        stack.passthrough._listener.close()
        accepter.join(WAIT)
        assert not accepter.is_alive()
    assert any("stopped accepting" in record.message for record in caplog.records)


def test_a_listener_that_fails_as_serve_stops_is_no_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An accept the close cut short is how the loop ends, not a fault to log."""
    caplog.set_level(logging.ERROR, logger=passthrough.__name__)
    with stack_on(SimulatedMeter()) as stack:
        tap = Passthrough(stack.session, stack.api, port=0)

        class Closing:
            """A listener whose accept fails as the close begins."""

            def accept(self) -> tuple[socket.socket, tuple[str, int]]:
                tap._closing.set()
                raise OSError("the listener was closed")

        real, tap._listener = tap._listener, Closing()  # type: ignore[assignment]  # a fault planted
        try:
            tap._accept_loop()
        finally:
            real.close()
    assert caplog.records == []


def test_a_passthrough_with_no_hub_and_no_output_still_refuses_and_closes() -> None:
    with stack_on(SimulatedMeter()) as stack:
        bare = Passthrough(stack.session, stack.api, port=0)
        bare.close()  # never started: nothing to join
        bare = Passthrough(stack.session, stack.api, port=0)
        bare.sequences = SimpleNamespace(running=True)  # type: ignore[assignment]  # a stand-in
        bare.start()
        try:
            conn = socket.create_connection(("127.0.0.1", bare.port), timeout=WAIT)
            try:
                assert conn.recv(1024) == b""
            finally:
                conn.close()
            until(lambda: bare.refusals == 1, "the refusal")
        finally:
            bare.close()


# -- a browser is not a client --------------------------------------------------------

#: What ``fetch("http://127.0.0.1:5025/", {method: "POST", mode: "no-cors",
#: body})`` sends from any web page: no preflight, the body's lines after the headers.
BROWSER_POST = (
    b"POST / HTTP/1.1\r\n"
    b"Host: 127.0.0.1:5025\r\n"
    b"User-Agent: Mozilla/5.0\r\n"
    b"Origin: http://evil.example\r\n"
    b"Content-Type: text/plain;charset=UTF-8\r\n"
    b"Content-Length: 21\r\n"
    b"\r\n"
    b"\nROUT:LEAD OPEN,FORC\n"
)


def path_of(stack: Stack) -> str:
    """The relays' path, as the panel reads it once the line is its own again."""
    (answer,) = stack.executor.query_all(["ROUT:STAT?"], wait=WAIT)
    return parse_fields(answer)["path"]


@pytest.mark.parametrize(
    "request_bytes",
    [
        BROWSER_POST,
        BROWSER_POST.replace(b"POST / HTTP/1.1", b"PUT /x HTTP/1.0"),
        # A request line split across two writes, and one a header line follows.
        b"POST / HT",
        b"*IDN?\nHost: 127.0.0.1:5025\r\nROUT:LEAD OPEN,FORC\n",
    ],
    ids=["browser-post", "put", "split", "header-after-scpi"],
)
def test_a_browser_request_never_reaches_the_instrument(request_bytes: bytes) -> None:
    """A cross-site request from a web page reaches the passthrough as an HTTP
    request whose body lines are program messages; serve drops the client at
    the first HTTP line and forwards nothing from it on."""
    with stack_on(SimulatedMeter()) as stack:
        assert path_of(stack) == "shunt"
        conn = socket.create_connection(("127.0.0.1", stack.passthrough.port), timeout=WAIT)
        try:
            until(lambda: attached(stack), "the client to attach")
            if request_bytes == b"POST / HT":
                conn.sendall(request_bytes)
                time.sleep(0.2)
                conn.sendall(b"TP/1.1\r\nHost: x\r\n\r\nROUT:LEAD OPEN,FORC\n")
            else:
                conn.sendall(request_bytes)
            until(lambda: released(stack), "the client to be dropped")
        finally:
            conn.close()
        forwarded = b"".join(stack.line.written(by_executor=False))
        assert b"ROUT:LEAD" not in forwarded
        assert b"HTTP/" not in forwarded and b"Host:" not in forwarded
        assert path_of(stack) == "shunt"
        assert any("HTTP" in line for line in stack.said), stack.said
