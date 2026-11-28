# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Live samples and events for the page: Server-Sent Events from a bounded ring.

:class:`LiveHub` listens to the executor (every stream record, status poll,
reboot, error and block progress the line carries) and publishes what it
learns as events, each with a sequence number, into a ring of bounded size
that ``GET /api/events`` serves as Server-Sent Events. The event kinds:

- ``samples``: every 100 ms, the samples that arrived since the last batch,
  as columns of integers in the device's units: ``t_us`` and, for each
  channel the batch carries, its value in thousandths of the record's unit
  (``i_na`` for ``i_ua``, ``v_uv`` for ``v_mv``, ``p_uw`` for ``p_mw``,
  ``die_mc`` for ``die_c``), ``null`` in a row that lacks it. ``n0`` is the
  first row's index among the rows of the capture serve started last (or of
  the hub's life before any), ``count`` the rows. The ``TEXT`` format's one
  line per channel is merged into one row per timestamp; ``CSV`` rows are
  rows already. A row is whole once it carries every channel of the row
  before it; one still waiting for a channel is never closed by the clock,
  however long the line takes to bring it.
- ``mark``: a mark record (``label``, ``n``, ``t_us``), in its place among
  the batches.
- ``status``: ``kind`` ``poll`` (the last status poll's reports, at most one
  per batch), ``link`` (the line went down or came back), ``reboot`` (the
  instrument rebooted; ``health`` after it), ``capture`` (a capture serve
  owns started or ended, from the recorder), ``settings`` (messages that
  change capture-shaping settings were written; ``changed`` names them, at
  most one per batch, and the page reads what it shows of them again),
  ``passthrough`` (a client of
  the SCPI passthrough attached, was refused, was dropped or detached, from
  :mod:`~kleidos_meter.panel.passthrough`), or ``hello``, the first event a
  stream gets: the last poll, capture and passthrough state, so a page
  starts drawn.
- ``error``: ``kind`` ``device`` (errors the instrument queued that no
  caller asked for), ``overflow`` (a line longer than the router holds) or
  ``recorder`` (a capture's files could not be written).
- ``sequence``: a measurement sequence's progress (published by the
  sequencer; nothing in this module makes one).
- ``ring``: a ring capture's fill from the polls (``samples``, ``running``)
  and a dump's progress (``dump``: ``received`` and ``total`` bytes), at most
  one of each per batch.
- ``lost``: sent to one stream only, never kept: the events that stream
  missed because it read more slowly than the ring turned over (``events``,
  and ``samples``, the sample rows among them, ``null`` when unknown).

Nothing here waits for a reader. The executor's thread only appends to the
open batch under a short lock; a thread of the hub's own closes a batch every
:attr:`~LiveHub.interval` and encodes it once for every stream; the ring
keeps at most ``ring_events`` events and ``ring_bytes`` bytes and forgets the
oldest first. A stream that falls behind the ring is told how much it lost
and goes on from the oldest event kept: a slow page costs itself events,
never the line a sample or the host memory. An event's id is
``<epoch>-<seq>-<rows>``: the hub's random epoch, the sequence number and
the sample rows published up to it, so a page that reconnects with
``Last-Event-ID`` resumes after it, and learns exactly how many samples it
missed when the ring has moved on; an id of another epoch (serve restarted)
resumes at the newest event, with the loss unknown.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections import deque
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

from ..protocol import (
    CHANNEL_INTEGER_KEYS,
    CSV_CHANNELS,
    BlockProgress,
    Mark,
    Overflow,
    Sample,
)
from .executor import DeviceErrors, Event, LinkChanged, MessageInfo, Rebooted, Status
from .reports import errors_json, typed_fields
from .shaping import changed_settings

__all__ = [
    "EVENT_KINDS",
    "ROW_IDLE_TICKS",
    "HubClosed",
    "LiveHub",
    "Subscription",
    "TooManyStreams",
    "batch_columns",
    "encode_event",
]

#: Every kind of event a stream carries, ``lost`` included.
EVENT_KINDS = ("samples", "mark", "status", "error", "sequence", "ring", "lost")

#: The ``status`` kinds whose newest event a stream's ``hello`` carries.
_KEPT_STATES = ("poll", "capture", "passthrough")

#: The integer columns of a ``samples`` batch, in order, after ``t_us``.
_COLUMNS = tuple(CHANNEL_INTEGER_KEYS[channel] for channel in CSV_CHANNELS)

#: The most bytes one read of a stream hands over at once.
_CHUNK_BYTES = 1 << 20

#: Batches a whole open row must stay unchanged before a tick closes it.
ROW_IDLE_TICKS = 2

#: What a stream is told first: how long a browser waits before it reconnects.
_RETRY = b"retry: 2000\n\n"


class HubClosed(Exception):
    """The hub has stopped: every stream ends."""


class TooManyStreams(Exception):
    """Every stream slot is taken; the page is told to come back later."""


def encode_event(kind: str, data: dict[str, Any], event_id: str | None = None) -> bytes:
    """One Server-Sent Event: its id when given, its kind, its data as one line of JSON.

    :param kind: The event's kind, one of :data:`EVENT_KINDS`.
    :param data: Its data, a JSON object.
    :param event_id: Its id, None for an event a stream must not resume after.
    :returns: The event's bytes, its blank line included.
    """
    head = f"id: {event_id}\n" if event_id is not None else ""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"{head}event: {kind}\ndata: {body}\n\n".encode()


def batch_columns(rows: list[tuple[int, dict[str, int]]], n0: int) -> dict[str, Any]:
    """A ``samples`` event's data: the rows as columns.

    :param rows: ``(t_us, values)`` rows; a row's values are keyed by the
        integer column names (``i_na``, ...).
    :param n0: The first row's index among the capture's rows.
    :returns: ``n0``, ``count``, ``t_us`` and a column for every channel any
        row carries, ``None`` where a row lacks it.
    """
    present = [name for name in _COLUMNS if any(name in values for _, values in rows)]
    data: dict[str, Any] = {"n0": n0, "count": len(rows), "t_us": [t for t, _ in rows]}
    for name in present:
        data[name] = [values.get(name) for _, values in rows]
    return data


@dataclass(frozen=True)
class _Entry:
    """One event in the ring.

    :ivar seq: Its sequence number.
    :ivar frame: Its encoded bytes.
    :ivar rows: The sample rows it carries (0 but for ``samples``).
    :ivar rows_before: The rows the hub published before it.
    """

    seq: int
    frame: bytes
    rows: int
    rows_before: int


@dataclass
class _Batch:
    """The rows gathered for the next ``samples`` event."""

    n0: int
    rows: list[tuple[int, dict[str, int]]] = field(default_factory=list)


@dataclass
class _Pending:
    """An event decided under the lock and encoded outside it."""

    kind: str
    data: dict[str, Any]
    rows: int
    rows_before: int


class LiveHub:
    """The live events: see the module's description.

    :param interval: Seconds between batches.
    :param ring_events: The most events the ring keeps.
    :param ring_bytes: The most encoded bytes the ring keeps (the newest
        event is kept whatever its size).
    :param max_pending_rows: The most sample rows waiting for a batch; a row
        past them is counted as ``overrun`` in the next batch and dropped.
    :param max_streams: The most streams served at once.
    :param epoch: The ids' epoch, random by default.
    """

    def __init__(
        self,
        *,
        interval: float = 0.1,
        ring_events: int = 600,
        ring_bytes: int = 16 << 20,
        max_pending_rows: int = 1 << 16,
        max_streams: int = 8,
        epoch: str | None = None,
    ) -> None:
        self.interval = interval
        self.ring_events = ring_events
        self.ring_bytes = ring_bytes
        self.max_pending_rows = max_pending_rows
        self.max_streams = max_streams
        if epoch is not None and not epoch.isalnum():
            raise ValueError(f"an epoch is letters and digits, not {epoch!r}")
        #: The ids' epoch: a fresh one each time serve starts.
        self.epoch = epoch or secrets.token_hex(4)
        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._publishing = threading.Lock()
        self._ring: deque[_Entry] = deque()
        self._ring_size = 0
        self._next_seq = 1
        self._closed = False
        self._streams = 0
        # What the line delivered since the last batch, under _lock.
        self._row: tuple[int, dict[str, int]] | None = None
        self._idle_ticks = 0  # batches closed since the open row last changed
        # The channels a whole row carries: those of the last row the next
        # timestamp closed, None before the capture's first such row.
        self._shape: frozenset[str] | None = None
        self._batch: _Batch | None = None
        self._pending: list[tuple[str, dict[str, Any], _Batch | None]] = []
        self._pending_rows = 0
        self._overrun = 0
        self._skip_t: int | None = None
        self._poll: dict[str, Any] | None = None
        self._settings: set[str] = set()  # the settings messages changed since the last batch
        self._ring_fill: dict[str, Any] | None = None
        self._dump: dict[str, int] | None = None
        self._rows_total = 0  # rows published or decided, the hub's life long
        self._capture_rows = 0  # rows since capture_started()
        self._last: dict[str, dict[str, Any]] = {}  # the last poll and capture states
        self._thread: threading.Thread | None = None

    # -- the hub's life -----------------------------------------------------------

    def start(self) -> None:
        """Start closing a batch every :attr:`interval`."""
        self._thread = threading.Thread(target=self._run, name="kleidos-meter-live", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Publish what is pending, end every stream and stop the batches."""
        self.flush()
        with self._changed:
            self._closed = True
            self._changed.notify_all()
        if self._thread is not None:
            self._thread.join()

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` was called."""
        return self._closed

    @property
    def streams(self) -> int:
        """How many streams are being served."""
        with self._lock:
            return self._streams

    @property
    def last_id(self) -> str:
        """The id of the newest event (``<epoch>-0-0`` before any)."""
        with self._lock:
            return self._id(self._next_seq - 1, self._rows_published())

    def _run(self) -> None:
        """The batch thread."""
        while True:
            with self._changed:
                if self._closed:
                    return
                self._changed.wait(self.interval)
            self.tick()  # publishes nothing once the hub is closed

    # -- what the line delivers (the executor's thread) -----------------------------

    def on_event(self, event: Event) -> None:
        """Take one of the executor's events; never waits for a stream.

        :param event: As the executor emits it, on its thread.
        """
        with self._lock:
            if isinstance(event, Sample):
                self._take_sample(event)
            elif isinstance(event, Mark):
                self._close_row()
                self._close_batch()
                data = {"label": event.label, "n": event.index, "t_us": event.t_us}
                self._pending.append(("mark", data, None))
            elif isinstance(event, Status):
                self._take_status(event)
            elif isinstance(event, BlockProgress):
                self._dump = {"received": event.received, "total": event.total}
            elif isinstance(event, Rebooted):
                data = {"kind": "reboot", "health": typed_fields(event.health)}
                self._pending.append(("status", data, None))
            elif isinstance(event, LinkChanged):
                self._pending.append(("status", {"kind": "link", "up": event.up}, None))
            elif isinstance(event, DeviceErrors):
                data = {"kind": "device", "errors": errors_json(event.errors)}
                self._pending.append(("error", data, None))
            elif isinstance(event, Overflow):
                data = {"kind": "overflow", "discarded": event.discarded}
                self._pending.append(("error", data, None))

    def on_sent(self, info: MessageInfo) -> None:
        """Take a message the executor wrote; never waits for a stream.

        :param info: The message, as :meth:`~kleidos_meter.panel.executor.Executor.on_sent`
            hands it.
        """
        changed = changed_settings(info)
        if changed:
            with self._lock:
                self._settings |= changed

    def _take_sample(self, sample: Sample) -> None:
        """Merge *sample* into the open row, or close that row and open one (under the lock)."""
        values = {
            CHANNEL_INTEGER_KEYS[name]: round(value * 1000)
            for name, value in sample.values.items()
            if name in CHANNEL_INTEGER_KEYS
        }
        if self._row is not None and self._row[0] == sample.t_us:
            self._row[1].update(values)
            self._idle_ticks = 0
            return
        if self._skip_t == sample.t_us:
            # The rest of a row that had no room, or that was closed already: a
            # channel the shape lacked makes the rows after it wait for it.
            self._shape = frozenset(values).union(self._shape or ())
            return
        if self._row is not None:
            self._shape = frozenset(self._row[1])  # the next timestamp: the row is whole
        self._close_row()
        if self._pending_rows >= self.max_pending_rows:
            self._overrun += 1
            self._skip_t = sample.t_us
            return
        self._skip_t = None
        self._row = (sample.t_us, values)
        self._pending_rows += 1
        self._idle_ticks = 0

    def _take_status(self, status: Status) -> None:
        """Keep the newest poll, and a ring capture's fill (under the lock)."""
        polled = typed_fields(status.status)
        self._poll = {"kind": "poll", "status": polled, "health": typed_fields(status.health)}
        if polled.get("sink") == "ring":
            self._ring_fill = {"samples": polled.get("samples"), "running": polled.get("running")}

    def _close_row(self) -> None:
        """Put the open row in the batch (under the lock)."""
        if self._row is None:
            return
        if self._batch is None:
            self._batch = _Batch(self._capture_rows)
        self._batch.rows.append(self._row)
        self._capture_rows += 1
        # A channel of this row that comes after it closed (a mark or a flush
        # closed it, or a tick whose shape lacked that channel) is dropped: it
        # would be a second row.
        self._skip_t = self._row[0]
        self._row = None

    def _close_batch(self) -> None:
        """Queue the batch as a ``samples`` event (under the lock)."""
        if self._batch is not None and self._batch.rows:
            self._pending.append(("samples", {}, self._batch))
        self._batch = None

    # -- publishing -------------------------------------------------------------------

    def capture_started(self) -> None:
        """Count rows from 0 again: a capture serve owns has started."""
        self.flush()
        with self._lock:
            self._capture_rows = 0
            self._skip_t = None
            self._shape = None  # its rows may carry other channels

    def flush(self) -> None:
        """Publish what is pending now, the open row included.

        The recorder calls it once a capture's last records have arrived, so
        that the event it publishes next follows every sample of the capture.
        """
        self._publish_pending(row="close")

    def tick(self) -> None:
        """Close a batch, as the batch thread does every :attr:`interval`.

        The open row is closed too once it is whole, every channel of the
        last row the next timestamp closed in it, and has not changed for
        :data:`ROW_IDLE_TICKS` batches. The ``TEXT`` format writes a sample's
        channels back to back, but the line can deliver them in two reads with
        any pause between them, so a row that lacks a channel stays open until
        that channel, the next timestamp, a mark or a flush closes it; nor is a
        row ever cut between its lines by an event published meanwhile.
        """
        self._publish_pending(row="tick")

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        """Publish an event of another part of serve, after what is pending.

        :param kind: One of :data:`EVENT_KINDS` but ``lost``.
        :param data: Its data; a ``status`` of ``kind`` ``capture`` or
            ``passthrough`` is also kept for the ``hello`` of the streams that
            start later.
        """
        self._publish_pending(row="keep", extra=(kind, data))

    def _publish_pending(
        self, *, row: str, extra: tuple[str, dict[str, Any]] | None = None
    ) -> None:
        """Decide the pending events under the lock, encode them outside it, and ring them.

        :param row: What becomes of the open row: ``close`` it, ``keep`` it
            open, or close it on a ``tick`` once it is whole and has stopped
            growing.
        :param extra: An event to publish after the pending ones.
        """
        with self._publishing:
            with self._lock:
                if self._closed:
                    return
                if row == "tick":
                    self._idle_ticks += 1
                if self._row is not None and (
                    row == "close" or (row == "tick" and self._row_settled(self._row[1]))
                ):
                    self._close_row()
                self._close_batch()  # a row still open goes in the next batch, after these
                decided = self._decide(extra)
            entries = [
                (item, encode_event(item.kind, item.data, None))
                for item in decided  # encoded once, for every stream
            ]
            with self._changed:
                for item, frame in entries:
                    seq = self._next_seq
                    self._next_seq += 1
                    event_id = self._id(seq, item.rows_before + item.rows)
                    entry = _Entry(
                        seq, f"id: {event_id}\n".encode() + frame, item.rows, item.rows_before
                    )
                    self._ring.append(entry)
                    self._ring_size += len(entry.frame)
                    if item.kind == "status" and item.data.get("kind") in _KEPT_STATES:
                        self._last[item.data["kind"]] = item.data
                while len(self._ring) > 1 and (
                    len(self._ring) > self.ring_events or self._ring_size > self.ring_bytes
                ):
                    self._ring_size -= len(self._ring.popleft().frame)
                if entries:
                    self._changed.notify_all()

    def _row_settled(self, values: dict[str, int]) -> bool:
        """Whether a tick closes the open row, *values* its channels (under the lock)."""
        if self._idle_ticks < ROW_IDLE_TICKS or self._shape is None:
            return False
        return self._shape <= values.keys()

    def _decide(self, extra: tuple[str, dict[str, Any]] | None) -> list[_Pending]:
        """The pending events in their order, with their row counts (under the lock)."""
        decided: list[_Pending] = []
        for kind, data, batch in self._pending:
            if batch is None:
                decided.append(_Pending(kind, data, 0, self._rows_total))
                continue
            payload = batch_columns(batch.rows, batch.n0)
            decided.append(_Pending("samples", payload, len(batch.rows), self._rows_total))
            self._rows_total += len(batch.rows)
        if decided and self._overrun:
            for item in reversed(decided):
                if item.kind == "samples":
                    item.data["overrun"] = self._overrun
                    self._overrun = 0
                    break
        self._pending = []
        self._pending_rows = 0 if self._row is None else 1
        if self._poll is not None:
            decided.append(_Pending("status", self._poll, 0, self._rows_total))
            self._poll = None
        if self._settings:
            data = {"kind": "settings", "changed": sorted(self._settings)}
            decided.append(_Pending("status", data, 0, self._rows_total))
            self._settings = set()
        ring: dict[str, Any] = {}
        if self._ring_fill is not None:
            ring.update(self._ring_fill)
            self._ring_fill = None
        if self._dump is not None:
            ring["dump"] = self._dump
            self._dump = None
        if ring:
            decided.append(_Pending("ring", ring, 0, self._rows_total))
        if extra is not None:
            decided.append(_Pending(extra[0], extra[1], 0, self._rows_total))
        return decided

    def _rows_published(self) -> int:
        """The rows the ring's newest event counts up to (under the lock)."""
        if not self._ring:
            return 0
        newest = self._ring[-1]
        return newest.rows_before + newest.rows

    def _id(self, seq: int, rows: int) -> str:
        """An event id: the epoch, the sequence number, the rows up to the event."""
        return f"{self.epoch}-{seq}-{rows}"

    # -- the streams -------------------------------------------------------------------

    def subscribe(self, last_event_id: str | None = None) -> Subscription:
        """A stream of the events from the next one, or after *last_event_id*.

        :param last_event_id: The ``Last-Event-ID`` a reconnecting page sent.
        :returns: The stream; :meth:`Subscription.frames` gives its bytes.
        :raises TooManyStreams: When :attr:`max_streams` are being served.
        :raises HubClosed: When the hub has stopped.
        """
        with self._lock:
            if self._closed:
                raise HubClosed("serve is stopping")
            if self._streams >= self.max_streams:
                raise TooManyStreams(f"{self.max_streams} streams are open already")
            self._streams += 1
            cursor, rows, notice = self._resume_point(last_event_id)
            hello = {
                "kind": "hello",
                "epoch": self.epoch,
                "poll": self._last.get("poll"),
                "capture": self._last.get("capture"),
                "passthrough": self._last.get("passthrough"),
            }
        first = _RETRY + encode_event("status", hello) + notice
        return Subscription(self, cursor, rows, first)

    def _resume_point(self, last_event_id: str | None) -> tuple[int, int | None, bytes]:
        """Where a stream starts, the rows it has seen, and a ``lost`` to send first.

        Called under the lock.
        """
        newest = self._next_seq
        rows_now = self._rows_published()
        if last_event_id is None:
            return newest, rows_now, b""
        epoch, seq, rows = _parse_id(last_event_id)
        if epoch != self.epoch or seq is None or seq >= newest:
            notice = encode_event("lost", {"events": None, "samples": None})
            return newest, rows_now, notice
        return seq + 1, rows, b""

    def _release(self) -> None:
        """A stream has ended."""
        with self._lock:
            self._streams -= 1


def _parse_id(text: str) -> tuple[str, int | None, int | None]:
    """An event id's epoch, sequence number and rows; None for a part that is not one."""
    parts = text.strip().split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts[1:]):
        return parts[0], None, None
    return parts[0], int(parts[1]), int(parts[2])


class Subscription:
    """One stream's place in the ring.

    :param hub: The hub it reads.
    :param cursor: The sequence number of the next event it is owed.
    :param rows: The sample rows published up to that event, None when unknown.
    :param first: The bytes it sends before any event.
    """

    def __init__(self, hub: LiveHub, cursor: int, rows: int | None, first: bytes) -> None:
        self._hub = hub
        self.cursor = cursor
        self.rows = rows
        self._first = first
        self._done = False
        #: How many events it lost by falling behind, over its life.
        self.lost_events = 0

    def next(self, timeout: float) -> bytes:
        """The events owed, waiting up to *timeout* for one; ``b""`` when none came.

        :param timeout: Seconds to wait.
        :returns: Their bytes, a ``lost`` event first when the ring moved past
            the stream; at most about a mebibyte at once.
        :raises HubClosed: When the hub has stopped.
        """
        hub = self._hub
        frames: list[bytes] = []
        with hub._changed:
            if not hub._closed and hub._next_seq <= self.cursor:
                hub._changed.wait(timeout)
            if hub._closed:
                raise HubClosed("serve is stopping")
            if hub._next_seq <= self.cursor:
                return b""
            oldest = hub._ring[0]  # the ring keeps its newest event, always
            if self.cursor < oldest.seq:
                missed = oldest.seq - self.cursor
                samples = None if self.rows is None else oldest.rows_before - self.rows
                frames.append(encode_event("lost", {"events": missed, "samples": samples}))
                self.lost_events += missed
                self.cursor = oldest.seq
            size = 0
            for index in range(self.cursor - oldest.seq, len(hub._ring)):
                entry = hub._ring[index]
                frames.append(entry.frame)  # a reference: the bytes are joined outside the lock
                self.cursor = entry.seq + 1
                self.rows = entry.rows_before + entry.rows
                size += len(entry.frame)
                if size >= _CHUNK_BYTES:
                    break
        return b"".join(frames)

    def frames(self, keepalive: float = 15.0) -> Generator[bytes, None, None]:
        """The stream's bytes, until the hub stops or the reader goes away.

        :param keepalive: Seconds of silence after which a comment line is sent,
            so a proxy or a browser does not take the stream for dead.
        :returns: An iterator; closing it ends the stream.
        """
        try:
            yield self._first
            while True:
                try:
                    chunk = self.next(keepalive)
                except HubClosed:
                    return
                yield chunk or b": keepalive\n\n"
        finally:
            self.close()

    def close(self) -> None:
        """End the stream; a second call does nothing."""
        if not self._done:
            self._done = True
            self._hub._release()
