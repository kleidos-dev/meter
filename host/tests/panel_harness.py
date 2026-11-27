# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""``kleidos-meter serve`` as a user runs it, for the end-to-end tests.

:func:`serving` runs serve's own ``main()`` on a thread, on the real clock,
with its panel on any free port and, when asked, its SCPI passthrough: the
simulator is the clock-driven one serve builds for ``--sim``, or whatever
transport a test opens (a port on the bench, a process). :class:`Served`
talks to the panel the way the page does (the change header on every
``POST``, JSON in and out), reads its Server-Sent Events
(:class:`EventReader`) and waits for a sequence or a capture to end.

The contract tests, the browser tests and the panel's bench test share it,
so the bench runs the same scenario the simulator runs in every test run.
"""

from __future__ import annotations

import http.client
import io
import json
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from kleidos_meter.client import Transport
from kleidos_meter.panel import serve

#: Real seconds anything a test waits for may take before it fails instead of hanging.
WAIT = 30.0

#: The header every change carries (``kleidos_meter.panel.guards``).
CHANGE = {"X-Kleidos-Meter": "1"}

_PANEL = re.compile(r"^serve: panel at http://127\.0\.0\.1:(\d+)/$", re.MULTILINE)
_PASSTHROUGH = re.compile(r"^serve: SCPI passthrough at (socket://127\.0\.0\.1:\d+)$", re.MULTILINE)


@dataclass
class Answer:
    """A response as a client saw it.

    :ivar status: The HTTP status.
    :ivar headers: Its headers, names in lower case.
    :ivar body: The JSON body, None when the response is not JSON.
    :ivar raw: The body's bytes.
    """

    status: int
    headers: dict[str, str]
    body: Any
    raw: bytes = b""


def until(condition: Callable[[], bool], what: str, wait: float = WAIT) -> None:
    """Wait in real time until *condition* holds.

    :raises AssertionError: When it does not within *wait* seconds.
    """
    deadline = time.monotonic() + wait
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


@dataclass
class Served:
    """A running serve: its panel's port, its passthrough, what it printed.

    :ivar port: The panel's TCP port on 127.0.0.1.
    :ivar passthrough: The passthrough's URL (``socket://127.0.0.1:<port>``), or None.
    :ivar out: What serve printed to standard output.
    :ivar err: What it printed to standard error.
    :ivar status: Its exit status once it ended, empty before.
    """

    port: int
    passthrough: str | None
    out: io.StringIO
    err: io.StringIO
    status: list[int] = field(default_factory=list)

    @property
    def url(self) -> str:
        """The panel's address, ``http://127.0.0.1:<port>/``."""
        return f"http://127.0.0.1:{self.port}/"

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = WAIT,
    ) -> Answer:
        """Send one request; a dict or list body goes as JSON, bytes as they are."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            data = (
                json.dumps(body).encode()
                if isinstance(body, dict | list)
                else body.encode()
                if isinstance(body, str)
                else body
            )
            sent = dict(headers or {})
            if data is not None:
                sent.setdefault("Content-Type", "application/json")
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            raw = response.read()
            seen = {name.lower(): value for name, value in response.getheaders()}
            is_json = seen.get("content-type", "").startswith("application/json")
            return Answer(response.status, seen, json.loads(raw) if raw and is_json else None, raw)
        finally:
            connection.close()

    def get(self, path: str, **kwargs: Any) -> Answer:
        """``GET`` *path*."""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: Any = b"", **kwargs: Any) -> Answer:
        """``POST`` *path* with the change header, as the page sends every change."""
        headers = {**CHANGE, **kwargs.pop("headers", {})}
        return self.request("POST", path, body, headers=headers, **kwargs)

    def sequence(self, name: str, params: dict[str, int] | None = None, wait: float = WAIT) -> Any:
        """Run one sequence to its end; its last state.

        :raises AssertionError: When serve refuses it or it does not end within *wait*.
        """
        started = self.post("/api/sequences/run", {"name": name, "params": params or {}})
        assert started.status == 200, (name, started.status, started.body)
        number = started.body["number"]
        state: dict[str, Any] = {}

        def ended() -> bool:
            state.update(self.get("/api/sequences").body)
            return state.get("number") == number and state.get("state") == "idle"

        until(ended, f"sequence {name} to end", wait)
        return state

    def capture_idle(self, wait: float = WAIT) -> Any:
        """Wait until the recorder is idle; its state."""
        state: dict[str, Any] = {}

        def idle() -> bool:
            state.update(self.get("/api/capture").body)
            return state.get("state") == "idle"

        until(idle, "the recorder to be idle", wait)
        return state


class EventReader:
    """``GET /api/events`` read on a thread of its own, every event kept.

    :param served: The serve to read from.
    """

    def __init__(self, served: Served) -> None:
        self._connection = http.client.HTTPConnection("127.0.0.1", served.port, timeout=WAIT)
        self._connection.request("GET", "/api/events")
        self._response = self._connection.getresponse()
        assert self._response.status == 200
        self._lock = threading.Lock()
        self._events: list[tuple[str, dict[str, Any]]] = []
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        name = ""
        data: list[str] = []
        try:
            while not self._stopped.is_set():
                line = self._response.fp.readline()
                if not line:
                    return
                text = line.decode("utf-8").rstrip("\n")
                if text.startswith("event: "):
                    name = text[len("event: ") :]
                elif text.startswith("data: "):
                    data.append(text[len("data: ") :])
                elif text == "" and data:
                    with self._lock:
                        self._events.append((name, json.loads("\n".join(data))))
                    name, data = "", []
        except (OSError, ValueError):
            return

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        """The events read so far, of *kind* only when given."""
        with self._lock:
            return [data for name, data in self._events if kind is None or name == kind]

    def sample_rows(self) -> int:
        """The sample rows the ``samples`` events carried so far."""
        return sum(int(batch["count"]) for batch in self.events("samples"))

    def close(self) -> None:
        """Stop reading and drop the connection."""
        self._stopped.set()
        self._connection.close()
        self._thread.join(5.0)


@contextmanager
def serving(
    argv: list[str],
    *,
    open_transport: Callable[[str], Transport] | None = None,
    ready: float = WAIT,
) -> Iterator[Served]:
    """``serve`` with *argv* (``--http 0`` added) running on a thread until the block ends.

    :param argv: serve's arguments: ``--sim`` or ``--port``, and any others.
    :param open_transport: How ``--port`` is opened; serve's own (the serial port) by default.
    :param ready: Seconds serve may take to hold the port.
    :raises AssertionError: When serve ends before it holds the port, or does not end after.
    """
    stop = threading.Event()
    out, err = io.StringIO(), io.StringIO()
    status: list[int] = []
    kwargs: dict[str, Any] = {"stop": stop, "out": out, "err": err}
    if open_transport is not None:
        kwargs["open_transport"] = open_transport
    thread = threading.Thread(
        target=lambda: status.append(serve.main([*argv, "--http", "0"], **kwargs)),
        daemon=True,
    )
    thread.start()
    try:
        until(
            lambda: "holding the port" in out.getvalue() or bool(status),
            "serve to hold the port",
            ready,
        )
        text = out.getvalue()
        panel = _PANEL.search(text)
        assert panel is not None and not status, (text, err.getvalue())
        passthrough = _PASSTHROUGH.search(text)
        yield Served(
            int(panel.group(1)),
            passthrough.group(1) if passthrough else None,
            out,
            err,
            status,
        )
    finally:
        stop.set()
        thread.join(WAIT)
        assert not thread.is_alive(), "serve did not end"
