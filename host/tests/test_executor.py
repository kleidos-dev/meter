# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's executor: one thread, a FIFO, one message in flight.

Against the clock-driven simulator on a fake clock (every read that finds
nothing lets 50 ms pass), and against :class:`ScriptedLine`, a line whose
answers the test writes, for the paths a well-behaved instrument never
takes: a late answer, silence, a fragment, a port that fails.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Any

import pytest
from conftest import FakeClock
from kleidos_meter.client import MeterError, MeterTimeout
from kleidos_meter.panel import executor as executor_module
from kleidos_meter.panel.executor import (
    MAX_MESSAGE,
    TIMEOUTS,
    DeviceErrors,
    Event,
    Executor,
    ExecutorBusy,
    ExecutorClosed,
    LinkChanged,
    LinkDown,
    MessageRefused,
    Rebooted,
    Reply,
    Status,
    StrayLine,
    analyze,
    check_message,
    pack_units,
)
from kleidos_meter.panel.schema import resolve_unit
from kleidos_meter.panel.shaping import changed_settings
from kleidos_meter.protocol import ErrorEntry, LineRouter, Response, Sample
from kleidos_meter.sim import SimulatedMeter

#: Real seconds a test waits for anything before it fails instead of hanging.
WAIT = 10.0


def clocked(**knobs: Any) -> tuple[SimulatedMeter, FakeClock]:
    """A clock-driven simulator on a fake clock that its own reads move."""
    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep, **knobs)
    return sim, clock


@contextmanager
def running(transport: object, clock: Callable[[], float], **knobs: Any) -> Iterator[Executor]:
    """An executor on *transport*, started and synced; closed after."""
    executor = Executor(transport, clock=clock, **knobs)  # type: ignore[arg-type]  # the knobs are Executor's
    executor.start()
    try:
        executor.wait_ready(WAIT)
        yield executor
    finally:
        executor.close()


def until(condition: Callable[[], bool], what: str) -> None:
    """Wait in real time until *condition* holds."""
    deadline = time.monotonic() + WAIT
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.001)


def quiet(executor: Executor) -> None:
    """Stop the polls, and wait until a poll already written is answered."""
    executor.polling = False
    until(lambda: executor.in_flight is None, "a poll already written to be answered")


class Watch:
    """A transport that counts messages written and responses read, around another one.

    :ivar overlaps: Messages written while an earlier one was unanswered.
    """

    def __init__(self, inner: SimulatedMeter) -> None:
        self.inner = inner
        self.router = LineRouter()
        self.written = self.answered = self.overlaps = 0

    def write(self, data: bytes) -> int:
        for line in data.split(b"\n")[:-1]:
            if line:
                if self.written != self.answered:
                    self.overlaps += 1
                self.written += 1
        return self.inner.write(data)

    def read(self, size: int) -> bytes:
        chunk = self.inner.read(size)
        events = self.router.feed(chunk)
        self.answered += sum(isinstance(event, Response) for event in events)
        return chunk

    def close(self) -> None:
        self.inner.close()


class ScriptedLine:
    """A line whose answers a responder or the test writes.

    Syncs are answered as the instrument answers them unless *answer_syncs*
    is off. An empty read lets *tick* seconds pass on *clock*, as a port's
    timeout would (none with 0, so that nothing times out); while
    :attr:`hold` is set a read waits for :attr:`release`.
    """

    def __init__(
        self,
        clock: FakeClock,
        responder: Callable[[str], str | None] | None = None,
        *,
        answer_syncs: bool = True,
        tick: float = 0.05,
    ) -> None:
        self.clock = clock
        self.tick = tick
        self.responder = responder
        self.answer_syncs = answer_syncs
        self.messages: list[str] = []
        self.out: queue.Queue[bytes] = queue.Queue()
        self.hold = threading.Event()
        self.release = threading.Event()
        self.held = threading.Event()
        self.fail: OSError | None = None

    def push(self, text: str) -> None:
        """Queue *text* for the reader."""
        self.out.put(text.encode("ascii"))

    def write(self, data: bytes) -> int:
        for raw in data.decode("ascii").split("\n")[:-1]:
            if not raw:
                continue
            self.messages.append(raw)
            if raw.startswith("KMSYNC") and self.answer_syncs:
                nonce = raw.split(";")[0]
                self.push(f'-113,"Undefined header;{nonce}"\n')
            elif self.responder is not None:
                answer = self.responder(raw)
                if answer is not None:
                    self.push(answer)
        return len(data)

    def read(self, size: int) -> bytes:
        if self.fail is not None:
            raise self.fail
        if self.hold.is_set():
            self.held.set()
            assert self.release.wait(WAIT)
            self.hold.clear()
            self.release.clear()
        try:
            return self.out.get_nowait()
        except queue.Empty:
            self.clock.sleep(self.tick)
            time.sleep(0.0005)
            return b""

    def close(self) -> None:
        """Nothing to release."""


def ok(raw: str) -> str:
    """The answer of a message with no query: its error query's empty queue."""
    return '0,"No error"\n'


# -- what is sent ---------------------------------------------------------------


def test_a_message_over_255_bytes_with_its_error_query_is_refused_unsent() -> None:
    sim, clock = clocked()
    with running(sim, clock) as executor:
        quiet(executor)
        longest = "*IDN?" + " " * (MAX_MESSAGE - len("*IDN?;:SYST:ERR:ALL?"))
        assert len(check_message(longest)) == MAX_MESSAGE
        before = list(sim.messages)
        with pytest.raises(MessageRefused, match="256 bytes"):
            executor.submit(longest + " ")
        assert sim.messages == before  # nothing reached the instrument
        reply = executor.call(longest, wait=WAIT)
        assert reply.units[0].startswith("KLEIDOS,") and reply.errors == []


def test_what_the_instrument_would_discard_is_refused_unsent() -> None:
    with pytest.raises(MessageRefused, match="empty"):
        check_message("  ")
    with pytest.raises(MessageRefused, match="0x0A"):
        check_message("*IDN?\n*RST")  # two messages, one answer awaited
    with pytest.raises(MessageRefused, match="0xE9"):
        check_message("TRAC:MARK 'café'")
    assert check_message("SENS:PROF\tSTAT") == "SENS:PROF\tSTAT;:SYST:ERR:ALL?"


@pytest.mark.parametrize("message", ['SYST:STAT? "x', "TRAC:MARK 'a", "TRAC:MARK \"a';*IDN?"])
def test_a_quote_left_open_is_refused_unsent(message: str) -> None:
    """The error query appended after it would sit inside the quote: no verdict
    unit, so the message would never be matched and the line would stall (P2)."""
    with pytest.raises(MessageRefused, match="quote"):
        check_message(message)
    assert check_message("TRAC:MARK 'a\"b'") == "TRAC:MARK 'a\"b';:SYST:ERR:ALL?"
    assert check_message('SYST:STAT?;:TRAC:MARK "a;b"').endswith(";:SYST:ERR:ALL?")


def test_the_simulator_discards_what_the_firmware_discards() -> None:
    sim = SimulatedMeter()
    sim.write(b"*IDN?" + b" " * 251 + b"\n")  # 256 bytes
    sim.write(b"*IDN\x01?" + b" " * 300 + b"\n")  # a bad byte wins over the length
    sim.write(b"SYST:ERR:ALL?\n")
    answer = sim.read(1 << 16).decode()
    assert answer.startswith('-363,"Input buffer overrun;program message longer than 255')
    assert ',-101,"Invalid character;byte 0x01' in answer


def test_units_are_classed_by_their_slowest_one() -> None:
    assert analyze("*IDN?").kind == "default"
    assert analyze("MEAS:CURR?").acquisition and analyze("MEAS:CURR?").kind == "acquisition"
    assert analyze("SENS:PROF STAT;:CAL:SHUN").acquisition
    assert analyze("ABOR;:FETC:STAT?").kind == "close"
    assert analyze("ROUT:LEAD OPEN,FORC;:ROUT:STAT?").kind == "relay"
    assert analyze("*TST?").kind == "test"
    assert analyze("FETC:HEAD?;:TRAC:DATA? RAW").kind == "dump"
    assert analyze("SENS:PROF TRAC;:INIT").opens_window
    assert not analyze("ABOR").opens_window
    # A unit after a ';' continues the path of the one before (SCPI-1999 §6.2.4).
    info = analyze("SENS:CURR:RANG:IND 1;REP?;:NOPE;;*IDN?")
    assert info.patterns == (
        "SENSe:CURRent:RANGe:INDex",
        "SENSe:CURRent:RANGe:REPort?",
        None,
        "*IDN?",
    )
    assert TIMEOUTS["acquisition"] > 2 * 600 + 1


@pytest.mark.parametrize("separator", ["\t", "\t\t", " \t"])
def test_a_header_ends_where_the_firmware_ends_it(separator: str) -> None:
    """The firmware ends a header at a space, tab, carriage return or newline
    (scpi.cpp, Splitter::next; chars::isSpace), and runs the unit: so the host
    classes it as the same command, with what it changes."""
    with_space, other = analyze("SENS:PROF STATE"), analyze(f"SENS:PROF{separator}STATE")
    assert other.patterns == with_space.patterns == ("SENSe:PROFile",)
    assert changed_settings(other) == changed_settings(with_space) == {"profile", "channels"}
    measure = analyze(f"MEAS:CURR?{separator}0.4")
    assert measure.patterns == analyze("MEAS:CURR? 0.4").patterns
    assert measure.acquisition and measure.kind == "acquisition"
    resolved = resolve_unit(f"SENS:PROF{separator}SLEEP")[0]
    assert resolved is not None and resolved.pattern == "SENSe:PROFile"


def test_units_are_packed_into_as_few_messages_as_fit() -> None:
    units = [f"SENS:CURR:THR:HYST? {index:03d}" for index in range(20)]
    messages = pack_units(units)
    assert len(messages) > 1
    assert all(len(check_message(message)) <= MAX_MESSAGE for message in messages)
    assert ";:".join(messages) == ";:".join(units)
    with pytest.raises(MessageRefused):
        pack_units(["X" * MAX_MESSAGE])


# -- order ------------------------------------------------------------------------


def test_three_callers_are_served_in_submission_order_one_message_at_a_time() -> None:
    sim, clock = clocked()
    line = Watch(sim)
    order: list[str] = []
    replies: dict[str, Future[Reply]] = {}
    lock = threading.Lock()
    start = threading.Barrier(3)

    def caller(base: int) -> None:
        start.wait()
        for index in range(40):
            message = f"SAMP:COUN {base + index};:SAMP:COUN?"
            with lock:
                replies[message] = executor.submit(message)
                order.append(message)

    with running(line, clock, queue_limit=200) as executor:
        threads = [threading.Thread(target=caller, args=(base,)) for base in (1000, 2000, 3000)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(WAIT)
        for message, future in replies.items():
            reply = future.result(WAIT)
            assert reply.units == [message.split()[1].split(";")[0]]  # its own answer
            assert reply.errors == [] and reply.sent
    sent = [message.removesuffix(";:SYST:ERR:ALL?") for message in sim.messages]
    assert [message for message in sent if message.startswith("SAMP:COUN ")] == order
    assert len(order) == 120
    assert line.overlaps == 0  # never a message written before the last was answered
    assert sent[0].startswith("KMSYNC")


# -- the priority abort -----------------------------------------------------------


def test_an_abort_goes_ahead_of_the_fifo_while_an_acquisition_holds_the_console() -> None:
    clock = FakeClock()
    # The clock stands still while the line is read: the acquisition ends
    # only by being aborted.
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=lambda _: time.sleep(0.0005))
    with running(sim, clock) as executor:
        quiet(executor)
        executor.call("SENS:PROF STAT;:SAMP:COUN 500000", wait=WAIT)
        measuring = executor.submit("MEAS:CURR?")
        behind = executor.submit("*IDN?")
        until(lambda: executor.in_flight is not None and executor.in_flight.acquisition, "MEAS")
        aborting = executor.abort()
        reply = measuring.result(WAIT)
        assert reply.units == [] and reply.codes == [-230]
        assert "aborted after 0 of 500000 samples" in reply.errors[0].detail
        aborted = aborting.result(WAIT)
        assert aborted.sent and aborted.errors == [] and aborted.message == "ABOR"
        assert behind.result(WAIT).units[0].startswith("KLEIDOS,")
    sent = [message.removesuffix(";:SYST:ERR:ALL?") for message in sim.messages]
    assert sent[-3:] == ["MEAS:CURR?", "ABOR", "*IDN?"]


def test_an_abort_with_no_acquisition_waits_its_turn() -> None:
    sim, clock = clocked()
    with running(sim, clock) as executor:
        assert executor.abort().result(WAIT).codes == [111]  # no window open


def test_an_abort_whose_acquisition_ended_first_is_not_sent() -> None:
    clock = FakeClock()
    line = ScriptedLine(clock, lambda raw: None)  # nothing answered but syncs
    executor = Executor(line, clock=clock)
    executor.polling = False
    executor.start()
    try:
        executor.wait_ready(WAIT)
        measuring = executor.submit("MEAS:CURR?")
        until(lambda: executor.in_flight is not None, "the acquisition's write")
        line.held.clear()
        line.hold.set()
        until(line.held.is_set, "the reader to wait")
        aborting = executor.abort()  # queued while the reader waits
        line.push('0.088600000;0,"No error"\n')  # the acquisition ends first
        line.release.set()
        assert measuring.result(WAIT).units == ["0.088600000"]
        aborted = aborting.result(WAIT)
        assert not aborted.sent and aborted.units == [] and aborted.errors == []
        assert all(not message.startswith("ABOR") for message in line.messages)
    finally:
        executor.close()


def test_a_second_abort_queued_as_the_acquisition_ends_is_not_sent() -> None:
    """The first abort is on the wire, so the second waits for its answer and
    then finds nothing to abort: it never goes ahead of a message in flight."""
    clock = FakeClock()
    line = ScriptedLine(clock, lambda raw: None, tick=0.0)
    executor = Executor(line, clock=clock)
    executor.polling = False
    executor.start()
    try:
        executor.wait_ready(WAIT)
        measuring = executor.submit("MEAS:CURR?")
        until(lambda: executor.in_flight is not None, "the acquisition's write")
        first = executor.abort()
        until(lambda: any(m.startswith("ABOR") for m in line.messages), "the first abort")
        line.held.clear()
        line.hold.set()
        until(line.held.is_set, "the reader to wait")
        second = executor.abort()  # the acquisition's answer is still to be read
        line.push('-230,"Data corrupt or stale;aborted after 0 of 1000 samples"\n')
        line.release.set()
        assert measuring.result(WAIT).codes == [-230]
        line.push('0,"No error"\n')
        assert first.result(WAIT).sent
        late = second.result(WAIT)
        assert not late.sent and late.units == [] and late.errors == []
        assert sum(message.startswith("ABOR") for message in line.messages) == 1
    finally:
        executor.close()


# -- time-outs and getting back in step ---------------------------------------------


def test_a_message_the_instrument_drops_times_out_and_the_next_one_is_answered() -> None:
    sim, clock = clocked()
    events: list[Event] = []
    with running(sim, clock) as executor:
        executor.polling = False
        until(lambda: executor.in_flight is None, "a poll already written to be answered")
        executor.add_listener(events.append)
        sim.boot_writes = 1  # the next write is discarded, as by a booting instrument
        started = clock()
        with pytest.raises(MeterTimeout, match="no answer within 5 s"):
            executor.call("*IDN?", wait=WAIT)
        assert clock() - started >= TIMEOUTS["default"]
        assert executor.call("*IDN?", wait=WAIT).units[0].startswith("KLEIDOS,")
        assert executor.call("*IDN?", timeout=0.5, wait=WAIT).errors == []
    assert any(isinstance(event, LinkChanged) and event.up for event in events)
    assert sum(message.startswith("KMSYNC") for message in sim.messages) == 2


def test_a_late_answer_is_discarded_and_never_taken_for_the_next_message() -> None:
    clock = FakeClock()
    late: list[str] = []

    def responder(raw: str) -> str | None:
        if raw.startswith("SAMP:COUN?"):
            if not late:
                late.append(raw)
                return None  # this one is answered too late, below
            return '42;0,"No error"\n'
        return ok(raw)

    line = ScriptedLine(clock, responder, answer_syncs=False)
    executor = Executor(line, clock=clock)
    executor.polling = False
    strays: list[Event] = []
    executor.add_listener(strays.append)
    executor.start()
    try:
        until(lambda: len(line.messages) >= 1, "the first sync")
        line.push(f'-113,"Undefined header;{line.messages[-1].split(";")[0]}"\n')
        executor.wait_ready(WAIT)
        first = executor.submit("SAMP:COUN?")
        with pytest.raises(MeterTimeout):
            first.result(WAIT)
        until(lambda: line.messages[-1].startswith("KMSYNC2"), "the sync after the time-out")
        line.push('7;0,"No error"\n')  # the late answer to the first message
        line.push('-113,"Undefined header;KMSYNC2"\n')
        assert executor.call("SAMP:COUN?", wait=WAIT).units == ["42"]
    finally:
        executor.close()
    assert StrayLine(["7", '0,"No error"']) in strays


def test_an_answer_that_is_no_reply_is_a_stray_line() -> None:
    clock = FakeClock()
    line = ScriptedLine(clock, ok)
    events: list[Event] = []
    with running(line, clock) as executor:
        quiet(executor)
        executor.add_listener(events.append)
        line.push("ua v=0.088\n")  # a record's tail, as a port opened mid-line gives it
        assert executor.call("*CLS", wait=WAIT).errors == []
        line.push('-113,"Undefined header;KMSYNC99"\n')  # an old sync's answer
        line.push("orphan\n")
    assert StrayLine(["ua v=0.088"]) in events


def test_a_silent_line_goes_down_and_comes_back_when_it_answers() -> None:
    clock = FakeClock()
    line = ScriptedLine(clock, lambda raw: None, answer_syncs=False)
    executor = Executor(line, clock=clock, link_timeout=3.0)
    events: list[Event] = []
    executor.add_listener(events.append)
    waiting = executor.submit("*IDN?")  # queued behind the first sync
    executor.start()
    try:
        with pytest.raises(MeterTimeout, match="did not answer within 0.2 s"):
            executor.wait_ready(0.2)
        until(lambda: executor.link_down, "the line to go down")
        assert not executor.link_up
        with pytest.raises(LinkDown):
            waiting.result(WAIT)
        with pytest.raises(LinkDown):
            executor.submit("*IDN?")
        line.answer_syncs = True
        line.responder = lambda raw: '1;0,"No error"\n'
        executor.wait_ready(WAIT)
        assert executor.link_up and not executor.link_down
        assert executor.call("*ESR?", wait=WAIT).units == ["1"]
    finally:
        executor.close()
    assert LinkChanged(False) in events and LinkChanged(True) in events


def test_a_port_that_fails_ends_everything_it_held() -> None:
    clock = FakeClock()
    line = ScriptedLine(clock, lambda raw: None, tick=0.0)
    executor = Executor(line, clock=clock)
    executor.polling = False
    events: list[Event] = []
    executor.add_listener(events.append)
    executor.start()
    try:
        executor.wait_ready(WAIT)
        in_flight = executor.submit("*IDN?")
        until(lambda: executor.in_flight is not None, "the write")
        line.fail = OSError(5, "Input/output error")
        with pytest.raises(LinkDown, match="the port failed"):
            in_flight.result(WAIT)
        with pytest.raises(ExecutorClosed):
            executor.submit("*IDN?")
        with pytest.raises(ExecutorClosed):
            executor.abort()
    finally:
        executor.close()
    assert events[-1] == LinkChanged(False)


# -- the FIFO's bounds and the end ----------------------------------------------------


def test_a_full_fifo_refuses_at_once_and_close_ends_what_waits() -> None:
    clock = FakeClock()
    line = ScriptedLine(clock, lambda raw: None, tick=0.0)
    executor = Executor(line, clock=clock, queue_limit=2)
    executor.polling = False
    executor.start()
    executor.wait_ready(WAIT)
    in_flight = executor.submit("*IDN?")
    until(lambda: executor.in_flight is not None, "the write")
    queued = [executor.submit("*IDN?"), executor.submit("*IDN?")]
    assert executor.queued == 2
    with pytest.raises(ExecutorBusy, match="2 messages are waiting"):
        executor.submit("*IDN?")
    executor.close(wait=0.2)
    for future in [*queued, in_flight]:
        with pytest.raises(ExecutorClosed):
            future.result(WAIT)
    with pytest.raises(ExecutorClosed):
        executor.submit("*IDN?")
    with pytest.raises(ExecutorClosed):
        executor.abort().result(WAIT)


def test_query_all_raises_on_an_error_or_a_short_answer() -> None:
    sim, clock = clocked()
    with running(sim, clock) as executor:
        assert executor.query_all(["SENS:PROF?", "SAMP:COUN?"]) == ["TRACE", "100"]
        with pytest.raises(MeterError, match="-113"):
            executor.query_all(["NOPE?"])
        with pytest.raises(MeterError, match="2 queries answered 1 units"):
            executor.query_all(["SENS:PROF?", "SAMP:COUN 5"])


# -- the status poll ------------------------------------------------------------------


def _polls(sim: SimulatedMeter) -> int:
    return sum(message.startswith("SYST:STAT?;:SYST:HEAL?") for message in sim.messages)


def test_the_status_is_polled_twice_a_second_idle_and_once_under_a_capture() -> None:
    sim, clock = clocked()
    polls: list[tuple[float, Status]] = []

    def note(event: Event) -> None:
        if isinstance(event, Status):
            polls.append((clock(), event))  # on the executor's thread: the poll's own time

    with running(sim, clock) as executor:
        executor.add_listener(note)
        start = clock()
        until(lambda: clock() - start >= 10.0, "ten idle seconds")
        executor.call("SENS:PROF STAT;:SENS:CAPT STAT;:INIT", wait=WAIT)
        start = clock()
        until(lambda: clock() - start >= 10.0, "ten seconds under a capture")
        assert executor.status is not None and executor.status.window_open
    idle = [t for t, status in polls if not status.window_open]
    capturing = [t for t, status in polls if status.window_open]
    # The fake clock moves in 50 ms reads, so a period is due on the read after it.
    assert len(idle) >= 15 and len(capturing) >= 8
    idle_gaps = [round(b - a, 3) for a, b in zip(idle, idle[1:], strict=False)]
    capture_gaps = [round(b - a, 3) for a, b in zip(capturing, capturing[1:], strict=False)]
    assert all(0.5 <= gap < 0.6 for gap in idle_gaps), idle_gaps
    assert all(1.0 <= gap < 1.1 for gap in capture_gaps), capture_gaps
    last = polls[-1][1]
    assert last.status["sink"] == "stats" and last.health["fw"] == "0.18.0"


def test_no_poll_while_polling_is_off_and_one_at_once_when_asked() -> None:
    sim, clock = clocked()
    with running(sim, clock) as executor:
        executor.polling = False
        assert not executor.polling
        until(lambda: executor.in_flight is None, "a poll already written to be answered")
        polls, start = _polls(sim), clock()
        until(lambda: clock() - start >= 5.0, "five seconds")
        assert _polls(sim) == polls
        executor.polling = True
        executor.poll_now()
        until(lambda: _polls(sim) > polls, "a poll")


def test_turning_polling_off_drops_a_poll_that_waits_in_the_fifo() -> None:
    clock = FakeClock()
    line = ScriptedLine(clock, lambda raw: None, tick=0.0)  # nothing answered but syncs
    executor = Executor(line, clock=clock)
    executor.polling = False
    executor.start()
    try:
        executor.wait_ready(WAIT)
        line.held.clear()
        line.hold.set()
        until(line.held.is_set, "the reader to wait")
        held = executor.submit("*IDN?")  # written next, and never answered
        executor.polling = True
        executor.poll_now()
        line.release.set()
        until(lambda: executor.in_flight is not None and executor.queued == 1, "the poll behind")
        executor.polling = False
        assert executor.queued == 0  # the poll left the FIFO
        assert not held.done()
    finally:
        executor.close(wait=0.2)


def test_a_cancelled_message_is_never_sent_and_the_hooks_see_each_one_written() -> None:
    clock = FakeClock()
    line = ScriptedLine(clock, ok, tick=0.0)
    executor = Executor(line, clock=clock)
    executor.polling = False
    seen: list[str] = []
    executor.on_sent(lambda info: seen.append(info.message))
    executor.start()
    try:
        executor.wait_ready(WAIT)
        line.held.clear()
        line.hold.set()
        until(line.held.is_set, "the reader to wait")
        first = executor.submit("*CLS")
        second = executor.submit("*WAI")
        assert second.cancel()
        line.release.set()
        assert first.result(WAIT).errors == []
        assert executor.call("*OPC", wait=WAIT).errors == []
    finally:
        executor.close()
    assert seen == ["*CLS", "*OPC"]  # neither the syncs nor the cancelled one
    assert all(not message.startswith("*WAI") for message in line.messages)


def test_a_cancelled_message_dropped_by_the_close_stays_cancelled() -> None:
    """The close fails what waits; a caller that had cancelled already keeps its answer."""
    executor = Executor(ScriptedLine(FakeClock(), ok))  # never started: nothing is sent
    cancelled, waiting = executor.submit("*CLS"), executor.submit("*WAI")
    assert cancelled.cancel()
    executor.close()
    assert cancelled.cancelled()
    with pytest.raises(ExecutorClosed, match=r"'\*WAI' was not sent"):
        waiting.result(0)


def test_an_unanswered_poll_times_out_like_any_message_and_polling_goes_on() -> None:
    clock = FakeClock()
    unanswered: list[str] = []

    def responder(raw: str) -> str | None:
        if raw.startswith("SYST:STAT?") and not unanswered:
            unanswered.append(raw)
            return None
        if raw.startswith("SYST:STAT?"):
            return 'running=0 sink=idle;reset=poweron uptime_us=1 elf=a fw=1;0,"No error"\n'
        return ok(raw)

    line = ScriptedLine(clock, responder)
    statuses: list[Event] = []
    with running(line, clock) as executor:
        executor.add_listener(
            lambda event: statuses.append(event) if isinstance(event, Status) else None
        )
        until(lambda: len(statuses) >= 2, "two answered polls after the one that was not")
    assert unanswered


def test_closing_an_executor_never_started_ends_what_it_holds() -> None:
    executor = Executor(SimulatedMeter())
    waiting = executor.submit("*IDN?")
    executor.close()
    with pytest.raises(ExecutorClosed):
        waiting.result(WAIT)


def test_a_reboot_and_the_errors_nobody_asked_for_reach_the_listeners() -> None:
    sim, clock = clocked()
    events: list[Event] = []
    with running(sim, clock) as executor:
        executor.add_listener(events.append)
        until(lambda: sum(isinstance(e, Status) for e in events) >= 2, "two polls")
        with sim._lock:
            sim._fail(113, "the hold reclosed k1")
        until(lambda: any(isinstance(e, DeviceErrors) for e in events), "the hold's error")
        sim.inject_reboot("panic")
        until(lambda: any(isinstance(e, Rebooted) for e in events), "the reboot")
    reboot = next(event for event in events if isinstance(event, Rebooted))
    assert reboot.health["reset"] == "panic"
    found = next(event for event in events if isinstance(event, DeviceErrors))
    assert found.errors[0].code == 113


def test_a_reboot_is_an_uptime_short_of_the_time_between_polls_or_a_new_image() -> None:
    rebooted = executor_module._rebooted
    before = {"uptime_us": "1200000", "elf": "a", "fw": "0.18.0"}
    # Rebooted 0.3 s after a poll at 1.2 s of uptime: 1.5 s later it reads 1.2 s again.
    assert rebooted(10.0, before, 11.5, {**before, "uptime_us": "1200000"})
    assert not rebooted(10.0, before, 11.5, {**before, "uptime_us": "2700150"})
    assert rebooted(10.0, before, 11.5, {**before, "uptime_us": "2700150", "elf": "b"})
    assert not rebooted(10.0, {"elf": "a"}, 11.5, {"elf": "a", "uptime_us": "x"})


def test_the_first_sync_reports_what_was_queued_before() -> None:
    sim, clock = clocked()
    with sim._lock:
        sim._fail(116, "a reset (panic) released k1")
    executor = Executor(sim, clock=clock)
    events: list[Event] = []
    executor.add_listener(events.append)
    executor.start()
    try:
        executor.wait_ready(WAIT)
    finally:
        executor.close()
    errors = [event for event in events if isinstance(event, DeviceErrors)]
    assert [entry.code for entry in errors[0].errors] == [116]


# -- the stream and the listeners ---------------------------------------------------


def test_stream_records_reach_the_listeners_and_a_failing_listener_is_kept(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sim, clock = clocked()
    samples: list[Sample] = []

    def broken(event: Event) -> None:
        raise RuntimeError("a listener's bug")

    with running(sim, clock) as executor:
        executor.add_listener(broken)
        executor.add_listener(
            lambda event: samples.append(event) if isinstance(event, Sample) else None
        )
        with caplog.at_level(logging.ERROR, logger="kleidos_meter.panel.executor"):
            executor.call("SENS:PROF STAT;:INIT", wait=WAIT)
            until(lambda: len(samples) >= 100, "a hundred samples")
        executor.remove_listener(broken)
        stats = executor.call("ABOR;:FETC:STAT?", wait=WAIT)
    assert "a listener failed" in caplog.text
    assert int(stats.units[0].split("samples=")[1].split()[0]) >= len(samples)


def test_reply_and_status_helpers() -> None:
    reply = Reply("X", [], [ErrorEntry(-113, "Undefined header;X")])
    assert reply.codes == [-113]
    assert not Status({}, {}).window_open
    assert Status({"sink": "ring"}, {}).window_open


def test_a_poll_that_left_the_fifo_as_polling_went_off_is_not_written() -> None:
    # The thread takes a poll from the FIFO and, before it writes it, polling
    # goes off (a passthrough client attaching): the write must not happen.
    line = ScriptedLine(FakeClock(), ok)
    executor = Executor(line)
    poll = executor._job(executor_module.POLL_MESSAGE, None, internal="poll")
    executor._poll_job = poll
    executor.polling = False
    executor._write(poll)
    assert poll.future.cancelled() and executor._poll_job is None
    assert line.messages == [] and executor.in_flight is None


# -- errors no caller asked for --------------------------------------------------------


@pytest.mark.parametrize(
    ("order", "code"),
    [("ROUT:NULL ON", 113), ("ROUT:LEAD OPEN,FORC", 114)],
)
def test_a_hold_release_queued_meanwhile_is_an_event_not_the_next_callers_error(
    order: str, code: int
) -> None:
    """The instrument queues 113/114 on its own; the next message's error query
    drains them, and that message did not cause them (P1)."""
    from kleidos_meter.panel.settings import read_settings

    sim, clock = clocked()
    with running(sim, clock) as executor:
        quiet(executor)
        events: list[object] = []
        executor.add_listener(events.append)
        assert executor.call(order, wait=WAIT).errors == []
        clock.sleep(200.0)  # past the hold's 120 s
        settings = read_settings(executor)  # raised MeterError before the fix
        assert settings
        found = [e for e in events if isinstance(e, DeviceErrors)]
        assert [entry.code for event in found for entry in event.errors] == [code]
        assert executor.call("SYST:STAT?", wait=WAIT).errors == []


def test_a_callers_own_error_stays_its_own_beside_an_asynchronous_one() -> None:
    sim, clock = clocked()
    with running(sim, clock) as executor:
        quiet(executor)
        events: list[object] = []
        executor.add_listener(events.append)
        executor.call("ROUT:NULL ON", wait=WAIT)
        clock.sleep(200.0)
        reply = executor.call("SYST:BOGUS", wait=WAIT)
        assert [entry.code for entry in reply.errors] == [-113]
        found = [e for e in events if isinstance(e, DeviceErrors)]
        assert [entry.code for event in found for entry in event.errors] == [113]
