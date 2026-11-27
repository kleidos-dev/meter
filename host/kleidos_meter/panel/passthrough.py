# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The SCPI passthrough: another controller on the meter's line while serve holds the port.

``kleidos-meter serve --scpi-passthrough [PORT]`` listens on 127.0.0.1,
port 5025 by default, and gives the line to one TCP client at a time, byte
for byte in both directions: what the client writes reaches the instrument
as it came, and what the instrument writes reaches the client as it came, so
a QA run or a CLI tool opens ``socket://127.0.0.1:5025`` as it would open the
port (:class:`~kleidos_meter.client.SerialTransport` takes the URL) while
serve keeps the port and the panel. Serve adds and removes nothing: no
error query, no sync, no line of its own.

Attaching, in this order, and refused at the first step that fails (the
client's connection is then closed with nothing written, and serve says
why):

1. Nobody else holds the line: a second client is refused at once.
2. The panel is put on hold (:meth:`~kleidos_meter.panel.http.Api.hold`):
   every route that talks to the instrument answers ``423`` and names the
   client, and the status poll stops.
3. No measurement sequence runs, no pin map is being applied, and the message
   in flight and those queued end within :data:`ATTACH_WAIT` seconds.
4. No capture of serve's recorder runs, and no capture or null window serve
   opened is open (``SYSTem:STATus?`` asked afresh); a window serve found
   open when it started is no obstacle.
5. The capture-shaping settings serve changed are written back
   (:meth:`~kleidos_meter.panel.shaping.ShapingGuard.restore`), so the
   client finds them as serve found them.
6. The executor hands the line over (:meth:`~kleidos_meter.panel.executor.Executor.hand_over`).

While a client is attached the panel is a monitor: the executor's thread
still reads the port, gives every chunk to the client's queue first and then
routes its stream records to the listeners (the live view shows the client's
captures; responses are the client's alone). The thread never waits on the
client: the bytes wait in a queue of the client's own that a writer thread
empties, and a client that lets more than :data:`MAX_QUEUED` bytes pile up
(one that stopped reading while the instrument streams) is disconnected,
and how many bytes it lost is logged and published.

A client is SCPI, never a web page: any web page can make the browser send
``POST / HTTP/1.1`` and its headers to this port with no preflight, and each
line of its body would run as a program message (``ROUT:LEAD OPEN,FORC``).
So the client's bytes reach the instrument a whole line at a time, each line
looked at first, and the client is dropped at the first line of an HTTP
request (:func:`looks_like_http`: a request line, or a header every browser
sends), with nothing from that line on forwarded.

Detaching (the client closed, was dropped, or serve stops): the executor
takes the line back with a bare newline, which ends any message the client
left unfinished, and a sync whose answer discards every response before it;
then ``SYSTem:STATus?`` is read again, the hold is released and a status
poll follows at once (its ``SYSTem:HEALth?`` tells a reboot the client
caused, as any poll does). A capture or null window the client left
open is reported, and serve leaves it open on the way out, as it leaves one
it found. The settings the client changed are the client's: serve writes
back only what serve changed.

Each step publishes a ``status`` event of ``kind`` ``passthrough``
(:mod:`~kleidos_meter.panel.live`): ``state`` ``attached``, ``refused``
(with ``reason``) or ``detached`` (with ``dropped``, ``lost_bytes`` and the
``window`` left open, if any).
"""

from __future__ import annotations

import contextlib
import logging
import re
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..client import MeterError
from ..protocol import parse_fields
from .reports import typed_fields

if TYPE_CHECKING:  # serve imports this module: the types are for the checker only
    from .http import Api
    from .live import LiveHub
    from .recorder import Recorder
    from .sequences import SequenceRunner
    from .serve import Session

__all__ = [
    "ATTACH_WAIT",
    "BIND",
    "DEFAULT_PORT",
    "MAX_QUEUED",
    "Passthrough",
    "Refused",
    "looks_like_http",
]

_LOG = logging.getLogger(__name__)

#: The port ``--scpi-passthrough`` takes when it names none: SCPI's raw socket.
DEFAULT_PORT = 5025

#: Where the passthrough listens: the loopback only, whatever ``--bind`` says.
BIND = "127.0.0.1"

#: Seconds an attach waits for the message in flight and those queued.
ATTACH_WAIT = 5.0

#: The bytes a client may let pile up before it is disconnected.
MAX_QUEUED = 8 * 1024 * 1024

#: Seconds each query of an attach or a detach may take in all.
STATE_WAIT = 30.0

#: The most bytes the writer hands the socket at once.
_SEND_CHUNK = 1 << 20

#: The most bytes read from the client at once.
_RECV_CHUNK = 1 << 16

#: An HTTP request line: what a web page's cross-site request to the
#: passthrough's port starts with (``POST / HTTP/1.1``).
_HTTP_REQUEST = re.compile(rb"^[A-Z]+ \S+ HTTP/\d")

#: An HTTP header a browser always sends; no SCPI unit starts with one.
_HTTP_HEADER = re.compile(
    rb"^(?:host|origin|user-agent|referer|content-type|content-length|accept|connection"
    rb"|sec-fetch-[a-z-]+)[ \t]*:",
    re.IGNORECASE,
)

#: The most bytes a line may hold before it is judged without its end: a
#: program message is at most 255 bytes, and an HTTP line is judged by its start.
_MAX_HELD = 4096


def looks_like_http(line: bytes) -> bool:
    """Whether *line*, a line a client wrote, is a line of an HTTP request.

    :param line: The line, its end of line removed or not.
    :returns: True for a request line or a header a browser sends, which a web
        page's request to the port carries before the lines of its body.
    """
    return bool(_HTTP_REQUEST.match(line) or _HTTP_HEADER.match(line))


#: Seconds between the accept loop's looks at whether serve is stopping.
_ACCEPT_POLL = 0.1

#: Seconds between an attach's looks at whether the line is quiet.
_IDLE_POLL = 0.005


class Refused(Exception):
    """Why a client was not attached; the message is the reason serve gives."""


class _Client:
    """One attached client: its socket, the instrument's bytes queued for it, and its writer.

    :param conn: The client's connected socket.
    :param peer: Its address, ``host:port``.
    :param max_queued: The bytes it may let pile up before it is dropped.
    """

    def __init__(self, conn: socket.socket, peer: str, max_queued: int) -> None:
        self.conn = conn
        self.peer = peer
        self.max_queued = max_queued
        self._ready = threading.Condition(threading.Lock())
        self._chunks: deque[bytes] = deque()
        self._ended = False
        #: The bytes queued for the client and not yet handed to its socket.
        self.queued = 0
        #: Whether it was disconnected for letting too much pile up.
        self.dropped = False
        #: How many of the instrument's bytes it lost when it was dropped.
        self.lost = 0
        #: Bytes the client wrote, and bytes its socket took.
        self.received = 0
        self.delivered = 0
        #: Whether it was disconnected for writing a line of an HTTP request.
        self.http = False

    def tap(self, chunk: bytes) -> None:
        """Queue *chunk* for the client, on the executor's thread; never waits on the client.

        Past :attr:`max_queued` the queue is emptied, the client marked
        dropped and its socket shut down; the reporting is left to the
        client's own thread.
        """
        with self._ready:
            if self._ended:
                if self.dropped:
                    self.lost += len(chunk)
                return
            if self.queued + len(chunk) <= self.max_queued:
                self._chunks.append(chunk)
                self.queued += len(chunk)
                self._ready.notify()
                return
            self.lost = self.queued + len(chunk)
            self._chunks.clear()
            self.queued = 0
            self.dropped = self._ended = True
            self._ready.notify()
        self._shutdown()

    def end(self) -> None:
        """Stop the writer and shut the socket down; what was still queued is discarded."""
        with self._ready:
            self._ended = True
            self._chunks.clear()
            self.queued = 0
            self._ready.notify()
        self._shutdown()

    def write_loop(self) -> None:
        """The writer: hand the queued bytes to the socket until the client ends."""
        while True:
            with self._ready:
                while not self._chunks and not self._ended:
                    self._ready.wait()
                if self._ended:
                    return
                parts: list[bytes] = []
                size = 0
                while self._chunks and size < _SEND_CHUNK:
                    part = self._chunks.popleft()
                    parts.append(part)
                    size += len(part)
                self.queued -= size
            try:
                self.conn.sendall(b"".join(parts))
            except OSError:
                self.end()
                return
            self.delivered += size

    def _shutdown(self) -> None:
        """Wake the client's reader and writer: both see the socket end."""
        with contextlib.suppress(OSError):
            self.conn.shutdown(socket.SHUT_RDWR)


class Passthrough:
    """The passthrough's listener and its one client: see the module's description.

    The listening socket is bound when it is built, so a port that is taken
    fails serve's start; :meth:`start` begins accepting.

    :param session: Serve's session: its executor, its shaping guard, the window it found.
    :param api: The panel's API, put on hold while a client attaches and is attached.
    :param recorder: Serve's recorder: no client attaches while its capture runs.
    :param sequences: The sequence runner: no client attaches while a sequence runs.
    :param live: The hub the passthrough's state is published on, when there is one.
    :param port: The TCP port on 127.0.0.1, :data:`DEFAULT_PORT` by default (0: any free one).
    :param attach_wait: Seconds an attach waits for the line to go quiet.
    :param max_queued: The bytes a client may let pile up before it is dropped.
    :param say: Where serve's lines go (one line, no prefix); the log only by default.
    :raises OSError: When the port cannot be bound.
    """

    def __init__(
        self,
        session: Session,
        api: Api,
        *,
        recorder: Recorder | None = None,
        sequences: SequenceRunner | None = None,
        live: LiveHub | None = None,
        port: int = DEFAULT_PORT,
        attach_wait: float = ATTACH_WAIT,
        max_queued: int = MAX_QUEUED,
        say: Callable[[str], None] | None = None,
    ) -> None:
        self.session = session
        self.executor = session.executor
        self.api = api
        self.recorder = recorder
        self.sequences = sequences
        self.live = live
        self.attach_wait = attach_wait
        self.max_queued = max_queued
        self._say = say
        self._listener = socket.create_server((BIND, port))
        self._listener.settimeout(_ACCEPT_POLL)
        #: The TCP port it listens on.
        self.port: int = self._listener.getsockname()[1]
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._state = "listening"
        self._peer: str | None = None
        self._client: _Client | None = None
        self._worker: threading.Thread | None = None
        self._accepter: threading.Thread | None = None
        #: How many clients attached, were refused, and were dropped.
        self.attaches = 0
        self.refusals = 0
        self.drops = 0
        #: The last client's outcome, as its last ``passthrough`` event carried it.
        self.last: dict[str, Any] | None = None

    @property
    def url(self) -> str:
        """What a client opens: ``socket://127.0.0.1:<port>``."""
        return f"socket://{BIND}:{self.port}"

    def state_json(self) -> dict[str, Any]:
        """The passthrough for ``/api/status``: where it listens, its client, its counts."""
        with self._lock:
            client = self._client
            body: dict[str, Any] = {
                "url": self.url,
                "state": self._state,
                "client": self._peer,
                "attaches": self.attaches,
                "refusals": self.refusals,
                "drops": self.drops,
                "max_queued_bytes": self.max_queued,
                "last": self.last,
            }
        if client is not None:
            body.update(
                queued_bytes=client.queued,
                from_client_bytes=client.received,
                to_client_bytes=client.delivered,
            )
        return body

    def start(self) -> None:
        """Begin accepting clients."""
        self._accepter = threading.Thread(
            target=self._accept_loop, name="kleidos-meter-passthrough", daemon=True
        )
        self._accepter.start()

    def close(self, *, wait: float = STATE_WAIT + 10.0) -> None:
        """Stop accepting, and detach the client (serve is stopping); again, nothing.

        :param wait: Seconds of real time to wait for the client's detach.
        """
        if self._closing.is_set():
            return
        self._closing.set()
        if self._accepter is not None:
            self._accepter.join(wait)
        with contextlib.suppress(OSError):
            self._listener.close()
        with self._lock:
            client, worker = self._client, self._worker
        if client is not None:
            client.end()
        if worker is not None:
            worker.join(wait)

    # -- accepting --------------------------------------------------------------------

    def _accept_loop(self) -> None:
        """Take each connection: the first to find the line free is served, the others refused."""
        while not self._closing.is_set():
            try:
                conn, address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError as error:  # the listener failed: nothing more is accepted
                if not self._closing.is_set():
                    _LOG.error("the passthrough stopped accepting: %s", error)
                return
            peer = f"{address[0]}:{address[1]}"
            with self._lock:
                holder = self._peer
                free = holder is None and not self._closing.is_set()
                if free:
                    self._peer, self._state = peer, "attaching"
            if not free:
                reason = "serve is stopping" if holder is None else f"{holder} holds the line"
                self._refuse(conn, peer, reason)
                continue
            conn.settimeout(None)
            with contextlib.suppress(OSError):
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            worker = threading.Thread(
                target=self._serve, args=(conn, peer), name="kleidos-meter-passthrough-client"
            )
            with self._lock:
                self._worker = worker
            worker.start()

    def _refuse(self, conn: socket.socket, peer: str, reason: str) -> None:
        """Close *conn* with nothing written, and say why."""
        with contextlib.suppress(OSError):
            conn.close()
        with self._lock:
            self.refusals += 1
        self._note(f"refused passthrough client {peer}: {reason}")
        self._publish({"state": "refused", "client": peer, "reason": reason})

    def _serve(self, conn: socket.socket, peer: str) -> None:
        """One client, on a thread of its own: attach, carry its bytes, detach."""
        try:
            try:
                client = self._attach(conn, peer)
            except Refused as refused:
                self._refuse(conn, peer, str(refused))
                return
            self._pump(client)
            self._detach(client)
        finally:
            with self._lock:
                self._peer = self._client = None
                self._state = "listening"

    # -- attaching --------------------------------------------------------------------

    def _attach(self, conn: socket.socket, peer: str) -> _Client:
        """Hold the panel, wait for the line, restore the settings, and hand the line over.

        :raises Refused: At the first step that fails; the panel is released then.
        """
        executor = self.executor
        self.api.hold(f"a passthrough client ({peer})")
        executor.polling = False
        try:
            if self._closing.is_set():
                raise Refused("serve is stopping")
            if self.sequences is not None and self.sequences.running:
                raise Refused("a measurement sequence is running")
            if self.api.pins.running:
                raise Refused("a pin map is being applied")
            self._wait_quiet()
            state = "idle" if self.recorder is None else self.recorder.state
            if state != "idle":
                raise Refused(f"serve's capture is {state}")
            status = parse_fields(executor.query_all(["SYST:STAT?"], wait=STATE_WAIT)[0])
            sink = status.get("sink", "idle")
            if sink != "idle" and self.session.foreign_window is None:
                raise Refused(f"a {sink} window serve opened is open")
            restored = self.session.shaping.restore()
            client = _Client(conn, peer, self.max_queued)
            self._wait_quiet()
            executor.hand_over(client.tap)
        except Refused:
            self._release()
            raise
        except (MeterError, TimeoutError) as error:
            self._release()
            raise Refused(f"the instrument could not be readied: {error or 'no answer'}") from None
        for name, found, back in restored:
            self._note(f"wrote back {name} {back} (it was {found}) for the passthrough")
        with self._lock:
            self._client = client
            self._state = "attached"
            self.attaches += 1
        self._note(f"passthrough client {peer} attached: the panel watches until it leaves")
        self._publish({"state": "attached", "client": peer})
        return client

    def _wait_quiet(self) -> None:
        """Wait until nothing is in flight or queued.

        :raises Refused: When the line is down, serve is stopping, or the line
            is not quiet within :attr:`attach_wait` seconds.
        """
        deadline = time.monotonic() + self.attach_wait
        while not self.executor.idle:
            if self._closing.is_set():
                raise Refused("serve is stopping")
            if self.executor.link_down:
                raise Refused("the instrument is not answering")
            if time.monotonic() >= deadline:
                in_flight = self.executor.in_flight
                what = "the line" if in_flight is None else repr(in_flight.message)
                raise Refused(f"{what} did not go quiet within {self.attach_wait:g} s")
            time.sleep(_IDLE_POLL)

    def _release(self) -> None:
        """Give the panel the line again: the hold released, the polls resumed."""
        self.executor.polling = True
        self.executor.poll_now()
        self.api.release()

    # -- attached ---------------------------------------------------------------------

    def _pump(self, client: _Client) -> None:
        """Carry the client's bytes to the instrument until it ends; its writer carries the rest."""
        writer = threading.Thread(
            target=client.write_loop, name="kleidos-meter-passthrough-writer", daemon=True
        )
        writer.start()
        held = b""
        try:
            while True:
                try:
                    data = client.conn.recv(_RECV_CHUNK)
                except OSError:
                    break
                if not data:
                    break
                client.received += len(data)
                # Whole lines only, each looked at first: a web page's
                # request to this port is an HTTP request whose body lines
                # would each run as a program message (docs/panel.md).
                held += data
                end = held.rfind(b"\n") + 1
                if end == 0 and len(held) > _MAX_HELD:
                    end = len(held)
                lines, held = held[:end], held[end:]
                if any(looks_like_http(line) for line in lines.splitlines()) or (
                    looks_like_http(held)
                ):
                    client.http = True
                    break
                if not lines:
                    continue
                try:
                    self.executor.forward(lines)
                except (MeterError, OSError) as error:
                    _LOG.error("the client's bytes could not be written: %s", error)
                    break
        finally:
            client.end()
            writer.join()
            with contextlib.suppress(OSError):
                client.conn.close()

    # -- detaching --------------------------------------------------------------------

    def _detach(self, client: _Client) -> None:
        """Take the line back, read the state again, and release the panel."""
        executor = self.executor
        executor.take_back()
        if client.http:
            with self._lock:
                self.drops += 1
            message = (
                f"dropped passthrough client {client.peer}: it wrote a line of an HTTP"
                " request (a web page, not a SCPI client), and nothing from that line on"
                " reached the instrument"
            )
            _LOG.warning(message)
            self._note(message)
        if client.dropped:
            with self._lock:
                self.drops += 1
            message = (
                f"dropped passthrough client {client.peer}: it let more than"
                f" {self.max_queued} bytes pile up, and lost {client.lost}"
            )
            _LOG.warning(message)
            self._note(message)
        window: dict[str, Any] | None = None
        try:
            status = parse_fields(executor.query_all(["SYST:STAT?"], wait=STATE_WAIT)[0])
            if status.get("sink", "idle") != "idle":
                self.session.note_window(status)
                window = typed_fields(status)
                self._note(
                    f"the client left a {status.get('sink')} window open"
                    f" (profile {status.get('profile')}): serve leaves it as it found it"
                )
        except (MeterError, TimeoutError) as error:
            self._note(f"could not read the state after the client: {error or 'no answer'}")
        finally:
            self._release()
        self._note(f"passthrough client {client.peer} detached: the panel has the line again")
        last = {
            "state": "detached",
            "client": client.peer,
            "dropped": client.dropped,
            "lost_bytes": client.lost,
            "window": window,
        }
        with self._lock:
            self.last = last
        self._publish(last)

    # -- reporting --------------------------------------------------------------------

    def _note(self, line: str) -> None:
        """One line of serve's output, and the log."""
        _LOG.info(line)
        if self._say is not None:
            self._say(line)

    def _publish(self, data: dict[str, Any]) -> None:
        """A ``status`` event of ``kind`` ``passthrough`` on the hub."""
        if self.live is not None:
            self.live.publish("status", {"kind": "passthrough", **data})
