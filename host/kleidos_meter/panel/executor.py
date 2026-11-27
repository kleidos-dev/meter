# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The one owner of the line: program messages in a FIFO, one in flight.

:class:`Executor` runs one thread that alone writes to and reads from the
instrument's port. Callers on any thread :meth:`~Executor.submit` a program
message and get a :class:`~concurrent.futures.Future` of its :class:`Reply`;
the thread sends the messages in the order they were submitted, each only
after the previous one was answered, so a response is always the answer to
the message in flight. Every message is sent with ``;:SYST:ERR:ALL?``
appended, so its answer carries its own verdict and no error is left queued
for the next caller.

What it guarantees, and how (``docs/scpi.md``, "The line"):

- **Nothing is sent that the instrument would discard.** A message whose
  wire form (its ``;:SYST:ERR:ALL?`` included) is longer than 255 bytes, that
  holds a byte other than printable ASCII or a tab, or that is empty, is
  refused with :class:`MessageRefused` before anything is written: the first
  would queue ``-363`` and the second ``-101``, and neither would answer.
- **A time-out per class.** A message waits as long as its slowest unit
  may take (:data:`TIMEOUTS`): an acquisition up to the firmware's bound of
  twice 600 s plus a second, a ring dump four minutes, a relay order or a
  self-test seconds, everything else five.
- **Back in step after a time-out.** A response that comes after its
  message timed out would be taken for the next one's. So after a time-out
  the thread ends any partial line with a bare newline and sends a sync
  message, an undefined header carrying a fresh number (``KMSYNC<n>``)
  followed by ``SYSTem:ERRor:ALL?``; the instrument names the header in its
  ``-113`` (``console.cpp``), and every response before the one that names
  this number is discarded. The first sync, at :meth:`~Executor.start`, is
  also the readiness check: a booting instrument discards what it receives,
  so the sync is repeated until it is answered. A line that stays silent for
  ``link_timeout`` is down: what waits fails with :class:`LinkDown`, and the
  sync goes on until the instrument answers again.
- **A priority ``ABORt`` during an acquisition.** ``MEASure``, ``READ`` and
  ``CALibration:SHUNt`` hold the console for their whole window and read
  only a message whose first unit is ``ABORt`` meanwhile. :meth:`~Executor.abort`
  sends one at once while such a message is in flight, ahead of the FIFO;
  the acquisition's message is answered first (with ``-230``) and the abort's
  second. At any other time ``abort()`` is an ordinary message in its turn.
- **A status poll** (``SYSTem:STATus?`` and ``SYSTem:HEALth?``) twice a
  second with no window open and once a second with one, never while
  :attr:`~Executor.polling` is off (a passthrough client is attached). A
  ``SYSTem:HEALth?`` whose ``uptime_us`` grew by less than the time since
  the last poll, or whose image changed, is a reboot: listeners get a
  :class:`Rebooted`.
- **A hand-over to a passthrough client.** :meth:`~Executor.hand_over` gives
  the line to another controller while nothing is in flight or queued: the
  thread then writes nothing of its own, every byte it reads goes to the
  hand-over's tap as it came, and the caller writes the client's bytes with
  :meth:`~Executor.forward`. Messages submitted meanwhile are refused with
  :class:`LineHeld`. The stream records the thread reads still reach the
  listeners (the panel watches the client's captures); responses are the
  client's and reach no one. :meth:`~Executor.take_back` ends it with a sync,
  a bare newline that ends any message the client left unfinished and a
  ``KMSYNC`` whose answer discards every response before it.

Stream records, block progress and everything the thread learns reach the
listeners (:meth:`~Executor.add_listener`) as events, on the thread, in line
order. A listener must not block; one that raises is logged and kept.
"""

from __future__ import annotations

import itertools
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field

from ..client import MeterError, MeterTimeout, Transport
from ..protocol import (
    BlockProgress,
    ErrorEntry,
    LineRouter,
    Mark,
    Overflow,
    Response,
    Sample,
    parse_errors,
    parse_fields,
    split_units,
)
from ..sim_table import resolve, split_header

__all__ = [
    "ERRORS_SUFFIX",
    "MAX_MESSAGE",
    "POLL_MESSAGE",
    "TIMEOUTS",
    "WINDOW_OPENERS",
    "DeviceErrors",
    "Event",
    "Executor",
    "ExecutorBusy",
    "ExecutorClosed",
    "LinkChanged",
    "LineHeld",
    "LinkDown",
    "MessageInfo",
    "MessageRefused",
    "Rebooted",
    "Reply",
    "Status",
    "StrayLine",
    "analyze",
    "check_message",
    "pack_units",
    "unit_patterns",
]

_LOG = logging.getLogger(__name__)

#: The longest program message the instrument takes, bytes, without its newline.
MAX_MESSAGE = 255

#: What every message is sent with, so that its answer carries its errors.
ERRORS_SUFFIX = ";:SYST:ERR:ALL?"

#: How long a message may wait for its answer, seconds, by the class of its
#: slowest unit (:func:`analyze`).
TIMEOUTS: dict[str, float] = {
    "default": 5.0,
    "close": 20.0,  # ABORt: the stream's last samples, the null's computation
    "relay": 10.0,  # ROUTe orders: a coil's settle and the check that follows
    "test": 15.0,  # *TST?, DIAGnostic:I2C:SCAN?, DIAGnostic:WATChdog:TEST, a pin map's proof
    "dump": 240.0,  # TRACe:DATA?: a full ring at 6 Mbaud takes about 20 s
    "acquisition": 1206.0,  # twice the 600 s bound plus a second, and a margin
}

#: The class of each table pattern that is not ``default``.
_CLASS: dict[str, str] = {
    "ABORt": "close",
    "ROUTe:LEAD": "relay",
    "ROUTe:LEAD:CYCLe": "relay",
    "ROUTe:BYPass": "relay",
    "ROUTe:NULLpath": "relay",
    "ROUTe:RELay#": "relay",
    "*TST?": "test",
    "DIAGnostic:I2C:SCAN?": "test",
    "DIAGnostic:WATChdog:TEST": "test",
    "SYSTem:PINS:APPLy": "test",  # the bus brought up and the self-test's parts, twice on a revert
    "SYSTem:PINS:DEFault": "test",
    "TRACe:DATA?": "dump",
}

#: The patterns of the units that hold the console for an acquisition.
_ACQUISITION_PREFIXES = ("MEASure", "READ", "CALibration:SHUNt[:MEASure]")

#: The patterns of the units that open a capture or a null window.
WINDOW_OPENERS = frozenset({"INITiate[:IMMediate]", "INITiate:FORCe", "CALibration:ZERO"})

#: How far an uptime may fall short of the time between two polls before it
#: counts as a reboot, microseconds: the answers' latencies, never a boot.
_REBOOT_SLACK_US = 500_000

#: The status poll's message.
POLL_MESSAGE = "SYST:STAT?;:SYST:HEAL?"

#: An answer of ``SYSTem:ERRor:ALL?`` and nothing else: one or more entries.
_ERRORS_UNIT = re.compile(r'[+-]?\d+,"(?:[^"]|"")*"(?:,[+-]?\d+,"(?:[^"]|"")*")*')


class MessageRefused(MeterError, ValueError):
    """A message the instrument would discard: refused before it was sent."""


class LinkDown(MeterError):
    """The instrument has stopped answering; nothing is sent until it answers a sync."""


class LineHeld(MeterError):
    """The line is handed over to a passthrough client: nothing is sent until it is taken back."""


class ExecutorBusy(MeterError):
    """The FIFO is full: the caller is told at once instead of the queue growing."""


class ExecutorClosed(MeterError):
    """The executor has stopped, or stopped before this message was sent."""


@dataclass(frozen=True)
class MessageInfo:
    """What a program message is, from its units' table patterns.

    :ivar message: The message as the caller gave it.
    :ivar patterns: Each unit's pattern in ``kCommands``, None for an undefined header.
    :ivar kind: Its time-out class, a key of :data:`TIMEOUTS`.
    :ivar acquisition: Whether a unit holds the console for an acquisition.
    """

    message: str
    patterns: tuple[str | None, ...]
    kind: str
    acquisition: bool

    @property
    def opens_window(self) -> bool:
        """Whether a unit opens a capture or a null window."""
        return any(pattern in WINDOW_OPENERS for pattern in self.patterns)


@dataclass(frozen=True)
class Reply:
    """A message's answer.

    :ivar message: The message, as submitted.
    :ivar units: Its queries' response units, the appended error query's left out.
    :ivar errors: What the instrument queued while it ran, oldest first.
    :ivar blocks: Any definite-length block it carried.
    :ivar elapsed: Seconds from its write to its answer.
    :ivar sent: False only for a priority ``ABORt`` whose acquisition had
        ended before it could be written; nothing was sent then.
    """

    message: str
    units: list[str]
    errors: list[ErrorEntry]
    blocks: list[bytes] = field(default_factory=list)
    elapsed: float = 0.0
    sent: bool = True

    @property
    def codes(self) -> list[int]:
        """The error numbers."""
        return [entry.code for entry in self.errors]


@dataclass(frozen=True)
class Status:
    """A status poll's answer.

    :ivar status: ``SYSTem:STATus?``'s fields.
    :ivar health: ``SYSTem:HEALth?``'s fields.
    """

    status: dict[str, str]
    health: dict[str, str]

    @property
    def window_open(self) -> bool:
        """Whether a capture or a null window is open (``sink`` other than ``idle``)."""
        return self.status.get("sink", "idle") != "idle"


@dataclass(frozen=True)
class Rebooted:
    """The instrument rebooted since the last poll.

    :ivar health: ``SYSTem:HEALth?`` after the reboot: its ``reset`` is the reason.
    """

    health: dict[str, str]


#: The errors the instrument queues on its own, never as a unit's verdict
#: (``docs/scpi.md``): a hold that released a null path (``113``), reclosed a
#: lead (``114``) or opened a bypass (``115``), relays a reset released
#: (``116``), and a message a receive overrun discarded (``-363``: a message
#: that was discarded is never the one answered). The next message's error
#: query drains them, so they are taken out of its reply and given to the
#: listeners as :class:`DeviceErrors`.
ASYNC_ERRORS = frozenset({113, 114, 115, 116, -363})


@dataclass(frozen=True)
class DeviceErrors:
    """Errors the instrument queued that no caller's message asked for.

    Those a status poll or a sync found, and the :data:`ASYNC_ERRORS` any
    message's error query drained: a hold that released a lead (``113``),
    relays a reset released (``116``), what was queued before serve opened
    the port.

    :ivar errors: The entries, oldest first.
    """

    errors: list[ErrorEntry]


@dataclass(frozen=True)
class LinkChanged:
    """The line went down or came back.

    :ivar up: Whether the instrument answers now.
    """

    up: bool


@dataclass(frozen=True)
class StrayLine:
    """A response line that answered nothing in flight, discarded.

    A late answer to a message that timed out, an earlier sync's answer, or
    a piece of a line the port delivered from its middle.

    :ivar units: Its units.
    """

    units: list[str]


#: What a listener receives.
Event = (
    Sample
    | Mark
    | BlockProgress
    | Overflow
    | Status
    | Rebooted
    | DeviceErrors
    | LinkChanged
    | StrayLine
)


def check_message(message: str) -> str:
    """The wire form of *message*, or :class:`MessageRefused` when the instrument would discard it.

    :param message: A program message, without its newline.
    :returns: The message with :data:`ERRORS_SUFFIX` appended.
    :raises MessageRefused: When it is empty, holds a byte other than printable
        ASCII or a tab (a newline would make it two messages), leaves a quote
        open (the error query appended after it would be quoted, so the
        message would get no verdict and never be matched), or its wire form
        is longer than :data:`MAX_MESSAGE` bytes.
    """
    if not message.strip():
        raise MessageRefused("an empty message has no answer")
    bad = [char for char in message if not (char == "\t" or " " <= char <= "~")]
    if bad:
        raise MessageRefused(
            f"byte 0x{ord(bad[0]):02X} is not printable ASCII: the instrument would discard"
            " the message (-101)"
        )
    quote = ""
    for char in message:  # as split_outside_quotes() and the firmware's Splitter scan
        if quote:
            quote = "" if char == quote else quote
        elif char in "\"'":
            quote = char
    if quote:
        raise MessageRefused(
            f"a {quote} quote is left open: the error query after it would be quoted and"
            " the message would get no answer"
        )
    wire = message + ERRORS_SUFFIX
    if len(wire) > MAX_MESSAGE:
        raise MessageRefused(
            f"{len(wire)} bytes with {ERRORS_SUFFIX!r}: the instrument takes at most"
            f" {MAX_MESSAGE} and would discard it (-363)"
        )
    return wire


def unit_patterns(message: str) -> tuple[str | None, ...]:
    """Each unit's pattern in the command table, its path resolved as the firmware resolves it.

    :param message: A program message.
    :returns: One pattern per non-empty unit, None for an undefined header.
    """
    patterns: list[str | None] = []
    path = ""
    for raw in split_units(message):
        unit = raw.strip()
        if not unit:
            continue
        header = split_header(unit)[0]
        if not header.startswith("*"):
            header = header[1:] if header.startswith(":") else path + header
            path = header[: header.rfind(":") + 1] if ":" in header else ""
        resolved = resolve(header)
        patterns.append(None if resolved is None else resolved.pattern)
    return tuple(patterns)


def analyze(message: str) -> MessageInfo:
    """What *message* is: its patterns, its time-out class, whether it acquires.

    :param message: A program message.
    :returns: Its description.
    """
    patterns = unit_patterns(message)
    acquisition = any(
        pattern is not None and pattern.startswith(_ACQUISITION_PREFIXES) for pattern in patterns
    )
    kinds = ["acquisition"] if acquisition else []
    kinds += [_CLASS.get(pattern or "", "default") for pattern in patterns]
    kind = max(kinds or ["default"], key=TIMEOUTS.__getitem__)
    return MessageInfo(message, patterns, kind, acquisition)


def pack_units(units: Iterable[str]) -> list[str]:
    """Join *units* into as few messages as fit the instrument's 255 bytes, in order.

    Each unit after the first of a message is written from the root (``;:``),
    so their meaning does not depend on how they were packed.

    :param units: Units with no leading colon.
    :returns: The messages.
    :raises MessageRefused: When one unit alone does not fit.
    """
    messages: list[str] = []
    for unit in units:
        if messages and len(messages[-1] + ";:" + unit + ERRORS_SUFFIX) <= MAX_MESSAGE:
            messages[-1] += ";:" + unit
            continue
        check_message(unit)
        messages.append(unit)
    return messages


@dataclass
class _Job:
    """A message on its way: what it is, its future, when it must be answered."""

    info: MessageInfo
    wire: str
    timeout: float
    future: Future[Reply]
    seq: int
    internal: str = ""  # "poll" or "sync" for the executor's own messages
    nonce: str = ""
    sent_at: float = 0.0
    deadline: float = 0.0


class Executor:
    """The line's one owner: see the module's description.

    :param transport: The port, or a simulator behind the same three methods;
        its ``read`` must return within a short time (the port's 50 ms).
    :param clock: The monotonic clock deadlines and polls are measured on.
    :param poll_idle: Seconds between status polls with no window open.
    :param poll_capture: Seconds between status polls with a window open.
    :param queue_limit: The most messages the FIFO holds.
    :param sync_timeout: How long each sync waits for its answer, seconds.
    :param link_timeout: How long the line may stay silent before it is down.
    :param timeouts: The time-out of each class, :data:`TIMEOUTS` by default.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        clock: Callable[[], float] = time.monotonic,
        poll_idle: float = 0.5,
        poll_capture: float = 1.0,
        queue_limit: int = 64,
        sync_timeout: float = 1.0,
        link_timeout: float = 15.0,
        timeouts: dict[str, float] | None = None,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self.poll_idle = poll_idle
        self.poll_capture = poll_capture
        self._queue_limit = queue_limit
        self._sync_timeout = sync_timeout
        self._link_timeout = link_timeout
        self._timeouts = dict(TIMEOUTS if timeouts is None else timeouts)
        self._router = LineRouter()
        self._lock = threading.Lock()
        self._fifo: deque[_Job] = deque()
        self._priority: deque[_Job] = deque()
        self._written: deque[_Job] = deque()
        self._listeners: list[Callable[[Event], None]] = []
        self._sent_hooks: list[Callable[[MessageInfo], None]] = []
        self._seq = itertools.count()
        self._nonces = itertools.count(1)
        self._thread: threading.Thread | None = None
        self._stopping = False
        self._closed = False
        self._polling = True
        self._poll_job: _Job | None = None
        self._next_poll = 0.0
        self._syncing = True  # the first sync is the readiness check
        self._sync_since: float | None = None
        self._down = False
        self._ready = threading.Event()
        self._last_health: tuple[float, dict[str, str]] | None = None
        self._tap: Callable[[bytes], None] | None = None
        self._reset_router = False
        self._wire = threading.Lock()  # one writer of the transport at a time
        #: The last status poll's answer, None before the first.
        self.status: Status | None = None

    # -- the caller's side ----------------------------------------------------

    def start(self) -> None:
        """Start the thread; the first sync begins at once."""
        self._thread = threading.Thread(
            target=self._run, name="kleidos-meter-executor", daemon=True
        )
        self._thread.start()

    def wait_ready(self, timeout: float) -> None:
        """Wait until the instrument has answered the first sync.

        :param timeout: Seconds of real time to wait.
        :raises MeterTimeout: When it has not answered by then.
        """
        if not self._ready.wait(timeout):
            raise MeterTimeout(f"the instrument did not answer within {timeout:g} s")

    @property
    def link_up(self) -> bool:
        """Whether the line is in step: the last sync was answered and nothing timed out since."""
        return not self._syncing

    @property
    def link_down(self) -> bool:
        """Whether the line has been silent for ``link_timeout``: submits fail at once."""
        return self._down

    @property
    def polling(self) -> bool:
        """Whether the status is polled; off while a passthrough client is attached."""
        return self._polling

    @polling.setter
    def polling(self, value: bool) -> None:
        with self._lock:
            self._polling = value
            poll = self._poll_job
            if not value and poll is not None and poll in self._fifo:
                self._fifo.remove(poll)
                self._poll_job = None
                poll.future.cancel()

    @property
    def handed_over(self) -> bool:
        """Whether the line is handed over to a passthrough client (:meth:`hand_over`)."""
        with self._lock:
            return self._tap is not None

    @property
    def idle(self) -> bool:
        """Whether nothing is in flight, queued or being synced: a hand-over may begin."""
        with self._lock:
            return not (self._written or self._fifo or self._priority or self._syncing)

    @property
    def in_flight(self) -> MessageInfo | None:
        """The message whose answer is awaited, None when the line is quiet."""
        with self._lock:
            return self._written[0].info if self._written else None

    @property
    def queued(self) -> int:
        """How many messages wait in the FIFO."""
        with self._lock:
            return len(self._fifo)

    def add_listener(self, listener: Callable[[Event], None]) -> None:
        """Receive every :data:`Event`, on the executor's thread, in line order."""
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[Event], None]) -> None:
        """Stop *listener* receiving events."""
        self._listeners.remove(listener)

    def on_sent(self, hook: Callable[[MessageInfo], None]) -> None:
        """Call *hook* with each caller's message as it is written, on the thread."""
        self._sent_hooks.append(hook)

    def submit(self, message: str, *, timeout: float | None = None) -> Future[Reply]:
        """Queue *message*; its future gets the :class:`Reply`.

        :param message: One program message, without its newline and without
            the error query, which is appended.
        :param timeout: Seconds its answer may take once written; its class's by default.
        :returns: The future; it raises :class:`MeterTimeout` when the answer
            did not come, :class:`LinkDown` when the line went down first and
            :class:`ExecutorClosed` when the executor stopped first.
        :raises MessageRefused: When the instrument would discard it (nothing is sent).
        :raises LinkDown: When the line is down.
        :raises ExecutorBusy: When the FIFO is full.
        :raises ExecutorClosed: When the executor has stopped.
        :raises LineHeld: When the line is handed over to a passthrough client.
        """
        job = self._job(message, timeout)
        with self._lock:
            if self._closed or self._stopping:
                raise ExecutorClosed("the executor has stopped")
            if self._tap is not None:
                raise LineHeld("the line is handed over to a passthrough client")
            if self._down:
                raise LinkDown("the instrument is not answering")
            if len(self._fifo) >= self._queue_limit:
                raise ExecutorBusy(f"{len(self._fifo)} messages are waiting already")
            self._fifo.append(job)
        return job.future

    def call(
        self, message: str, *, timeout: float | None = None, wait: float | None = None
    ) -> Reply:
        """Submit *message* and wait for its :class:`Reply`.

        :param message: The program message.
        :param timeout: Seconds its answer may take once written.
        :param wait: Seconds of real time to wait in all, unbounded by default
            (the executor ends every future it holds).
        :raises MeterError: As :meth:`submit` and its future raise.
        """
        return self.submit(message, timeout=timeout).result(wait)

    def query_all(self, units: Sequence[str], *, wait: float | None = None) -> list[str]:
        """Ask every query of *units*, packed into as few messages as fit.

        :param units: Query units, no leading colon.
        :param wait: Seconds of real time each message may take in all.
        :returns: Their answers, in order.
        :raises MeterError: When the instrument queued an error, or answered
            fewer units than were asked.
        """
        answers: list[str] = []
        for message in pack_units(units):
            reply = self.call(message, wait=wait)
            if reply.errors:
                raise MeterError(f"{message!r}: " + "; ".join(map(str, reply.errors)), reply.errors)
            answers += reply.units
        if len(answers) != len(units):
            raise MeterError(f"{len(units)} queries answered {len(answers)} units")
        return answers

    def abort(self) -> Future[Reply]:
        """Send ``ABORt``: at once while an acquisition is in flight, else in its turn.

        While a ``MEASure``, ``READ`` or ``CALibration:SHUNt`` message is in
        flight the abort goes ahead of the FIFO, and the acquisition's message
        is answered first. If the acquisition has ended before the thread can
        write the abort, nothing is sent and the reply says ``sent=False``:
        an ``ABORt`` then would close whatever window is open, which nobody
        asked for. (An acquisition that ends while the abort is on the wire
        meets the same race on the instrument, which then runs it as an
        ordinary message.)

        :returns: The abort's future.
        """
        with self._lock:
            acquiring = bool(self._written) and self._written[0].info.acquisition
            if acquiring and not self._closed:
                job = self._job("ABOR", None)
                self._priority.append(job)
                return job.future
        return self.submit("ABOR")

    def close(self, *, wait: float = 5.0) -> None:
        """Stop the thread: what waits in the FIFO fails, the message in flight is waited for.

        :param wait: Seconds of real time to wait for the thread.
        """
        with self._lock:
            self._stopping = True
            dropped = [*self._fifo, *self._priority]
            self._fifo.clear()
            self._priority.clear()
        for job in dropped:
            _fail(job, ExecutorClosed(f"{job.info.message!r} was not sent: the executor stopped"))
        if self._thread is not None:
            self._thread.join(wait)
        with self._lock:
            self._closed = True
            left = list(self._written)
            self._written.clear()
        for job in left:
            _fail(job, ExecutorClosed(f"{job.info.message!r}: the executor stopped first"))

    def hand_over(self, tap: Callable[[bytes], None]) -> None:
        """Give the line to a passthrough client: see the module's description.

        :param tap: Called on the thread with every chunk the thread reads, as
            it came, before the chunk's stream records reach the listeners. It
            must not block: it only queues the bytes for the client.
        :raises ExecutorBusy: When a message is in flight or queued, or a sync runs.
        :raises LinkDown: When the line is down.
        :raises ExecutorClosed: When the executor has stopped.
        """
        with self._lock:
            if self._closed or self._stopping:
                raise ExecutorClosed("the executor has stopped")
            if self._down:
                raise LinkDown("the instrument is not answering")
            if self._tap is not None:
                raise ExecutorBusy("the line is handed over already")
            if self._written or self._fifo or self._priority or self._syncing:
                raise ExecutorBusy("a message is in flight or waiting")
            self._tap = tap

    def forward(self, data: bytes) -> None:
        """Write a passthrough client's *data* to the port as it came, on the caller's thread.

        :param data: The client's bytes.
        :raises LineHeld: When the line is not handed over (nothing is written).
        :raises OSError: When the port fails.
        """
        with self._wire:
            with self._lock:
                if self._tap is None:
                    raise LineHeld("the line is not handed over")
            self._transport.write(data)

    def take_back(self) -> None:
        """End the hand-over: the thread syncs, then sends what was submitted since.

        It waits for a :meth:`forward` that is writing, so nothing of the
        client's follows the sync's newline. A hand-over already ended is
        left as it is.
        """
        with self._wire, self._lock:
            if self._tap is None:
                return
            self._tap = None
            self._syncing, self._sync_since = True, None
            self._reset_router = True  # the client's unfinished answer is not the sync's

    def poll_now(self) -> None:
        """Poll the status at the next chance instead of at the next period."""
        with self._lock:
            self._next_poll = 0.0

    # -- the thread -----------------------------------------------------------

    def _job(self, message: str, timeout: float | None, *, internal: str = "") -> _Job:
        """A job for *message*, checked and classified.

        :raises MessageRefused: As :func:`check_message`.
        """
        wire = check_message(message)
        info = analyze(message)
        limit = self._timeouts[info.kind] if timeout is None else timeout
        return _Job(info, wire, limit, Future(), next(self._seq), internal)

    def _run(self) -> None:
        """The thread: write what is due, read what came, enforce the deadlines."""
        while True:
            with self._lock:
                if self._stopping and not self._written:
                    return
                tap = self._tap
                job = None if tap is not None else self._next_job()
                if self._reset_router:
                    self._reset_router = False
                    self._router.reset()
            try:
                if job is not None:
                    self._write(job)
                chunk = self._transport.read(4096)
            except OSError as error:
                self._lost(error)
                return
            if chunk:
                # Whoever holds the line when the chunk arrived owns it: a
                # hand-over made while the read waited gives the client the
                # answer to what it sent meanwhile, never the router.
                with self._lock:
                    tap = self._tap
            if chunk and tap is not None:
                self._tapped(tap, chunk)
            elif chunk:
                for event in self._router.feed(chunk):
                    self._route(event)
            self._enforce_deadlines()

    def _tapped(self, tap: Callable[[bytes], None], chunk: bytes) -> None:
        """Hand *chunk* to the passthrough's *tap*, and its stream records to the listeners."""
        try:
            tap(chunk)
        except Exception:  # noqa: BLE001  the tap's bug must not stop the line
            _LOG.exception("the passthrough's tap failed")
        for event in self._router.feed(chunk):
            if not isinstance(event, Response):  # a response is the client's alone
                self._emit(event)

    def _next_job(self) -> _Job | None:
        """The next message to write, under the lock; None when it must wait."""
        now = self._clock()
        if self._priority:
            if self._written and self._written[0].info.acquisition:
                return self._priority.popleft()
            if not self._written:  # the acquisition ended first: nothing to abort
                job = self._priority.popleft()
                reply = Reply(job.info.message, [], [], sent=False)
                job.future.set_result(reply)
                return None
        if self._written:
            return None
        if self._syncing:
            if self._sync_since is None:
                self._sync_since = now
            nonce = str(next(self._nonces))
            job = self._job(f"KMSYNC{nonce}", self._sync_timeout, internal="sync")
            job.nonce = nonce
            return job
        if (
            self._polling
            and self._poll_job is None
            and now >= self._next_poll
            and len(self._fifo) < self._queue_limit
        ):
            self._poll_job = self._job(POLL_MESSAGE, None, internal="poll")
            self._fifo.append(self._poll_job)
        return self._fifo.popleft() if self._fifo else None

    def _write(self, job: _Job) -> None:
        """Write *job*'s message and start its clock; a sync first ends any partial line.

        The job is in flight before its bytes leave, under the same lock that
        checks polling, so once polling is off and nothing is in flight no
        poll can follow.
        """
        with self._lock:
            if job.internal == "poll" and not self._polling:
                self._poll_job = None  # polling went off after the poll left the FIFO
                job.future.cancel()
            if not job.future.set_running_or_notify_cancel():
                return  # its caller cancelled it while it waited, or polling went off
            job.sent_at = self._clock()
            job.deadline = job.sent_at + job.timeout
            self._written.append(job)
        with self._wire:
            if job.internal == "sync":
                self._transport.write(b"\n")
            self._transport.write((job.wire + "\n").encode("ascii"))
        if not job.internal:
            for hook in self._sent_hooks:
                hook(job.info)

    def _route(self, event: Sample | Mark | BlockProgress | Overflow | Response) -> None:
        """Hand *event* to the job it answers, or to the listeners."""
        if not isinstance(event, Response):
            self._emit(event)
            return
        last = event.units[-1].strip() if event.units else ""
        with self._lock:
            job = self._written[0] if self._written else None
            ours = job is not None and _ERRORS_UNIT.fullmatch(last) is not None
            if ours and job is not None and job.internal == "sync":
                ours = any(
                    entry.code == -113 and entry.detail.upper() == f"KMSYNC{job.nonce}"
                    for entry in parse_errors(last)
                )
            if ours:
                self._written.popleft()
        if not ours or job is None:
            self._emit(StrayLine(list(event.units)))
            return
        errors = parse_errors(last)
        # A caller's reply keeps only its own message's errors; what the
        # instrument queued on its own goes to the listeners (a sync and a
        # poll give every error to the listeners already).
        drained = [] if job.internal else [e for e in errors if e.code in ASYNC_ERRORS]
        own = [entry for entry in errors if entry not in drained]
        reply = Reply(
            job.info.message, event.units[:-1], own, event.blocks, self._clock() - job.sent_at
        )
        if drained:
            self._emit(DeviceErrors(drained))
        if job.internal == "sync":
            self._synced(errors)
        elif job.internal == "poll":
            self._polled(reply)
        job.future.set_result(reply)

    def _synced(self, errors: list[ErrorEntry]) -> None:
        """The instrument answered a sync: the line is in step again."""
        others = [
            entry
            for entry in errors
            if entry.code != -113 or not entry.detail.upper().startswith("KMSYNC")
        ]
        with self._lock:
            self._syncing = self._down = False
            self._sync_since = None
            self._next_poll = 0.0
        self._ready.set()
        self._emit(LinkChanged(True))
        if others:
            self._emit(DeviceErrors(others))

    def _polled(self, reply: Reply) -> None:
        """A status poll's answer: the status, a reboot, errors nobody asked for."""
        status = Status(
            parse_fields(reply.units[0]) if reply.units else {},
            parse_fields(reply.units[1]) if len(reply.units) > 1 else {},
        )
        with self._lock:
            self._poll_job = None
            period = self.poll_capture if status.window_open else self.poll_idle
            self._next_poll = self._clock() + period
            previous, self._last_health = self._last_health, (self._clock(), status.health)
        self.status = status
        if previous is not None and _rebooted(*previous, *self._last_health):
            self._emit(Rebooted(status.health))
        self._emit(status)
        if reply.errors:
            self._emit(DeviceErrors(reply.errors))

    def _enforce_deadlines(self) -> None:
        """Fail what is overdue and start a sync; declare the line down when the sync is."""
        now = self._clock()
        with self._lock:
            if not self._written or now <= self._written[0].deadline:
                return
            overdue = list(self._written)
            self._written.clear()
            if not self._syncing:
                self._syncing, self._sync_since = True, now
            since = now if self._sync_since is None else self._sync_since
            down = not self._down and now - since >= self._link_timeout
            waiting: list[_Job] = []
            if down:
                self._down = True
                waiting = list(self._fifo)
                self._fifo.clear()
                self._poll_job = None
        self._router.reset()  # a partial answer of the late message is not the next one's
        for job in overdue:
            if job.internal == "poll":
                with self._lock:
                    self._poll_job = None
            _fail(job, MeterTimeout(f"{job.info.message!r}: no answer within {job.timeout:g} s"))
        if down:
            self._emit(LinkChanged(False))
        for job in waiting:
            _fail(job, LinkDown(f"{job.info.message!r} was not sent: the line is down"))

    def _lost(self, error: OSError) -> None:
        """The port failed: everything held fails, and the thread ends."""
        with self._lock:
            self._closed = self._down = True
            held = [*self._written, *self._priority, *self._fifo]
            self._written.clear()
            self._priority.clear()
            self._fifo.clear()
        _LOG.error("the port failed: %s", error)
        self._emit(LinkChanged(False))
        for job in held:
            _fail(job, LinkDown(f"{job.info.message!r}: the port failed ({error})"))

    def _emit(self, event: Event) -> None:
        """Give *event* to every listener; one that raises is logged and kept."""
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:  # noqa: BLE001  a listener's bug must not stop the line
                _LOG.exception("a listener failed on %s", type(event).__name__)


def _fail(job: _Job, error: MeterError) -> None:
    """End *job*'s future with *error* unless it has ended already."""
    if not job.future.done():
        job.future.set_exception(error)


def _rebooted(then: float, before: dict[str, str], now: float, after: dict[str, str]) -> bool:
    """Whether two ``SYSTem:HEALth?`` answers straddle a reboot or a reflash.

    The instrument's uptime should have grown by the time between the polls;
    one that grew by half a second less started again in between (a reboot
    takes the uptime back to its boot, which is less than the elapsed time
    since the earlier poll, and the earlier uptime was at least a second).

    :param then: When the earlier answer came, on the executor's clock.
    :param before: The earlier answer's fields.
    :param now: When the later one came.
    :param after: The later answer's fields.
    """
    try:
        expected_us = int(before["uptime_us"]) + (now - then) * 1e6
        if int(after["uptime_us"]) < expected_us - _REBOOT_SLACK_US:
            return True
    except (KeyError, ValueError):
        pass
    return before.get("elf") != after.get("elf") or before.get("fw") != after.get("fw")
