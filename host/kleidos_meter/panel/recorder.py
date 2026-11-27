# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every capture serve owns, recorded to a run directory, and what is read back from one.

:class:`Recorder` starts a capture through the session's executor, writes
what the line carries for it, and closes it; each capture is a directory of
its own under the data directory (``$XDG_STATE_HOME/kleidos-meter/runs`` by
default), named ``<UTC start>-<profile>-<6 hex>``, with:

- ``stream.txt``: the capture's stream records as the instrument wrote them,
  one per line, in the stream's own format (``TEXT`` or ``CSV``, whichever
  ``FORMat`` was; serve never changes it): the samples of a streamed capture,
  the marks of any.
- ``trace.raw``: a ring capture's ``TRACe:DATA? RAW`` block, written only
  once its CRC-32 matched (a block that fails twice is kept as
  ``trace.damaged`` and the run says so).
- ``meta.json`` (``kleidos.meter.run/1``): what makes the files
  self-describing. The instrument (``*IDN?``, ``SYSTem:INFormation?``), every
  shaping setting as read at the start, the capture's ``FETCh:HEADer?`` and
  its ``FETCh:STATistics?`` (each unit as the instrument wrote it and its
  fields), the INA228's averaging as that header says it
  (``noise_reduction``: its state, the count, the conversion time and the
  sample's period, with the averaging off as with it on), what the stream
  and the trace hold (records, sample rows, marks, bytes, SHA-256, the first
  and last timestamp, records the host could not write), the errors the
  instrument queued meanwhile, how the capture ended, and the host's wall
  clock at the start and the end (labeled as the host's: the instrument has
  no calendar clock). ``complete`` is true when the capture closed normally
  and the stream carried every sample the instrument says it streamed
  (``samples - dropped``), or the ring dump holds every sample the ring kept.

A data directory inside a git work tree is refused (:func:`check_data_dir`):
a run carries the instrument's serial, and measurements that are filed go
through the Kleidos harness, never a commit of a run directory.

:class:`Run` reads a run back: its records (:meth:`Run.records`, merged into
one row per sample), its exports (``text``, ``csv`` and ``json``, each made
from the files and read back by :func:`~kleidos_meter.protocol.parse_record`
as the instrument's own records are) and a min/max decimation for a chart
(:meth:`Run.decimate`).

The executor's thread only appends a record to a bounded list; a writer
thread of the run's own writes it to disk, so a slow disk costs records
(counted in ``stream.lost``), never the line a sample.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, TypeGuard

from ..client import MeterError
from ..invariant import present
from ..protocol import (
    CHANNEL_INTEGER_KEYS,
    CSV_CHANNELS,
    Dump,
    ErrorEntry,
    Mark,
    Sample,
    check_dump,
    decode_dump,
    format_thousandths,
    parse_fields,
    parse_record,
    quote,
)
from .executor import DeviceErrors, Event, LinkChanged, Rebooted, Status, pack_units
from .paths import shown_error, shown_path
from .reports import errors_json, status_for, typed_fields
from .schema import MARK_LABEL_EXCLUDED, MARK_LABEL_MAX, RUN_LABEL_MAX, SCHEMA

if TYPE_CHECKING:  # serve imports this module: the types are for the checker only
    from .live import LiveHub
    from .serve import Session

__all__ = [
    "EXPORTS",
    "EXPORT_SCHEMA",
    "RING_PROFILES",
    "RUN_SCHEMA",
    "CaptureRefused",
    "DataDirRefused",
    "Recorder",
    "Row",
    "Run",
    "check_data_dir",
    "mark_label_ok",
    "run_id_ok",
    "run_label_ok",
    "work_tree_of",
]

#: ``meta.json``'s schema.
RUN_SCHEMA = "kleidos.meter.run/1"

#: The JSON export's schema.
EXPORT_SCHEMA = "kleidos.meter.export/1"

#: The profiles that capture to the ring, as ``SENSe:PROFile?`` answers them.
RING_PROFILES = frozenset({"TRACE", "BURST"})

#: Each export: its file's extension and its content type.
EXPORTS: dict[str, tuple[str, str]] = {
    "text": ("txt", "text/plain; charset=utf-8"),
    "csv": ("csv", "text/csv; charset=utf-8"),
    "json": ("json", "application/json; charset=utf-8"),
}

#: The most buckets a decimation answers.
MAX_BUCKETS = 10_000

#: The longest a capture may be asked to last, seconds: a week.
MAX_SECONDS = 7 * 24 * 3600.0

#: A run directory's name.
_RUN_ID = re.compile(r"\d{8}T\d{6}Z-[A-Z]{5}-[0-9a-f]{6}")

#: A label ``TRACe:MARK`` takes: 1 to 15 printable characters, no space, none of ``,;="'``.
_MARK_LABEL = re.compile(rf"[!-~]{{1,{MARK_LABEL_MAX}}}")

#: The bytes of a run's label kept in its ``meta.json``.
_MAX_LABEL = RUN_LABEL_MAX


def mark_label_ok(label: object) -> TypeGuard[str]:
    """Whether *label* is a label ``TRACe:MARK`` takes.

    :param label: What a request gave.
    :returns: True for 1 to :data:`~kleidos_meter.panel.schema.MARK_LABEL_MAX`
        printable characters with no space and none of ``,;="'``.
    """
    return (
        isinstance(label, str)
        and _MARK_LABEL.fullmatch(label) is not None
        and not any(char in label for char in MARK_LABEL_EXCLUDED)
    )


def run_label_ok(label: object) -> TypeGuard[str]:
    """Whether *label* is a label a run keeps.

    :param label: What a request gave.
    :returns: True for printable ASCII of at most
        :data:`~kleidos_meter.panel.schema.RUN_LABEL_MAX` characters.
    """
    return (
        isinstance(label, str) and len(label) <= _MAX_LABEL and all(" " <= c <= "~" for c in label)
    )


#: How the instrument's channel names map to the integer columns.
_INTEGER_OF = CHANNEL_INTEGER_KEYS
_CHANNEL_OF = {key: channel for channel, key in CHANNEL_INTEGER_KEYS.items()}

#: A sample row: its timestamp and each channel's value in thousandths of its unit.
Row = tuple[int, dict[str, int]]


class DataDirRefused(ValueError):
    """A data directory recordings must not go to."""


class CaptureRefused(Exception):
    """A capture route the recorder refuses, with the HTTP status it maps to.

    :param status: 400, 409 or 503.
    :param error: A short machine word for the page.
    :param detail: What went wrong, for a person.
    :param errors: The instrument's errors, when it refused.
    """

    def __init__(
        self, status: int, error: str, detail: str, errors: list[ErrorEntry] | None = None
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail
        self.errors = list(errors or [])


def work_tree_of(path: Path) -> Path | None:
    """The git work tree *path* is inside, or None.

    :param path: A directory that may not exist yet.
    :returns: The nearest directory at or above *path* that holds a ``.git``
        entry (a directory, or the file of a linked work tree).
    """
    resolved = path.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def check_data_dir(path: Path) -> Path:
    """*path*, absolute, when recordings may go there.

    :param path: The data directory asked for.
    :returns: It, resolved.
    :raises DataDirRefused: When it is inside a git work tree, or is a file.
    """
    resolved = path.expanduser().resolve()
    tree = work_tree_of(resolved)
    if tree is not None:
        raise DataDirRefused(
            f"{resolved} is inside the git work tree {tree}: recordings never go in a"
            " repository (filed measurements go through the Kleidos harness); choose"
            " --data-dir outside it"
        )
    if resolved.exists() and not resolved.is_dir():
        raise DataDirRefused(f"{resolved} is not a directory")
    return resolved


def run_id_ok(run_id: str) -> bool:
    """Whether *run_id* is a run directory's name, and so safe in a path."""
    return _RUN_ID.fullmatch(run_id) is not None


def _utc_now() -> dt.datetime:
    """The host's wall clock, in UTC."""
    return dt.datetime.now(dt.UTC)


def _iso(moment: dt.datetime) -> str:
    """*moment* as ISO 8601 to the millisecond."""
    return moment.isoformat(timespec="milliseconds")


def _noise_reduction(header: Mapping[str, Any]) -> dict[str, Any] | None:
    """The INA228's averaging in a capture, as its header says it was taken.

    :param header: ``FETCh:HEADer?``'s fields.
    :returns: ``state`` (``off``: the profile's own conversion; ``on``: the
        averaging settings'), the ``count`` averaged into a sample, the shunt's
        ``conversion_us`` and the sample's ``period_us``, whichever the state;
        None for an image whose header has no ``avg_state`` (before 0.18.0).
    """
    if "avg_state" not in header:
        return None
    fields = typed_fields(header)
    return {
        "state": fields["avg_state"],
        "count": fields["avg"],
        "conversion_us": fields["vshct_us"],
        "period_us": fields["period_us"],
    }


def _report(unit: str) -> dict[str, Any]:
    """A report unit as it is kept: the text and its fields."""
    return {"unit": unit, "fields": parse_fields(unit)}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Write *value* to *path* through a temporary file, so a reader never sees half of it."""
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


class _Recording:
    """The files of a run being written, and the writer thread that writes them.

    :param path: The run's directory, created.
    :param max_pending: The most records waiting for the writer.
    :param interval: Seconds between the writer's passes.
    """

    def __init__(self, path: Path, *, max_pending: int, interval: float) -> None:
        self.path = path
        self._max_pending = max_pending
        self._interval = interval
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: list[str] = []
        self._stream: IO[bytes] = (path / "stream.txt").open("wb")
        self._open = True
        self._hash = hashlib.sha256()
        self._done = threading.Event()
        self.records = 0
        self.rows = 0
        self.marks: list[dict[str, Any]] = []
        self.lost = 0
        self.bytes = 0
        self.first_t: int | None = None
        self.last_t: int | None = None
        self.error: str | None = None
        self._last_sample_t: int | None = None
        self._thread = threading.Thread(
            target=self._run, name="kleidos-meter-recorder", daemon=True
        )
        self._thread.start()

    def append(self, record: Sample | Mark) -> None:
        """Queue *record*'s line for the writer (the executor's thread); never waits on the disk."""
        with self._lock:
            if self.error is not None or len(self._pending) >= self._max_pending:
                self.lost += 1
                return
            self._pending.append(record.line)
            self.records += 1
            if isinstance(record, Sample):
                if record.t_us != self._last_sample_t:
                    self.rows += 1
                    self._last_sample_t = record.t_us
                if self.first_t is None:
                    self.first_t = record.t_us
                self.last_t = record.t_us
            else:
                self.marks.append({"label": record.label, "n": record.index, "t_us": record.t_us})

    def _run(self) -> None:
        """The writer thread: what is queued, to disk, every interval."""
        while not self._done.wait(self._interval):
            self.drain()

    def drain(self) -> None:
        """Write what is queued now."""
        with self._write_lock:
            with self._lock:
                lines, self._pending = self._pending, []
            if not lines or not self._open:
                return
            data = ("\n".join(lines) + "\n").encode("utf-8")
            try:
                self._stream.write(data)
                self._stream.flush()
            except OSError as error:
                with self._lock:
                    self.error = f"writing stream.txt failed: {shown_error(error)}"
                    self.lost += len(lines)
                    self.records -= len(lines)
                return
            self._hash.update(data)
            self.bytes += len(data)

    def finish(self) -> dict[str, Any]:
        """Write what is left, close the file, and say what it holds.

        :returns: The ``stream`` block of ``meta.json``.
        """
        self._done.set()
        self._thread.join()
        self.drain()
        with self._write_lock:
            self._open = False
            self._stream.close()
        return {
            "file": "stream.txt",
            "records": self.records,
            "samples": self.rows,
            "marks": len(self.marks),
            "bytes": self.bytes,
            "sha256": self._hash.hexdigest(),
            "first_t_us": self.first_t,
            "last_t_us": self.last_t,
            "lost": self.lost,
            "error": self.error,
        }


@dataclass
class _Capture:
    """The capture serve owns, while it runs.

    :ivar recording: Its files.
    :ivar meta: Its ``meta.json`` so far.
    :ivar profile: Its profile, as ``SENSe:PROFile?`` answered.
    :ivar started: When it started, on the recorder's clock.
    :ivar seconds: How long it was asked to last, None for until stopped.
    :ivar errors: What the instrument queued while it ran.
    """

    recording: _Recording
    meta: dict[str, Any]
    profile: str
    started: float
    seconds: float | None
    errors: list[ErrorEntry] = field(default_factory=list)

    @property
    def run_id(self) -> str:
        """Its run directory's name."""
        return self.recording.path.name


class Recorder:
    """Start, mark and stop the captures serve owns, and record each: see the module's description.

    :param session: Serve's session: its executor carries every message,
        its shaping guard reads the settings, its identity goes in the run.
    :param data_dir: Where the run directories go, checked by :func:`check_data_dir`.
    :param live: The hub the capture's state is published on, when there is one.
    :param clock: The monotonic clock a capture's ``seconds`` are measured on.
    :param wall: The host's wall clock, for the run's name and its times.
    :param interval: Seconds between the writer's passes and the timer's looks.
    :param max_pending: The most records waiting for the writer.
    :param tool: What made the runs, as ``meta.json`` names it.
    :raises DataDirRefused: As :func:`check_data_dir`.
    """

    def __init__(
        self,
        session: Session,
        data_dir: Path,
        *,
        live: LiveHub | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], dt.datetime] = _utc_now,
        interval: float = 0.1,
        max_pending: int = 1 << 20,
        tool: str = "kleidos-meter serve",
    ) -> None:
        self.session = session
        self.executor = session.executor
        self.data_dir = check_data_dir(data_dir)
        self.live = live
        self.clock = clock
        self.wall = wall
        self.interval = interval
        self.max_pending = max_pending
        self.tool = tool
        self._lock = threading.Lock()
        self._state = "idle"
        self._capture: _Capture | None = None
        #: The run whose files are still being written: from its start until
        #: its ``meta.json`` is final. Its exports are refused meanwhile.
        self._writing: str | None = None
        self._ending = False
        self._closed = False
        self._idle = threading.Event()
        self._idle.set()
        #: The last run this recorder finished, its ``meta.json``; None before.
        self.last_meta: dict[str, Any] | None = None
        self.executor.add_listener(self.on_event)

    # -- the state -----------------------------------------------------------------

    @property
    def state(self) -> str:
        """``idle``, ``starting``, ``running`` or ``stopping``."""
        with self._lock:
            return self._state

    def state_json(self) -> dict[str, Any]:
        """The recorder's state for the page: the capture running, or the last one.

        ``data_dir`` is named as :func:`~kleidos_meter.panel.paths.shown_path`
        shows it, never by its absolute path.
        """
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> dict[str, Any]:
        """:meth:`state_json`, its caller holding the lock."""
        capture = self._capture
        state = self._state
        body: dict[str, Any] = {"state": state, "run": None, "data_dir": shown_path(self.data_dir)}
        if capture is not None:
            body.update(
                run=capture.run_id,
                profile=capture.profile,
                seconds=capture.seconds,
                elapsed_s=round(self.clock() - capture.started, 3),
                samples=capture.recording.rows,
                marks=len(capture.recording.marks),
                noise_reduction=capture.meta["noise_reduction"],
            )
        if self.last_meta is not None:
            body["last"] = Run.summary_of(self.last_meta)
        return body

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until no capture runs.

        :param timeout: Seconds of real time; None waits for ever.
        :returns: Whether it is idle.
        """
        return self._idle.wait(timeout)

    # -- start, mark, stop -----------------------------------------------------------

    def start(
        self,
        profile: object,
        channels: object = None,
        seconds: object = None,
        label: object = "",
    ) -> dict[str, Any]:
        """Configure and start a capture, recording it.

        :param profile: The profile, as ``SENSe:PROFile`` takes it (any case, long or short).
        :param channels: A list of ``CURR``, ``VOLT``, ``POW``, ``TEMP``, or
            ``DEFault``; None keeps the instrument's.
        :param seconds: When given, the capture is stopped after it (on :attr:`clock`).
        :param label: A note kept in ``meta.json``.
        :returns: :meth:`state_json` as the capture began to run, its run named
            even when a short ``seconds`` has ended it by the time this returns.
        :raises CaptureRefused: When a capture runs, a window serve did not open
            is open, a parameter is malformed (400), or the instrument refused (its status).
        :raises MeterError: When the line fails meanwhile.
        """
        units = self._start_units(profile, channels)
        limit = _seconds(seconds)
        note = _label(label)
        with self._lock:
            if self._state != "idle":
                raise CaptureRefused(409, "capture_running", "a capture is running already")
            if self.session.foreign_window is not None:
                raise CaptureRefused(
                    409,
                    "window_open",
                    "a window serve did not open is open: it is left alone",
                )
            self._state = "starting"
            self._idle.clear()
        try:
            return self._start(units, limit, note)
        except BaseException:
            with self._lock:
                self._state = "idle"
                self._capture = None
                self._writing = None
            self._idle.set()
            raise

    def _start_units(self, profile: object, channels: object) -> list[str]:
        """The units that configure the capture, checked for their form.

        :raises CaptureRefused: With 400 for a malformed parameter.
        """
        profile_setting = present(SCHEMA.setting("profile"), "the profile setting")
        channel_setting = present(SCHEMA.setting("channels"), "the channels setting")
        try:
            units = [profile_setting.write_unit(profile_setting.parameter(profile))]
            if channels is not None:
                units.append(channel_setting.write_unit(channel_setting.parameter(channels)))
        except ValueError as why:
            raise CaptureRefused(400, "bad_parameter", f"the capture takes {why}") from None
        return units

    def _start(self, units: list[str], seconds: float | None, label: str) -> dict[str, Any]:
        """Configure, read the settings, open the run, ``INITiate`` (state ``starting``)."""
        executor = self.executor
        errors: list[ErrorEntry] = []
        for message in pack_units(units):
            errors += executor.call(message).errors
        if errors:
            raise CaptureRefused(
                status_for(entry.code for entry in errors),
                "refused",
                "the instrument refused the capture's settings",
                errors,
            )
        settings = self.session.shaping.read()
        profile = settings.get("profile", "").strip().upper()
        recording = self._open_run(profile)
        meta = self._meta(recording.path.name, profile, settings, seconds, label)
        capture = _Capture(recording, meta, profile, self.clock(), seconds)
        with self._lock:
            self._capture = capture  # records are kept from here, before INITiate is answered
            self._writing = capture.run_id
        if self.live is not None:
            self.live.capture_started()  # the rows are counted from the capture's first
        try:
            reply = executor.call("INIT;:FETC:HEAD?")
        except MeterError:
            self._discard(capture)
            raise
        if reply.errors or not reply.units:
            self._discard(capture)
            raise CaptureRefused(
                status_for(entry.code for entry in reply.errors) if reply.errors else 502,
                "refused",
                "the instrument did not start the capture",
                reply.errors,
            )
        meta["header"] = _report(reply.units[0])
        meta["noise_reduction"] = _noise_reduction(meta["header"]["fields"])
        try:
            _write_json(recording.path / "meta.json", meta)
        except OSError as error:  # the capture runs: the run's end writes it again
            capture.meta["failure"] = f"writing meta.json failed: {shown_error(error)}"
        with self._lock:
            capture.started = self.clock()
            self._state = "running"
            # The state as it began, before the timer can end it: read afterwards,
            # a short capture could already be over and name no run.
            running = self._state_locked()
        if self.live is not None:
            self.live.publish("status", {"kind": "capture", **running})
        if seconds is not None:
            threading.Thread(
                target=self._timer, args=(capture,), name="kleidos-meter-timer", daemon=True
            ).start()
        return running

    def _open_run(self, profile: str) -> _Recording:
        """Create the run's directory and start its writer."""
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = f"{self.wall().strftime('%Y%m%dT%H%M%SZ')}-{profile[:5]:X<5}-{secrets.token_hex(3)}"
        path = self.data_dir / name
        path.mkdir(mode=0o700)
        return _Recording(path, max_pending=self.max_pending, interval=self.interval)

    def _meta(
        self,
        run_id: str,
        profile: str,
        settings: dict[str, str],
        seconds: float | None,
        label: str,
    ) -> dict[str, Any]:
        """``meta.json`` at the start."""
        from .. import __version__

        session = self.session
        return {
            "schema": RUN_SCHEMA,
            "id": run_id,
            "state": "recording",
            "complete": False,
            "label": label,
            "profile": profile,
            "format": settings.get("format", "").strip().upper(),
            "request": {"seconds": seconds},
            "host": {
                "clock": "the host's wall clock, UTC; the instrument has no calendar clock",
                "started": _iso(self.wall()),
                "ended": None,
                "tool": f"{self.tool} {__version__}",
            },
            "instrument": {"idn": session.idn, "info": dict(session.info)},
            "settings": settings,
            "header": None,
            "noise_reduction": None,
            "statistics": None,
            "stream": None,
            "trace": None,
            "ended_by": None,
            "errors": [],
        }

    def _discard(self, capture: _Capture) -> None:
        """Remove a run that never started."""
        with self._lock:
            self._capture = None
            self._writing = None
        capture.recording.finish()
        shutil.rmtree(capture.recording.path, ignore_errors=True)

    def mark(self, label: object) -> dict[str, Any]:
        """Mark the running capture (``TRACe:MARK``).

        :param label: 1 to 15 printable characters, no space, none of ``,;="'``.
        :returns: The instrument's errors for the mark, as JSON.
        :raises CaptureRefused: When no capture of serve's runs (409), the label is malformed (400).
        """
        if not mark_label_ok(label):
            raise CaptureRefused(
                400,
                "bad_label",
                "a mark is 1 to 15 printable characters, with no space and none of , ; = \" '",
            )
        with self._lock:
            if self._state != "running":
                raise CaptureRefused(409, "no_capture", "no capture of serve's is running")
        reply = self.executor.call(f"TRAC:MARK {quote(label)}")
        return {"label": label, "errors": errors_json(reply.errors), "codes": reply.codes}

    def stop(self, reason: str = "stop") -> dict[str, Any]:
        """Close the running capture, read its statistics (and its ring), and finish its run.

        :param reason: How it ended, as ``meta.json``'s ``ended_by`` says:
            ``stop``, ``seconds``, ``ended_by_itself``, ``shutdown``, or
            ``reboot`` and ``link_down``, which send nothing.
        :returns: The run's ``meta.json``.
        :raises CaptureRefused: When no capture of serve's is running.
        """
        with self._lock:
            capture = self._capture
            if self._state != "running" or capture is None:
                raise CaptureRefused(409, "no_capture", "no capture of serve's is running")
            self._state = "stopping"
        stats = ""
        trace: dict[str, Any] | None = None
        failure: str | None = None
        if reason not in ("reboot", "link_down"):
            try:
                reply = self.executor.call("ABOR;:FETC:STAT?")
                capture.errors += reply.errors
                stats = reply.units[0] if reply.units else ""
                if capture.profile in RING_PROFILES and stats:
                    trace = self._fetch_trace(capture)
            except MeterError as error:
                failure = str(error) or type(error).__name__
        return self._finish(capture, reason, stats, trace, failure)

    def _fetch_trace(self, capture: _Capture) -> dict[str, Any] | None:
        """Dump the ring into ``trace.raw``, trying once more when the CRC fails.

        The header, the ring's sample count and the ``RAW`` dump are read in
        one message, so all three describe the same capture.

        :returns: The ``trace`` block of ``meta.json``, None when no block came.
        """
        path = capture.recording.path
        failure = ""
        block = b""
        for _ in range(2):
            reply = self.executor.call("FETC:HEAD?;:TRAC:POIN?;:TRAC:DATA? RAW")
            capture.errors += reply.errors
            if not reply.blocks or len(reply.units) < 2:
                return None
            block = reply.blocks[0]
            try:
                crc = check_dump(block)
                dump = decode_dump(block, reply.units[0])
            except ValueError as error:
                failure = str(error)
                continue
            temporary = path / "trace.raw.tmp"
            temporary.write_bytes(block)
            os.replace(temporary, path / "trace.raw")
            last = dump.start + dump.count - 1
            return {
                "file": "trace.raw",
                "format": "RAW",
                "bytes": len(block),
                "sha256": hashlib.sha256(block).hexdigest(),
                "crc32": "checked" if crc else "none",
                "count": dump.count,
                "points": int(reply.units[1]),
                "start": dump.start,
                "first_t_us": dump.t_us(dump.start) if dump.count else None,
                "last_t_us": dump.t_us(last) if dump.count else None,
                "dump_header": _report(reply.units[0]),
            }
        (path / "trace.damaged").write_bytes(block)
        return {"file": None, "damaged": "trace.damaged", "error": failure}

    def _finish(
        self,
        capture: _Capture,
        reason: str,
        stats: str,
        trace: dict[str, Any] | None,
        failure: str | None,
    ) -> dict[str, Any]:
        """Write the run's last records and its ``meta.json``, and go idle."""
        if self.live is not None:
            self.live.flush()  # every sample of the capture is published before its end
        with self._lock:
            self._capture = None  # nothing more is recorded
        stream = capture.recording.finish()
        meta = capture.meta
        meta["host"]["ended"] = _iso(self.wall())
        meta["ended_by"] = reason
        meta["statistics"] = _report(stats) if stats else None
        meta["stream"] = stream
        meta["trace"] = trace
        meta["errors"] = errors_json(capture.errors)
        if failure is not None:
            meta["failure"] = failure
        meta["complete"] = _complete(meta)
        meta["state"] = "complete" if stats else "incomplete"
        try:
            _write_json(capture.recording.path / "meta.json", meta)
        except OSError as error:
            meta["failure"] = f"writing meta.json failed: {shown_error(error)}"
            if self.live is not None:
                self.live.publish("error", {"kind": "recorder", "detail": meta["failure"]})
        self.last_meta = meta
        with self._lock:
            self._state = "idle"
            self._ending = False
            self._writing = None
        self._idle.set()
        if self.live is not None:
            self.live.publish("status", {"kind": "capture", **self.state_json()})
        return meta

    def close(self) -> None:
        """Stop a capture of serve's still running (serve is stopping); again, nothing."""
        if self._closed:
            return
        self._closed = True
        if self.state == "running":
            with contextlib.suppress(CaptureRefused):  # it ended meanwhile
                self.stop("shutdown")
        self.wait_idle(60.0)
        self.executor.remove_listener(self.on_event)

    # -- what the line carries (the executor's thread) -----------------------------------

    def on_event(self, event: Event) -> None:
        """Record a capture's records; end it when it ended by itself, or the instrument rebooted.

        :param event: As the executor emits it, on its thread; nothing here waits.
        """
        if isinstance(event, Sample | Mark):
            with self._lock:
                capture = self._capture
            if capture is not None:
                capture.recording.append(event)
            return
        reason = None
        if isinstance(event, Status) and event.status.get("running") == "0":
            reason = "ended_by_itself"
        elif isinstance(event, Rebooted):
            reason = "reboot"
        elif isinstance(event, LinkChanged) and not event.up:
            reason = "link_down"
        elif isinstance(event, DeviceErrors):
            with self._lock:
                if self._capture is not None:
                    self._capture.errors += event.errors
            return
        if reason is not None:
            self._end_async(reason)

    def _end_async(self, reason: str) -> None:
        """Stop the running capture on a thread of its own, once."""
        with self._lock:
            if self._state != "running" or self._ending:
                return
            self._ending = True
        threading.Thread(
            target=self._stop_quietly, args=(reason,), name="kleidos-meter-end", daemon=True
        ).start()

    def _stop_quietly(self, reason: str) -> None:
        """:meth:`stop`, for a thread: a capture someone else stopped first is no fault."""
        with contextlib.suppress(CaptureRefused):
            self.stop(reason)

    def _timer(self, capture: _Capture) -> None:
        """Stop *capture* once its seconds have passed on :attr:`clock`."""
        seconds = present(capture.seconds, "a timed capture's seconds")
        while True:
            with self._lock:
                if self._capture is not capture:
                    return  # stopped some other way
                due = capture.started + seconds
            if self.clock() >= due:
                self._end_async("seconds")
                return
            self._idle.wait(self.interval / 10)

    # -- the runs on disk -----------------------------------------------------------------

    def runs(self, limit: int = 100) -> list[dict[str, Any]]:
        """The runs in the data directory, newest first.

        :param limit: The most to list.
        :returns: Each run's summary (:meth:`Run.summary_of`).
        """
        if not self.data_dir.is_dir():
            return []
        names = sorted((p.name for p in self.data_dir.iterdir() if run_id_ok(p.name)), reverse=True)
        return [self.run(name).summary() for name in names[:limit]]

    def writing(self, run_id: str) -> bool:
        """Whether *run_id* is the run whose files this recorder still writes."""
        with self._lock:
            return self._writing == run_id

    def run(self, run_id: str) -> Run:
        """The run called *run_id*.

        :raises CaptureRefused: With 404 when there is none (or the name is not a run's).
        """
        path = self.data_dir / run_id
        if not run_id_ok(run_id) or not path.is_dir():
            raise CaptureRefused(404, "no_run", f"no run {run_id!r}")
        return Run(path, recording=self.writing(run_id))


def _seconds(value: object) -> float | None:
    """A capture's ``seconds``: None, or a number above 0 and at most a week.

    :raises CaptureRefused: With 400 otherwise.
    """
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not 0 < value <= MAX_SECONDS
    ):
        raise CaptureRefused(
            400, "bad_parameter", f"seconds is a number above 0, at most {MAX_SECONDS:g}"
        )
    return float(value)


def _label(value: object) -> str:
    """A run's label: printable ASCII, at most :data:`_MAX_LABEL` characters.

    :raises CaptureRefused: With 400 otherwise.
    """
    if value is None:
        return ""
    if not run_label_ok(value):
        raise CaptureRefused(
            400, "bad_parameter", f"a label is printable ASCII, at most {_MAX_LABEL}"
        )
    return value


def _complete(meta: dict[str, Any]) -> bool:
    """Whether the run holds every sample the instrument says it delivered.

    A streamed capture's stream carried ``samples - dropped``; a ring dump
    holds every sample the ring kept (``TRACe:POINts?``); a statistics-only
    capture carries none. Records the host lost or could not write make a
    run incomplete.
    """
    stats = (meta.get("statistics") or {}).get("fields", {})
    settings = meta.get("settings") or {}
    stream = meta.get("stream") or {}
    try:
        samples = int(stats["samples"])
        dropped = int(stats.get("dropped", "0"))
    except (KeyError, ValueError):
        return False
    if stream.get("lost") or stream.get("error"):
        return False
    if str(settings.get("capture", "")).upper().startswith("STAT"):
        return stream.get("samples") == 0
    if meta.get("profile") in RING_PROFILES:
        trace = meta.get("trace") or {}
        return trace.get("file") == "trace.raw" and trace.get("count") == trace.get("points")
    return stream.get("samples") == samples - dropped


class Run:
    """A run directory read back: its records, its exports, a decimation.

    :param path: The run's directory.
    :param recording: Whether its files are still being written (the
        recorder's capture): its exports are refused until it ends.
    """

    def __init__(self, path: Path, *, recording: bool = False) -> None:
        self.path = path
        self.id = path.name
        self.recording = recording
        self._dump: Dump | None = None

    @property
    def meta(self) -> dict[str, Any]:
        """Its ``meta.json``.

        :raises CaptureRefused: With 409 when it cannot be read.
        """
        try:
            value = json.loads((self.path / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            said = shown_error(error) if isinstance(error, OSError) else error
            raise CaptureRefused(409, "unreadable", f"{self.id}: meta.json: {said}") from None
        if not isinstance(value, dict):
            raise CaptureRefused(409, "unreadable", f"{self.id}: meta.json is not an object")
        return value

    @staticmethod
    def summary_of(meta: dict[str, Any]) -> dict[str, Any]:
        """A run's line in a list, from its ``meta.json``."""
        stats = (meta.get("statistics") or {}).get("fields", {})
        stream = meta.get("stream") or {}
        host = meta.get("host") or {}
        return {
            "id": meta.get("id"),
            "state": meta.get("state"),
            "complete": meta.get("complete"),
            "profile": meta.get("profile"),
            "format": meta.get("format"),
            "label": meta.get("label"),
            "started": host.get("started"),
            "ended": host.get("ended"),
            "ended_by": meta.get("ended_by"),
            "samples": stats.get("samples"),
            "dropped": stats.get("dropped"),
            "stream_samples": stream.get("samples"),
            "marks": stream.get("marks"),
            "trace": (meta.get("trace") or {}).get("file"),
            "noise_reduction": meta.get("noise_reduction"),
        }

    def summary(self) -> dict[str, Any]:
        """This run's line in a list, with whether it still records; an unreadable one says so."""
        try:
            return {**self.summary_of(self.meta), "recording": self.recording}
        except CaptureRefused as refused:
            return {"id": self.id, "state": "unreadable", "detail": refused.detail}

    # -- the records ----------------------------------------------------------------------

    def dump(self) -> Dump | None:
        """The ring dump, decoded with the capture's header; None for a streamed run."""
        if self._dump is None:
            trace = self.path / "trace.raw"
            if not trace.is_file():
                return None
            header = ((self.meta.get("trace") or {}).get("dump_header") or {}).get("unit")
            self._dump = decode_dump(trace.read_bytes(), header)
        return self._dump

    def records(self) -> Iterator[Row | Mark]:
        """The run's samples as rows, one per timestamp, and its marks, in order.

        A row's values are each channel's thousandths, keyed by the
        instrument's channel names (``i_ua``, ...). A ring run's rows come
        from ``trace.raw``, timed by the drift anchors (:meth:`Dump.t_us`),
        each mark before the sample it marks; a streamed run's from
        ``stream.txt``, where the ``TEXT`` format's lines of one timestamp
        become one row.
        """
        dump = self.dump()
        if dump is not None:
            yield from _dump_records(dump)
            return
        row: Row | None = None
        with (self.path / "stream.txt").open("r", encoding="utf-8") as handle:
            for line in handle:
                record = parse_record(line.rstrip("\n"))
                if record is None:
                    continue
                if isinstance(record, Mark):
                    if row is not None:
                        yield row
                        row = None
                    yield record
                    continue
                values = {name: round(value * 1000) for name, value in record.values.items()}
                if row is not None and row[0] == record.t_us:
                    row[1].update(values)
                    continue
                if row is not None:
                    yield row
                row = (record.t_us, values)
        if row is not None:
            yield row

    # -- the exports --------------------------------------------------------------------

    def export(self, kind: str) -> tuple[Path, str, str]:
        """The export of *kind*, made once from the run's files and kept beside them.

        A streamed run's own format is ``stream.txt`` itself, byte for byte.
        A run still recording has no export: one made from its partial files
        would be kept and served for good. Each request that makes the file
        writes its own temporary one and replaces the kept one at once.

        :param kind: ``text``, ``csv`` or ``json``.
        :returns: The file, its content type and the name it is downloaded as.
        :raises CaptureRefused: With 400 for another kind, 409 while the run records.
        """
        if kind not in EXPORTS:
            raise CaptureRefused(400, "bad_format", f"an export is one of {', '.join(EXPORTS)}")
        if self.recording:
            raise CaptureRefused(
                409, "recording", f"{self.id} is still recording: export it once it has ended"
            )
        extension, content_type = EXPORTS[kind]
        meta = self.meta
        name = f"{self.id}.{extension}"
        stream_format = meta.get("format", "")
        if self.dump() is None and stream_format == kind.upper():
            return self.path / "stream.txt", content_type, name
        target = self.path / f"export.{extension}"
        if not target.is_file():
            descriptor, name_tmp = tempfile.mkstemp(
                prefix=f"{target.name}.", suffix=".tmp", dir=self.path
            )
            temporary = Path(name_tmp)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    if kind == "json":
                        self._write_json_export(handle, meta)
                    else:
                        writer = _text_lines if kind == "text" else _csv_lines
                        for record in self.records():
                            handle.writelines(writer(record))
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return target, content_type, name

    def _write_json_export(self, handle: IO[str], meta: dict[str, Any]) -> None:
        """``kleidos.meter.export/1``: the run's ``meta.json``, its columns and its marks.

        Written a column at a time, so a run of a million samples is never
        held as Python objects.
        """
        channels: list[str] = []
        marks: list[dict[str, Any]] = []
        for record in self.records():
            if isinstance(record, Mark):
                marks.append({"label": record.label, "n": record.index, "t_us": record.t_us})
            else:
                channels += [name for name in record[1] if name not in channels]
        present = [name for name in CSV_CHANNELS if name in channels]
        units = {"t_us": "us", "i_na": "nA", "v_uv": "uV", "p_uw": "uW", "die_mc": "mdegC"}
        head = {
            "schema": EXPORT_SCHEMA,
            "run": self.id,
            "units": {key: units[key] for key in ["t_us", *(_INTEGER_OF[c] for c in present)]},
            "meta": meta,
            "marks": marks,
        }
        text = json.dumps(head, ensure_ascii=False)
        handle.write(text[:-1] + ',"columns":{')
        columns = [("t_us", None), *((_INTEGER_OF[c], c) for c in present)]
        for place, (key, channel) in enumerate(columns):
            handle.write(("," if place else "") + json.dumps(key) + ":[")
            chunk: list[str] = []
            first = True
            for record in self.records():
                if isinstance(record, Mark):
                    continue
                value = record[0] if channel is None else record[1].get(channel)
                chunk.append("null" if value is None else str(value))
                if len(chunk) >= 4096:
                    handle.write(("" if first else ",") + ",".join(chunk))
                    first, chunk = False, []
            if chunk:
                handle.write(("" if first else ",") + ",".join(chunk))
            handle.write("]")
        handle.write("}}\n")

    # -- the decimation -----------------------------------------------------------------

    def decimate(
        self,
        column: str,
        buckets: int = 1000,
        t0_us: int | None = None,
        t1_us: int | None = None,
    ) -> dict[str, Any]:
        """The run's *column* between two times, as each bucket's figures.

        Buckets of equal duration cover ``[t0_us, t1_us]``; each that holds
        a sample answers its start, its lowest and highest value, how many
        samples it holds, and their arithmetic mean and root mean square, so a
        chart keeps every spike at any zoom and weighs each bucket by its
        samples when it averages them.

        :param column: ``i_na``, ``v_uv``, ``p_uw`` or ``die_mc``.
        :param buckets: How many, 1 to :data:`MAX_BUCKETS`.
        :param t0_us: The first timestamp, the run's first by default.
        :param t1_us: The last, the run's last by default.
        :returns: ``column``, ``t0_us``, ``t1_us``, ``bucket_us`` and the
            columns ``t_us``, ``min``, ``max``, ``n``, ``mean`` and ``rms`` of
            the buckets that hold samples.
        :raises CaptureRefused: With 400 for an unknown column, a bucket count
            out of range or an empty window.
        """
        channel = _CHANNEL_OF.get(column)
        if channel is None:
            raise CaptureRefused(400, "bad_column", f"a column is one of {', '.join(_CHANNEL_OF)}")
        if not 1 <= buckets <= MAX_BUCKETS:
            raise CaptureRefused(400, "bad_buckets", f"buckets is 1 to {MAX_BUCKETS}")
        if t0_us is None or t1_us is None:
            first, last = self._span()
            t0_us = first if t0_us is None else t0_us
            t1_us = last if t1_us is None else t1_us
        body: dict[str, Any] = {
            "column": column,
            "t0_us": t0_us,
            "t1_us": t1_us,
            "bucket_us": None,
            "t_us": [],
            "min": [],
            "max": [],
            "n": [],
            "mean": [],
            "rms": [],
        }
        if t0_us is None or t1_us is None:
            return body  # no sample at all
        if t1_us < t0_us:
            raise CaptureRefused(400, "bad_window", "t1_us is before t0_us")
        width = max(1, -(-(t1_us - t0_us + 1) // buckets))
        body["bucket_us"] = width
        low: dict[int, int] = {}
        high: dict[int, int] = {}
        count: dict[int, int] = {}
        total: dict[int, int] = {}
        squares: dict[int, int] = {}
        for record in self.records():
            if isinstance(record, Mark):
                continue
            t, values = record
            value = values.get(channel)
            if value is None or not t0_us <= t <= t1_us:
                continue
            slot = (t - t0_us) // width
            if slot in count:
                low[slot] = min(low[slot], value)
                high[slot] = max(high[slot], value)
                count[slot] += 1
                total[slot] += value
                squares[slot] += value * value
            else:
                low[slot] = high[slot] = value
                count[slot] = 1
                total[slot] = value
                squares[slot] = value * value
        for slot in sorted(count):
            body["t_us"].append(t0_us + slot * width)
            body["min"].append(low[slot])
            body["max"].append(high[slot])
            body["n"].append(count[slot])
            body["mean"].append(total[slot] / count[slot])  # exact integers, one rounding
            body["rms"].append(math.sqrt(squares[slot] / count[slot]))
        return body

    def _span(self) -> tuple[int | None, int | None]:
        """The run's first and last sample timestamps, from its ``meta.json``."""
        meta = self.meta
        block = (meta.get("trace") if self.dump() is not None else meta.get("stream")) or {}
        return block.get("first_t_us"), block.get("last_t_us")


def _dump_records(dump: Dump) -> Iterator[Row | Mark]:
    """A ring dump's samples as rows and its marks, each mark before the sample it marks."""
    marks = list(dump.marks)
    place = 0
    for sample in dump.samples():
        while place < len(marks) and marks[place].index <= sample.index:
            yield marks[place]
            place += 1
        values = {} if sample.nanoamps is None else {"i_ua": sample.nanoamps}
        yield (sample.t_us, values)
    yield from marks[place:]


def _text_lines(record: Row | Mark) -> list[str]:
    """A record in the ``TEXT`` format: a line per channel, or the mark's line."""
    if isinstance(record, Mark):
        return [f"METER_MARK mark={record.label} n={record.index} t={record.t_us}\n"]
    t, values = record
    return [
        f"METER t={t} ch={name} v={format_thousandths(values[name])}\n"
        for name in CSV_CHANNELS
        if name in values
    ]


def _csv_lines(record: Row | Mark) -> list[str]:
    """A record in the ``CSV`` format: an ``S`` row (a channel it lacks empty), or a ``K`` row."""
    if isinstance(record, Mark):
        return [f"K,{record.index},{record.t_us},{record.label}\n"]
    t, values = record
    cells = [format_thousandths(values[name]) if name in values else "" for name in CSV_CHANNELS]
    return [f"S,{t}," + ",".join(cells) + "\n"]
