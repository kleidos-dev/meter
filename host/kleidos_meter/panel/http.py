# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The panel's HTTP API: JSON over the standard library's threading server.

:class:`PanelServer` listens on 127.0.0.1 port 8040 by default and answers
the routes of :data:`~kleidos_meter.panel.schema.ROUTES`, every one through
:class:`Api`, which knows nothing of sockets: it takes a method, a target,
the headers and a body, and gives back a status, headers and a JSON body. A
request is served on a thread of its own, and whatever it sends goes through
the session's executor, so it waits its turn behind the message in flight
and never writes to the port itself.

Before any route runs, the request passes the guards of
:mod:`~kleidos_meter.panel.guards` (the loopback ``Host``, the header every
change carries, a foreign ``Origin`` refused) and, for a route that talks to
the instrument, the hold: while another controller holds the line
(:meth:`Api.hold`) such a route answers ``423`` and names it; while a
measurement sequence runs (:mod:`~kleidos_meter.panel.sequences`) every
change answers ``409`` but ``POST /api/abort`` and the sequence's own answer
and abort, and while a pin map is applied and checked
(:mod:`~kleidos_meter.panel.pins`) every change but ``POST /api/abort`` does.
At most
:data:`MAX_REQUESTS` requests run at once and the next is answered ``503``,
``POST /api/abort`` aside: an abort is always served, and while an
acquisition holds the console it goes ahead of the executor's queue.

What a response says (every body is a JSON object):

- A report (``key=value`` tokens) becomes an object of the same keys
  (:func:`report_json`): a value in canonical integer form becomes a JSON
  integer in the unit its key names (``i_na``, ``vbus_uv``), ``-`` becomes
  ``null``, anything else, and every key of
  :data:`~kleidos_meter.panel.schema.TEXT_KEYS`, stays the string the
  instrument wrote. An integer beyond 2^53 stays a string too, so a browser
  never rounds it.
- The instrument's errors are a list of ``{"code", "text"}``, oldest first.
  A route that changes or reads something answers ``400`` when one of them
  is a command error (``-1xx``) and ``409`` for an execution error
  (``-2xx``), a device error (``-3xx``) or a device code (``100`` and up),
  with the whole reply in the body. ``POST /api/scpi`` is a transport and
  answers ``200`` with each message's errors beside its answer.
- A message the instrument would discard is refused unsent (``400``); a
  full queue, a line that is down or an executor that is closing is ``503``
  (with ``Retry-After``), an answer that did not come in time ``504``.

The page itself (:mod:`~kleidos_meter.panel.static`) is served beside the
API: ``GET /`` is its document and ``GET /static/<name>`` each of its
files, read once when serve starts, with the page's own
Content-Security-Policy on the document; any other method on those paths is
405, a name the page does not ship 404, and the ``Host`` guard applies as
it does to the API. They send nothing to the instrument, so they are served
while another controller holds the line.

Three routes answer something other than one JSON object
(:class:`StreamResponse`): ``GET /api/events`` streams Server-Sent Events
(:mod:`~kleidos_meter.panel.live`) until the page goes away or serve stops,
outside the request slots and up to the hub's own stream limit, and a run's
export is a download of its file. A stream's socket sends at most
:data:`STREAM_SNDBUF` bytes ahead of its reader and gives up on one that has
read nothing for :data:`STREAM_WRITE_TIMEOUT` seconds; until then a slow
reader is told what it lost instead of holding anything up.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import sys
import threading
from collections.abc import Callable, Generator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, overload
from urllib.parse import parse_qs, urlsplit

from ..client import MeterError, MeterTimeout
from ..invariant import present
from ..protocol import split_units
from .executor import (
    ExecutorBusy,
    ExecutorClosed,
    LineHeld,
    LinkDown,
    MessageRefused,
    Reply,
    analyze,
    check_message,
)
from .guards import Refusal, check_host, check_mutation
from .live import HubClosed, LiveHub, TooManyStreams
from .local_elf import LocalElf
from .pins import CHANGING, PinChanges
from .recorder import CaptureRefused, Recorder, Run
from .reports import errors_json, report_json, status_for, typed_fields
from .schema import (
    HEALTH,
    PIN_ORDERS,
    RELAY_READBACK,
    SCHEMA,
    Relay,
    Route,
    Schema,
    get_refusal,
    resolve_unit,
    short_form,
)
from .sequences import SequenceRefused, SequenceRunner
from .settings import Calibration, SettingsRefused, read_settings, write_settings
from .shaping import changes_averaging
from .static import Page

if TYPE_CHECKING:  # serve and the passthrough import this module: their types are the checker's
    from .passthrough import Passthrough
    from .serve import Session

__all__ = [
    "API_POLICY",
    "MAX_BODY",
    "MAX_REQUESTS",
    "STREAM_SNDBUF",
    "STREAM_WRITE_TIMEOUT",
    "Api",
    "ApiResponse",
    "PanelServer",
    "StreamResponse",
    "errors_json",
    "report_json",
    "status_for",
    "typed_fields",
]

_LOG = logging.getLogger(__name__)

#: The largest request body taken, bytes.
MAX_BODY = 16 * 1024

#: How many requests run at once; the next is answered 503 (an abort is always served).
MAX_REQUESTS = 32

#: Seconds an idle connection is kept open for its next request.
IDLE_TIMEOUT = 10.0

#: The ``ABORt`` route, served whatever else is running.
_ABORT = ("POST", "/api/abort")

#: The changes served while a sequence runs: the ones that end it or answer it.
_DURING_SEQUENCE = frozenset(
    {_ABORT, ("POST", "/api/sequences/answer"), ("POST", "/api/sequences/abort")}
)

#: The numbers an override's preview takes in its query string.
_OVERRIDE_NUMBERS = (
    "ohms",
    "uohm",
    "expanded_ppm",
    "expanded_uohm",
    "tolerance_ppm",
    "tolerance_uohm",
)

#: The bytes a stream's socket holds for a reader that has not taken them.
STREAM_SNDBUF = 64 * 1024

#: Seconds a stream or a download waits for a reader that takes nothing.
STREAM_WRITE_TIMEOUT = 30.0

#: Seconds of silence after which an event stream sends a comment line.
KEEPALIVE = 15.0

#: The bytes a download is read from its file at a time.
_FILE_CHUNK = 256 * 1024

#: The Content-Security-Policy of every answer but the page's document: nothing loads.
API_POLICY = "default-src 'none'; frame-ancestors 'none'"


@dataclass
class ApiResponse:
    """What a route answers.

    :ivar status: The HTTP status.
    :ivar body: The JSON body.
    :ivar headers: Headers beyond the ones every response carries.
    """

    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)

    def encode(self) -> bytes:
        """The body as UTF-8 JSON, one line and a newline."""
        return (json.dumps(self.body, ensure_ascii=False) + "\n").encode("utf-8")


@dataclass
class StreamResponse:
    """A route's answer that is not one JSON object: an event stream, or a download.

    :ivar status: The HTTP status.
    :ivar content_type: Its ``Content-Type``.
    :ivar chunks: Its body, a piece at a time: a generator, closed once written or abandoned.
    :ivar length: Its ``Content-Length``, None for a body that ends with the connection.
    :ivar headers: Headers beyond the ones every response carries.
    :ivar stream: Whether it is an event stream (a small send buffer, its own time-out).
    :ivar policy: Its Content-Security-Policy, :data:`API_POLICY` unless it is the page's.
    """

    status: int
    content_type: str
    chunks: Generator[bytes, None, None]
    length: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    stream: bool = False
    policy: str = API_POLICY


def _bytes_chunks(data: bytes) -> Generator[bytes, None, None]:
    """*data* as one chunk."""
    yield data


def _file_chunks(path: Path) -> Generator[bytes, None, None]:
    """*path*'s bytes, a chunk at a time."""
    with path.open("rb") as handle:
        while chunk := handle.read(_FILE_CHUNK):
            yield chunk


class _BadRequest(Exception):
    """A request a route refuses before it sends anything: 400 and why."""

    def __init__(self, error: str, detail: str, **extra: Any) -> None:
        super().__init__(detail)
        self.error = error
        self.detail = detail
        self.extra = extra


def _refusal(refusal: Refusal) -> ApiResponse:
    """A guard's refusal as a response."""
    return ApiResponse(refusal.status, {"error": refusal.error, "detail": refusal.detail})


def _reply_json(reply: Reply) -> dict[str, Any]:
    """A message's reply: what was sent, its answers and its errors."""
    return {
        "message": reply.message,
        "units": reply.units,
        "errors": errors_json(reply.errors),
        "elapsed_s": round(reply.elapsed, 6),
    }


def _failure_kind(error: MeterError) -> str:
    """The word a route's answer gives *error* (``Api.handle``'s mapping)."""
    kinds: tuple[tuple[type[MeterError], str], ...] = (
        (ExecutorBusy, "busy"),
        (LinkDown, "link_down"),
        (ExecutorClosed, "closing"),
        (LineHeld, "held"),
        (MeterTimeout, "timeout"),
    )
    return next((kind for cls, kind in kinds if isinstance(error, cls)), "instrument")


def _path_regex(path: str) -> re.Pattern[str]:
    """A route's path as a regular expression: ``{name}`` is one segment."""
    parts = re.split(r"\{(\w+)\}", path)
    pattern = "".join(
        re.escape(part) if index % 2 == 0 else f"(?P<{part}>[^/]+)"
        for index, part in enumerate(parts)
    )
    return re.compile(pattern)


class Api:
    """The routes, on a session's executor: see the module's description.

    :param session: Serve's session; its executor carries every message.
    :param schema: The declarations the routes follow.
    :param remote: Whether serve listens beyond the loopback (``--allow-remote``).
    :param live: The live events ``/api/events`` streams; without it the route answers 503.
    :param recorder: The recorder the capture and run routes use; without it they answer 503.
    :param calibration: The shunt's and the registry's side; without it the
        shunt is not shown and its routes answer 503.
    :param sequences: The sequence runner; without it its routes answer 503.
    :param passthrough: The SCPI passthrough, whose state ``/api/status``
        carries; serve sets it once the passthrough is built (it needs the API).
    :param page: The page served at ``/`` and below ``/static/``
        (:func:`~kleidos_meter.panel.static.packaged_page` in serve); without
        it those paths are the API's, and answer 404.
    :param local_elf: The local build ``GET /api/health`` holds the
        instrument's ``elf=`` to; without it the health says it has none.
    """

    def __init__(
        self,
        session: Session,
        schema: Schema = SCHEMA,
        *,
        remote: bool = False,
        live: LiveHub | None = None,
        recorder: Recorder | None = None,
        calibration: Calibration | None = None,
        sequences: SequenceRunner | None = None,
        passthrough: Passthrough | None = None,
        page: Page | None = None,
        local_elf: LocalElf | None = None,
    ) -> None:
        self.session = session
        #: The local build the instrument's ``elf=`` is compared with.
        self.local_elf = LocalElf(None) if local_elf is None else local_elf
        #: The page, None when this API serves none.
        self.page = page
        self.executor = session.executor
        self.schema = schema
        self.remote = remote
        self.live = live
        self.recorder = recorder
        self.calibration = calibration
        self.sequences = sequences
        self.passthrough = passthrough
        #: The pin map: staged, applied, checked.
        self.pins = PinChanges(
            session, recorder=recorder, sequences=sequences, holder=lambda: self.holder
        )
        self._lock = threading.Lock()
        self._holder: str | None = None
        self._routes = [(route, _path_regex(route.path)) for route in schema.routes]
        #: Each route's handler, by (method, path).
        self.handlers: dict[tuple[str, str], Callable[..., ApiResponse | StreamResponse]] = {
            ("GET", "/api/schema"): self._get_schema,
            ("GET", "/api/status"): self._get_status,
            ("GET", "/api/health"): self._get_health,
            ("GET", "/api/query"): self._get_query,
            ("POST", "/api/scpi"): self._post_scpi,
            ("GET", "/api/settings"): self._get_settings,
            ("POST", "/api/settings"): self._post_settings,
            ("GET", "/api/settings/shunt/preview"): self._get_shunt_preview,
            ("POST", "/api/settings/shunt"): self._post_shunt,
            ("GET", "/api/calibration"): self._get_calibration,
            ("POST", "/api/calibration/restore"): self._post_calibration_restore,
            ("GET", "/api/sequences"): self._get_sequences,
            ("POST", "/api/sequences/run"): self._post_sequence_run,
            ("POST", "/api/sequences/answer"): self._post_sequence_answer,
            ("POST", "/api/sequences/abort"): self._post_sequence_abort,
            ("GET", "/api/relays"): self._get_relays,
            ("POST", "/api/relays/{relay}"): self._post_relay,
            ("GET", "/api/pins"): self._get_pins,
            ("POST", "/api/pins/stage"): self._post_pins_stage,
            ("POST", "/api/pins/apply"): self._post_pins_apply,
            ("POST", "/api/pins/defaults"): self._post_pins_defaults,
            ("POST", "/api/abort"): self._post_abort,
            ("GET", "/api/events"): self._get_events,
            ("GET", "/api/capture"): self._get_capture,
            ("POST", "/api/capture/start"): self._post_capture_start,
            ("POST", "/api/capture/stop"): self._post_capture_stop,
            ("POST", "/api/capture/mark"): self._post_capture_mark,
            ("GET", "/api/runs"): self._get_runs,
            ("GET", "/api/runs/{run}"): self._get_run,
            ("GET", "/api/runs/{run}/export"): self._get_export,
            ("GET", "/api/runs/{run}/decimated"): self._get_decimated,
        }

    # -- the hold ----------------------------------------------------------------

    def hold(self, holder: str) -> None:
        """Hand the line to *holder*: every route that talks to the instrument answers 423.

        :param holder: Who holds it, as the refusal names it.
        """
        with self._lock:
            self._holder = holder

    def release(self) -> None:
        """Take the line back from its holder."""
        with self._lock:
            self._holder = None

    @property
    def holder(self) -> str | None:
        """Who holds the line, None while the panel does."""
        with self._lock:
            return self._holder

    # -- dispatch ----------------------------------------------------------------

    def route_of(self, method: str, path: str) -> tuple[Route | None, dict[str, str], list[str]]:
        """The route *method* and *path* name, its path parameters, and the methods the path takes.

        :returns: The route (None when the method or the path matches none),
            the ``{name}`` segments, and every method a route of the path takes.
        """
        allowed: list[str] = []
        for route, regex in self._routes:
            match = regex.fullmatch(path)
            if match is None:
                continue
            allowed.append(route.method)
            if route.method == method:
                return route, match.groupdict(), allowed
        return None, {}, allowed

    def check(self, method: str, path: str, headers: Mapping[str, str]) -> ApiResponse | None:
        """The guards and the hold, before a route runs: a refusal, or None.

        :param method: The request's method.
        :param path: The target's path.
        :param headers: Its headers.
        """
        refused = check_host(_header(headers, "host"), remote=self.remote)
        if refused is not None:
            return _refusal(refused)
        if self.page is not None and self.page.owns(path):
            if method == "GET":
                return None
            return ApiResponse(
                405,
                {"error": "method_not_allowed", "detail": f"{path} takes GET"},
                {"Allow": "GET"},
            )
        route, _, allowed = self.route_of(method, path)
        if route is None and not allowed:
            return ApiResponse(404, {"error": "not_found", "detail": f"no route {path}"})
        if route is None:
            detail = (
                "a GET never changes the instrument"
                if method == "GET"
                else f"{path} takes {', '.join(allowed)}"
            )
            return ApiResponse(
                405,
                {"error": "method_not_allowed", "detail": detail},
                {"Allow": ", ".join(allowed)},
            )
        if route.mutates:
            refused = check_mutation(headers, remote=self.remote)
            if refused is not None:
                return _refusal(refused)
        holder = self.holder
        if route.sends and holder is not None:
            return ApiResponse(
                423,
                {"error": "held", "holder": holder, "detail": f"the line is held by {holder}"},
            )
        running = self.sequences is not None and self.sequences.running
        if running and route.mutates and (route.method, route.path) not in _DURING_SEQUENCE:
            return ApiResponse(
                409,
                {
                    "error": "sequence_running",
                    "detail": "a sequence is running: abort it, or wait for its end",
                },
            )
        if self.pins.running and route.mutates and (route.method, route.path) != _ABORT:
            return ApiResponse(409, {"error": "pins_changing", "detail": CHANGING})
        return None

    def handle(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes = b""
    ) -> ApiResponse | StreamResponse:
        """Answer one request.

        :param method: ``GET``, ``POST``, or another method (always refused).
        :param target: The request target, its query string included.
        :param headers: The request's headers.
        :param body: Its body.
        """
        split = urlsplit(target)
        refused = self.check(method, split.path, headers)
        if refused is not None:
            return refused
        if self.page is not None and self.page.owns(split.path):
            return self._get_static(split.path)
        route, params, _ = self.route_of(method, split.path)
        # check() refused every other request
        route = present(route, "the request's route")
        handler = self.handlers[(route.method, route.path)]
        try:
            return handler(query=parse_qs(split.query), body=body, headers=headers, **params)
        except (SettingsRefused, SequenceRefused) as refused_route:
            refusal = (
                refused_route.body()
                if isinstance(refused_route, SettingsRefused)
                else {"error": refused_route.error, "detail": refused_route.detail}
            )
            return ApiResponse(refused_route.status, refusal)
        except CaptureRefused as refused_capture:
            return ApiResponse(
                refused_capture.status,
                {
                    "error": refused_capture.error,
                    "detail": refused_capture.detail,
                    "errors": errors_json(refused_capture.errors),
                },
            )
        except _BadRequest as bad:
            return ApiResponse(400, {"error": bad.error, "detail": bad.detail, **bad.extra})
        except MessageRefused as refused_unsent:
            return ApiResponse(400, {"error": "refused_unsent", "detail": str(refused_unsent)})
        except ExecutorBusy as busy:
            return ApiResponse(503, {"error": "busy", "detail": str(busy)}, {"Retry-After": "1"})
        except LinkDown as down:
            return ApiResponse(
                503, {"error": "link_down", "detail": str(down)}, {"Retry-After": "2"}
            )
        except ExecutorClosed as closed:
            return ApiResponse(503, {"error": "closing", "detail": str(closed)})
        except LineHeld as held:  # handed over between the hold's check and the send
            holder = self.holder or "a passthrough client"
            return ApiResponse(
                423, {"error": "held", "holder": holder, "detail": f"{held}: {holder}"}
            )
        except MeterTimeout as late:
            return ApiResponse(504, {"error": "timeout", "detail": str(late)})
        except MeterError as error:
            body_json = {
                "error": "instrument",
                "detail": str(error),
                "errors": errors_json(error.errors),
            }
            return ApiResponse(status_for(error.codes) if error.errors else 502, body_json)

    # -- the page ------------------------------------------------------------------

    def _get_static(self, path: str) -> ApiResponse | StreamResponse:
        """``GET /`` and ``GET /static/<name>``: a file of the page, or 404."""
        # handle() asks only when there is a page
        asset = present(self.page, "the page").lookup(path)
        if asset is None:
            return ApiResponse(404, {"error": "not_found", "detail": f"the page has no {path}"})
        return StreamResponse(
            200,
            asset.content_type,
            _bytes_chunks(asset.data),
            len(asset.data),
            {"Referrer-Policy": "no-referrer"},
            policy=asset.policy or API_POLICY,
        )

    # -- the routes ----------------------------------------------------------------

    def _get_schema(self, **_: Any) -> ApiResponse:
        """``GET /api/schema``."""
        return ApiResponse(200, self.schema.to_json())

    def _get_status(self, **_: Any) -> ApiResponse:
        """``GET /api/status``: what the executor and the polls know; sends nothing."""
        executor, session = self.executor, self.session
        polled = executor.status
        in_flight = executor.in_flight
        return ApiResponse(
            200,
            {
                "link": {"up": executor.link_up, "down": executor.link_down},
                "polling": executor.polling,
                "in_flight": None if in_flight is None else in_flight.message,
                "queued": executor.queued,
                "held": self.holder,
                "passthrough": None if self.passthrough is None else self.passthrough.state_json(),
                "status": None if polled is None else typed_fields(polled.status),
                "health": None if polled is None else typed_fields(polled.health),
                "instrument": {"idn": session.idn, **typed_fields(session.info)},
                "foreign_window": None
                if session.foreign_window is None
                else typed_fields(session.foreign_window),
                "reboots": session.reboots,
                "capture": None if self.recorder is None else self.recorder.state_json(),
                "live": None
                if self.live is None
                else {"streams": self.live.streams, "last_id": self.live.last_id},
            },
        )

    def _get_health(self, **_: Any) -> ApiResponse:
        """``GET /api/health``: the reports of :data:`~kleidos_meter.panel.schema.HEALTH`.

        Beside them, ``local_elf``: the local build's ``elf=`` against the
        instrument's (:meth:`~kleidos_meter.panel.local_elf.LocalElf.compare`).
        """
        answers = self.executor.query_all([query for _, query in HEALTH])
        keys = [key for key, _ in HEALTH]
        body = {key: report_json(answer) for key, answer in zip(keys, answers, strict=True)}
        body["local_elf"] = self.local_elf.compare(body["info"].get("elf"))
        return ApiResponse(200, body)

    def _get_query(self, query: dict[str, list[str]], **_: Any) -> ApiResponse:
        """``GET /api/query?q=``: one message of queries that read without acting."""
        texts = query.get("q", [])
        if len(texts) != 1:
            raise _BadRequest("bad_query", "name one message with ?q=")
        message = texts[0]
        check_message(message)
        path = ""
        for unit in (unit for unit in split_units(message) if unit.strip()):
            resolved, path = resolve_unit(unit, path)
            reason = get_refusal(resolved)
            if reason is not None:
                raise _BadRequest("get_mutates", reason, unit=unit.strip())
        reply = self.executor.call(message)
        body = _reply_json(reply)
        body["fields"] = [report_json(unit) for unit in reply.units]
        return ApiResponse(status_for(reply.codes), body)

    def _post_scpi(self, body: bytes, **_: Any) -> ApiResponse:
        """``POST /api/scpi``: program messages, one per line, each with its errors."""
        try:
            text = body.decode("ascii")
        except UnicodeDecodeError as error:
            raise _BadRequest("bad_body", f"the body is not ASCII (byte {error.start})") from None
        # Only a newline ends a message (and the CR before it): any other control
        # byte stays inside, where check_message refuses it, as the instrument would.
        lines = (line.removesuffix("\r") for line in text.split("\n"))
        messages = [line for line in lines if line.strip()]
        if not messages:
            raise _BadRequest("bad_body", "no program message: one per line")
        for message in messages:
            check_message(message)
            if "TRACe:DATA?" in analyze(message).patterns:
                raise _BadRequest(
                    "dump_refused",
                    "a ring dump is too large for a reply: the recorder fetches it to a file",
                    message=message,
                )
        replies: list[dict[str, Any]] = []
        for index, message in enumerate(messages):
            try:
                replies.append(_reply_json(self._call(message)))
            except MeterError as error:
                if index == 0:
                    raise  # nothing ran: answered as any route's failure, a retry harmless
                # The lines before it changed the instrument: say what ran, what failed
                # and what was never sent, and ask for no retry that would run them twice.
                failed = {"message": message, "error": _failure_kind(error), "detail": str(error)}
                return ApiResponse(
                    200, {"replies": replies, "failed": failed, "unsent": messages[index + 1 :]}
                )
        return ApiResponse(200, {"replies": replies})

    def _call(self, message: str) -> Reply:
        """Send a person's *message*; the shaping guard told what it needs to hand back.

        :raises MeterError: As the executor raises.
        """
        info, shaping = analyze(message), self.session.shaping
        if changes_averaging(info):
            shaping.learn()
        reply = self.executor.call(message)
        if not reply.errors and PIN_ORDERS.intersection(p for p in info.patterns if p):
            shaping.adopt_bus_clock()
        return reply

    def _get_settings(self, **_: Any) -> ApiResponse:
        """``GET /api/settings``: every setting as its query answers it, and the shunt."""
        body: dict[str, Any] = {"settings": read_settings(self.executor, self.schema)}
        body["shunt"] = None if self.calibration is None else self.calibration.shunt()
        return ApiResponse(200, body)

    def _post_settings(self, body: bytes, **_: Any) -> ApiResponse:
        """``POST /api/settings``: write the settings named, then read them back."""
        status, answer = write_settings(
            self.executor, _json_object(body), self.schema, self.session.shaping.learn
        )
        return ApiResponse(status, answer)

    def _need_calibration(self) -> Calibration:
        """The shunt's side, or a 503 when serve runs without one."""
        if self.calibration is None:
            raise SettingsRefused(503, "unavailable", "this serve shows no shunt")
        return self.calibration

    def _get_shunt_preview(self, query: dict[str, list[str]], **_: Any) -> ApiResponse:
        """``GET /api/settings/shunt/preview``: what an override would write; sends only queries."""
        return ApiResponse(200, self._need_calibration().preview(_override_query(query)))

    def _post_shunt(self, body: bytes, **_: Any) -> ApiResponse:
        """``POST /api/settings/shunt``: write an override and keep it on the host."""
        return ApiResponse(200, self._need_calibration().write_override(_json_object(body)))

    def _get_calibration(self, **_: Any) -> ApiResponse:
        """``GET /api/calibration``: the registry's verdict on the instrument."""
        return ApiResponse(200, self._need_calibration().verify())

    def _post_calibration_restore(self, **_: Any) -> ApiResponse:
        """``POST /api/calibration/restore``: the registry's copy back, the overrides gone."""
        status, answer = self._need_calibration().restore()
        return ApiResponse(status, answer)

    def _need_sequences(self) -> SequenceRunner:
        """The sequence runner, or a 503 when serve runs without one."""
        if self.sequences is None:
            raise SequenceRefused(503, "unavailable", "this serve runs no sequence")
        return self.sequences

    def _get_sequences(self, **_: Any) -> ApiResponse:
        """``GET /api/sequences``: the sequence running, or the last one."""
        return ApiResponse(200, self._need_sequences().state_json())

    def _post_sequence_run(self, body: bytes, **_: Any) -> ApiResponse:
        """``POST /api/sequences/run``: start a sequence."""
        order = _json_object(body)
        unknown = sorted(set(order) - {"name", "params"})
        name, params = order.get("name"), order.get("params", {})
        if unknown or not isinstance(name, str) or not isinstance(params, dict):
            raise _BadRequest(
                "bad_body", "a sequence takes name and, if wanted, params (an object)"
            )
        return ApiResponse(200, self._need_sequences().start(name, params))

    def _post_sequence_answer(self, body: bytes, **_: Any) -> ApiResponse:
        """``POST /api/sequences/answer``: answer the running sequence's question."""
        order = _json_object(body)
        if set(order) != {"id", "answer"}:
            raise _BadRequest("bad_body", "an answer takes id and answer")
        return ApiResponse(200, self._need_sequences().answer(order["id"], order["answer"]))

    def _post_sequence_abort(self, **_: Any) -> ApiResponse:
        """``POST /api/sequences/abort``: abort the running sequence; its cleanup runs."""
        return ApiResponse(200, self._need_sequences().abort())

    def _get_relays(self, **_: Any) -> ApiResponse:
        """``GET /api/relays``: the circuit, the hold and the last order."""
        result, state = self.executor.query_all(list(RELAY_READBACK))
        return ApiResponse(200, {"result": report_json(result), "state": report_json(state)})

    def _post_relay(self, body: bytes, relay: str, **_: Any) -> ApiResponse:
        """``POST /api/relays/<relay>``: one order, with only the flags the caller set."""
        found = self.schema.relay(relay)
        if found is None:
            names = ", ".join(r.name for r in self.schema.relays)
            return ApiResponse(
                404, {"error": "unknown_relay", "detail": f"no relay {relay!r}: one of {names}"}
            )
        unit = relay_unit(found, _json_object(body))
        reply = self.executor.call(";:".join([unit, *RELAY_READBACK]))
        result, state = (reply.units + ["", ""])[:2]
        return ApiResponse(
            status_for(reply.codes),
            {
                "order": unit,
                "errors": errors_json(reply.errors),
                "result": report_json(result),
                "state": report_json(state),
            },
        )

    def _get_pins(self, **_: Any) -> ApiResponse:
        """``GET /api/pins``: the board, the live map, the staged one and the last result."""
        return ApiResponse(200, self.pins.state())

    def _post_pins_stage(self, body: bytes, **_: Any) -> ApiResponse:
        """``POST /api/pins/stage``: fields staged and judged."""
        status, answer = self.pins.stage(_json_object(body))
        return ApiResponse(status, answer)

    def _post_pins_apply(self, **_: Any) -> ApiResponse:
        """``POST /api/pins/apply``: the staged map made the live one."""
        status, answer = self.pins.apply()
        return ApiResponse(status, answer)

    def _post_pins_defaults(self, **_: Any) -> ApiResponse:
        """``POST /api/pins/defaults``: the rig's own map back."""
        status, answer = self.pins.apply(defaults=True)
        return ApiResponse(status, answer)

    def _post_abort(self, **_: Any) -> ApiResponse:
        """``POST /api/abort``: ``ABORt``, ahead of the queue under an acquisition.

        A sequence that runs is aborted first, so its cleanup puts back what it moved.
        """
        sequence = None
        if self.sequences is not None and self.sequences.running:
            sequence = self.sequences.abort()
        reply = self.executor.abort().result()
        if not reply.sent:
            return ApiResponse(
                200, {"sent": False, "detail": "the acquisition had ended", "sequence": sequence}
            )
        return ApiResponse(
            status_for(reply.codes), {"sent": True, **_reply_json(reply), "sequence": sequence}
        )

    # -- the live events, the captures and the runs ------------------------------------

    def _need_recorder(self) -> Recorder:
        """The recorder, or a 503 when serve runs without one."""
        if self.recorder is None:
            raise CaptureRefused(503, "unavailable", "this serve records nothing")
        return self.recorder

    def _get_events(self, headers: Mapping[str, str], **_: Any) -> ApiResponse | StreamResponse:
        """``GET /api/events``: the live events, resumed after ``Last-Event-ID`` when sent."""
        if self.live is None:
            return ApiResponse(503, {"error": "unavailable", "detail": "no live events"})
        try:
            stream = self.live.subscribe(_header(headers, "last-event-id"))
        except TooManyStreams as full:
            return ApiResponse(503, {"error": "busy", "detail": str(full)}, {"Retry-After": "5"})
        except HubClosed as closed:
            return ApiResponse(503, {"error": "closing", "detail": str(closed)})
        return StreamResponse(
            200,
            "text/event-stream; charset=utf-8",
            stream.frames(KEEPALIVE),
            headers={"X-Accel-Buffering": "no"},
            stream=True,
        )

    def _get_capture(self, **_: Any) -> ApiResponse:
        """``GET /api/capture``: the recorder's state."""
        return ApiResponse(200, self._need_recorder().state_json())

    def _post_capture_start(self, body: bytes, **_: Any) -> ApiResponse:
        """``POST /api/capture/start``: configure, start and record a capture."""
        recorder = self._need_recorder()
        order = _json_object(body)
        unknown = sorted(set(order) - {"profile", "channels", "seconds", "label"})
        if unknown or "profile" not in order:
            raise _BadRequest(
                "bad_body", "a capture takes profile and, if wanted, channels, seconds and label"
            )
        state = recorder.start(
            order["profile"], order.get("channels"), order.get("seconds"), order.get("label", "")
        )
        return ApiResponse(200, state)

    def _post_capture_stop(self, **_: Any) -> ApiResponse:
        """``POST /api/capture/stop``: close the capture and finish its run."""
        meta = self._need_recorder().stop()
        return ApiResponse(200, {"run": meta["id"], "summary": Run.summary_of(meta), "meta": meta})

    def _post_capture_mark(self, body: bytes, **_: Any) -> ApiResponse:
        """``POST /api/capture/mark``: ``TRACe:MARK`` in the running capture."""
        order = _json_object(body)
        answer = self._need_recorder().mark(order.get("label"))
        return ApiResponse(status_for(answer.pop("codes")), answer)

    def _get_runs(self, query: dict[str, list[str]], **_: Any) -> ApiResponse:
        """``GET /api/runs``: the runs, newest first."""
        limit = _int_parameter(query, "limit", 100, 1, 1000)
        return ApiResponse(200, {"runs": self._need_recorder().runs(limit)})

    def _get_run(self, run: str, **_: Any) -> ApiResponse:
        """``GET /api/runs/<run>``: its ``meta.json``."""
        return ApiResponse(200, self._need_recorder().run(run).meta)

    def _get_export(self, run: str, query: dict[str, list[str]], **_: Any) -> StreamResponse:
        """``GET /api/runs/<run>/export?format=``: a download of the run's records."""
        kinds = query.get("format", ["csv"])
        path, content_type, name = self._need_recorder().run(run).export(kinds[-1])
        return StreamResponse(
            200,
            content_type,
            _file_chunks(path),
            path.stat().st_size,
            {"Content-Disposition": f'attachment; filename="{name}"'},
        )

    def _get_decimated(self, run: str, query: dict[str, list[str]], **_: Any) -> ApiResponse:
        """``GET /api/runs/<run>/decimated``: each bucket's n, mean, RMS, min and max."""
        found = self._need_recorder().run(run)
        column = query.get("column", ["i_na"])[-1]
        buckets = _int_parameter(query, "buckets", 1000, None, None)
        t0 = _int_parameter(query, "t0_us", None, None, None)
        t1 = _int_parameter(query, "t1_us", None, None, None)
        return ApiResponse(200, found.decimate(column, buckets, t0, t1))


@overload
def _int_parameter(
    query: dict[str, list[str]], name: str, default: int, low: int | None, high: int | None
) -> int: ...


@overload
def _int_parameter(
    query: dict[str, list[str]], name: str, default: None, low: int | None, high: int | None
) -> int | None: ...


def _int_parameter(
    query: dict[str, list[str]],
    name: str,
    default: int | None,
    low: int | None,
    high: int | None,
) -> int | None:
    """An integer query parameter, *default* when absent.

    :raises _BadRequest: When it is not an integer, or is outside *low* to *high*.
    """
    texts = query.get(name)
    if not texts:
        return default
    text = texts[-1].strip()
    if not re.fullmatch(r"-?\d{1,19}", text):
        raise _BadRequest("bad_parameter", f"{name} is an integer")
    value = int(text)
    if (low is not None and value < low) or (high is not None and value > high):
        raise _BadRequest("bad_parameter", f"{name} is {low} to {high}")
    return value


def _override_query(query: dict[str, list[str]]) -> dict[str, Any]:
    """An override's preview from its query string: ``range`` an integer, the figures numbers.

    :raises _BadRequest: For a figure that is not a number, or a key given twice.
    """
    order: dict[str, Any] = {}
    for key, texts in query.items():
        if len(texts) != 1:
            raise _BadRequest("bad_parameter", f"give {key} once")
        text = texts[0].strip()
        if key == "range":
            order[key] = int(text) if re.fullmatch(r"\d", text) else text
        elif key in _OVERRIDE_NUMBERS:
            if not re.fullmatch(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?", text):
                raise _BadRequest("bad_parameter", f"{key} is a number")
            order[key] = float(text)
        else:
            order[key] = texts[0]
    return order


def relay_unit(relay: Relay, order: Mapping[str, Any]) -> str:
    """The unit an order writes: its state or duration, and only the flags set true.

    :param relay: The relay order.
    :param order: ``state`` (a word of its choices) or, for a duration,
        ``ms``; ``force`` and ``live`` as JSON booleans, false when absent.
    :raises _BadRequest: For a missing or malformed parameter, an unknown
        key, or a flag the order does not take.
    """
    flags = {"force": "FORCe", "live": "LIVE"}
    first = "state" if relay.choices else "ms"
    unknown = set(order) - {first, *flags}
    if unknown:
        raise _BadRequest("bad_order", f"{relay.name} takes {first}, force and live")
    if first not in order:
        raise _BadRequest("bad_order", f"{relay.name} needs {first}")
    try:
        value = relay.parameter(order[first])
    except ValueError as why:
        raise _BadRequest("bad_order", f"{first} takes {why}") from None
    chosen: set[str] = set()
    for key, option in flags.items():
        given = order.get(key, False)
        if not isinstance(given, bool):
            raise _BadRequest("bad_order", f"{key} is true or false")
        if given and option not in relay.options:
            raise _BadRequest("bad_order", f"{relay.name} takes no {option}")
        if given:
            chosen.add(option)
    options = "".join("," + short_form(option) for option in relay.options if option in chosen)
    return f"{relay.header} {value}{options}"


def _json_object(body: bytes) -> dict[str, Any]:
    """A request body that must be a JSON object (an empty body is an empty one).

    :raises _BadRequest: When it is not.
    """
    if not body.strip():
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _BadRequest("bad_body", "the body is not JSON") from None
    if not isinstance(value, dict):
        raise _BadRequest("bad_body", "the body is a JSON object")
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """A header by name in any case, None when absent."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


class _Handler(BaseHTTPRequestHandler):
    """One connection: each request read, checked, routed and answered."""

    server: _Server
    protocol_version = "HTTP/1.1"
    timeout = IDLE_TIMEOUT
    server_version = "kleidos-meter-serve"
    sys_version = ""  # the Python version stays out of the Server header

    def do_GET(self) -> None:
        """A GET request."""
        self._serve("GET")

    def do_POST(self) -> None:
        """A POST request."""
        self._serve("POST")

    def do_PUT(self) -> None:
        """A PUT request: no route takes one."""
        self._serve("PUT")

    def do_DELETE(self) -> None:
        """A DELETE request: no route takes one."""
        self._serve("DELETE")

    def do_PATCH(self) -> None:
        """A PATCH request: no route takes one."""
        self._serve("PATCH")

    def do_OPTIONS(self) -> None:
        """A CORS preflight, among others: answered 405 with no ``Access-Control-*`` header."""
        self._serve("OPTIONS")

    def do_HEAD(self) -> None:
        """A HEAD request: no route takes one."""
        self._serve("HEAD")

    def _serve(self, method: str) -> None:
        """Read the body, run the request through the API, and answer it."""
        api = self.server.api
        headers = dict(self.headers.items())
        length_text = self.headers.get("Content-Length", "0").strip()
        if self.headers.get("Transfer-Encoding") is not None:
            self.close_connection = True  # the body's end is unknown: nothing after it is read
            self._send(
                ApiResponse(411, {"error": "length_required", "detail": "send Content-Length"})
            )
            return
        if not length_text.isdigit() or int(length_text) > MAX_BODY:
            self.close_connection = True
            self._send(
                ApiResponse(413, {"error": "too_large", "detail": f"at most {MAX_BODY} bytes"})
            )
            return
        length = int(length_text)
        body = self.rfile.read(length) if length else b""
        is_abort = (method, urlsplit(self.path).path) == _ABORT
        if not is_abort and not self.server.slots.acquire(blocking=False):
            self._send(
                ApiResponse(
                    503,
                    {"error": "busy", "detail": f"{MAX_REQUESTS} requests are running"},
                    {"Retry-After": "1"},
                )
            )
            return
        try:
            response = api.handle(method, self.path, headers, body)
        except Exception:  # noqa: BLE001  a route's own bug: answered, logged, the server kept
            _LOG.exception("%s %s", method, self.path)
            response = ApiResponse(500, {"error": "internal", "detail": "see serve's log"})
        finally:
            if not is_abort:
                self.server.slots.release()  # a stream holds no slot while it runs
        if isinstance(response, StreamResponse):
            self._send_stream(response)
        else:
            self._send(response)

    def _send_stream(self, response: StreamResponse) -> None:
        """Write an event stream or a download, a chunk at a time, until it ends or the reader goes.

        A reader that goes away, or takes nothing for :data:`STREAM_WRITE_TIMEOUT`
        seconds, ends the response and the connection; the chunks are closed
        either way, so an event stream frees its slot at once.
        """
        chunks = response.chunks
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", response.policy)
            if response.length is None:
                self.send_header("Connection", "close")
                self.close_connection = True
            else:
                self.send_header("Content-Length", str(response.length))
            for name, value in response.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.connection.settimeout(STREAM_WRITE_TIMEOUT)
            if response.stream:
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, STREAM_SNDBUF)
            for chunk in chunks:
                self.wfile.write(chunk)
        except OSError as error:  # the reader went away, or took nothing for too long
            _LOG.debug("%s %s ended: %s", self.command, self.path, error)
            self.close_connection = True
        finally:
            chunks.close()

    def _send(self, response: ApiResponse) -> None:
        """Write *response* with the headers every answer carries; never a CORS approval."""
        data = response.encode()
        self.send_response(response.status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", API_POLICY)
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        """Requests go to the module's logger, not to standard error."""
        _LOG.debug("%s " + format, self.address_string(), *args)


class _Server(ThreadingHTTPServer):
    """The threading server, carrying the API and the request slots."""

    daemon_threads = True
    api: Api
    slots: threading.BoundedSemaphore

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A connection's failure goes to the module's logger, never to standard error.

        A client that went away (a reset, a broken pipe, a silent socket) is
        routine for a server and is logged at debug level; anything else is a
        bug of the handler and is logged with its traceback.
        """
        error = sys.exc_info()[1]
        if isinstance(error, ConnectionError | TimeoutError):
            _LOG.debug("%s went away: %s", client_address, error)
        else:
            _LOG.exception("serving %s", client_address)


class _Server6(_Server):
    """The same on an IPv6 address."""

    address_family = socket.AF_INET6


class PanelServer:
    """The HTTP API listening: bound at construction, served from :meth:`start`.

    :param api: What answers.
    :param bind: The address to listen on.
    :param port: The port, 0 for any free one.
    :raises OSError: When the address cannot be bound.
    """

    def __init__(self, api: Api, bind: str, port: int) -> None:
        kind = _Server6 if ":" in bind else _Server
        self._server = kind((bind, port), _Handler)
        self._server.api = api
        self._server.slots = threading.BoundedSemaphore(MAX_REQUESTS)
        self._thread: threading.Thread | None = None
        self._closed = False
        self.api = api
        #: The port it listens on (the one chosen when 0 was asked).
        self.port: int = self._server.server_address[1]
        host = f"[{bind}]" if ":" in bind else bind
        #: The panel's address.
        self.url = f"http://{host}:{self.port}/"

    def start(self) -> None:
        """Serve requests on a thread of their own."""
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="kleidos-meter-http",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop answering and close the listening socket; a second call does nothing."""
        thread, self._thread = self._thread, None
        if thread is not None:
            self._server.shutdown()
            thread.join()
        if not self._closed:
            self._closed = True
            self._server.server_close()
