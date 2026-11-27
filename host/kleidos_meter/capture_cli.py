# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""``kleidos-meter capture``: one capture, recorded to a run directory, with no panel.

Usage::

    kleidos-meter capture --profile STATE --seconds 10 --port /dev/cu.usbmodemXXXX1
    kleidos-meter capture --profile SLEEP --seconds 600 --channels CURR,VOLT --out ~/runs
    kleidos-meter capture --profile TRACE --sim       # a ring: until it is full

It is serve's recorder (:mod:`~kleidos_meter.panel.recorder`) on serve's
session, with no HTTP: the port is opened exclusively (a holder is named,
as serve names one), the line checked, the shaping settings read; the
profile and the channels are written, the capture started and recorded into
a run directory under ``--out`` (``$XDG_STATE_HOME/kleidos-meter/runs`` by
default; one inside a git work tree is a usage error), and stopped after
``--seconds`` (a ring profile may leave it out and runs until the ring is
full). Ctrl-C stops it early, and the run is finished either way. The
settings it changed are written back before the port is released, as serve
writes them back.

It prints the run's directory and what it holds. The exit statuses are
:data:`EXIT_STATUS`.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from .client import MeterError, MeterTimeout, PortBusyError, SerialTransport, Transport
from .invariant import present
from .panel import lockinfo
from .panel.executor import Executor
from .panel.recorder import CaptureRefused, DataDirRefused, Recorder, check_data_dir
from .panel.schema import SCHEMA
from .panel.serve import Session, default_data_dir
from .sim import SimulatedMeter

__all__ = ["EXIT_STATUS", "build_parser", "main"]

#: The profiles that capture to the ring, by their short forms.
_RING = frozenset({"TRAC", "BURS"})


#: What each exit status says.
EXIT_STATUS = {
    0: "the run is complete (every sample the instrument streamed is in it, or the dump holds"
    " the whole ring) and the settings were written back",
    1: "the port is held elsewhere, the instrument does not answer or refuses the capture, the"
    " run is incomplete, or the settings could not be written back",
    2: "a usage error (a data directory inside a git work tree among them)",
}


def build_parser() -> argparse.ArgumentParser:
    """The command line of ``kleidos-meter capture``.

    :returns: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="kleidos-meter capture",
        description="One capture, recorded to a run directory, with no panel.",
    )
    parser.add_argument(
        "--port", default=os.environ.get("KLEIDOS_METER_PORT"), help="the device node or URL"
    )
    parser.add_argument("--sim", action="store_true", help="capture from the simulated instrument")
    parser.add_argument("--profile", required=True, help="TRACE, STATE, SLEEP or BURST")
    parser.add_argument(
        "--seconds", type=float, default=None, help="how long (a ring profile may leave it out)"
    )
    parser.add_argument(
        "--channels",
        default=None,
        help="CURR,VOLT,POW,TEMP (comma-separated) or DEF; the instrument's own by default",
    )
    parser.add_argument("--label", default="", help="a note kept in the run's meta.json")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="the data directory ($XDG_STATE_HOME/kleidos-meter/runs)",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=15.0,
        help="seconds to wait for the instrument to answer (15)",
    )
    return parser


def _open(port: str) -> Transport:
    """The real port, opened exclusively."""
    return SerialTransport(port)


def _simulator() -> SimulatedMeter:
    """``--sim``'s instrument: the clock-driven simulator on the real clock."""
    return SimulatedMeter(clocked=True)


def main(
    argv: list[str] | None = None,
    *,
    stop: threading.Event | None = None,
    open_transport: Callable[[str], Transport] = _open,
    make_sim: Callable[[], SimulatedMeter] | None = None,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run ``kleidos-meter capture``.

    :param argv: The arguments after ``capture``, ``sys.argv[2:]`` when None.
    :param stop: Set to stop the capture early; on the main thread ``SIGINT``
        and ``SIGTERM`` set it too.
    :param open_transport: How a port is opened (a test's fake).
    :param make_sim: How ``--sim`` makes its instrument: the clock-driven
        simulator on the real clock by default.
    :param clock: The clock ``--seconds`` is measured on.
    :param out: Where the run's lines go, standard output by default.
    :param err: Where refusals go, standard error by default.
    :returns: The exit status (:data:`EXIT_STATUS`).
    :raises SystemExit: With status 2 on a usage error (argparse's).
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = build_parser()
    args = parser.parse_args(sys.argv[2:] if argv is None else argv)
    if not args.sim and not args.port:
        parser.error("name the port with --port or KLEIDOS_METER_PORT, or use --sim")
    profile_setting = present(SCHEMA.setting("profile"), "the profile setting")
    try:
        profile = profile_setting.parameter(args.profile)
    except ValueError as why:
        parser.error(f"--profile takes {why}")
    if profile not in _RING and args.seconds is None:
        parser.error(f"--seconds is needed: a {args.profile} capture runs until it is stopped")
    channels: object = None
    if args.channels is not None:
        text = args.channels.strip()
        default = text.upper() in ("DEF", "DEFAULT")
        channels = text if default else [name.strip() for name in text.split(",")]
    try:
        data_dir = check_data_dir(args.out or default_data_dir())
    except DataDirRefused as refused:
        parser.error(str(refused))

    def say(line: str) -> None:
        """One line of the run's progress, on *out*."""
        print(f"capture: {line}", file=out, flush=True)

    try:
        transport: Transport = (make_sim or _simulator)() if args.sim else open_transport(args.port)
    except PortBusyError as error:
        holder = lockinfo.read_holder(args.port)
        refusal = f"the port is held by {lockinfo.describe(holder)}" if holder else str(error)
        print(f"# {refusal}", file=err)
        return 1
    executor = Executor(transport, clock=clock)
    session = Session(executor)
    recorder = Recorder(session, data_dir, clock=clock, tool="kleidos-meter capture")
    executor.start()
    noted = False
    status = 1
    try:
        executor.wait_ready(args.ready_timeout)
        session.open()
        if not args.sim:
            try:
                note: dict[str, object] = {
                    "holder": "kleidos-meter capture",
                    "instrument": session.info.get("serial"),
                }
                lockinfo.write_info(args.port, note)
                noted = True
            except OSError as error:
                print(f"# could not leave a note of who holds the port: {error}", file=err)
        state = recorder.start(args.profile, channels, args.seconds, args.label)
        say(f"recording {data_dir / state['run']}")
        _wait(recorder, stop or threading.Event())
        meta = recorder.last_meta or {}
        notes, clean = session.close()
        for line in notes:
            say(line)
        status = 0 if meta.get("complete") and clean else 1
        say(_summary(meta))
    except CaptureRefused as refused:
        print(f"# the capture was refused: {refused.detail}", file=err)
        for entry in refused.errors:
            print(f"# {entry}", file=err)
    except MeterTimeout as error:
        print(f"# the instrument is not answering: {error}", file=err)
    except MeterError as error:
        print(f"# {error}", file=err)
    finally:
        recorder.close()
        executor.close()
        transport.close()
        if noted:
            lockinfo.remove_info(args.port)
    return status


def _summary(meta: dict[str, Any]) -> str:
    """One line of what a finished run holds."""
    stats = (meta.get("statistics") or {}).get("fields", {})
    stream = meta.get("stream") or {}
    trace = meta.get("trace") or {}
    parts = [
        f"run {meta.get('id')}",
        f"ended by {meta.get('ended_by')}",
        f"samples={stats.get('samples', '?')} dropped={stats.get('dropped', '?')}",
        f"stream={stream.get('samples', 0)} marks={stream.get('marks', 0)}",
    ]
    if trace:
        parts.append(f"trace={trace.get('count', trace.get('error'))}")
    parts.append("complete" if meta.get("complete") else "INCOMPLETE")
    return ", ".join(parts)


def _wait(recorder: Recorder, stop: threading.Event) -> None:
    """Wait for the capture's end; *stop* (and, on the main thread, a signal) ends it early."""
    # What getsignal() gave: a handler, SIG_DFL or SIG_IGN, which signal() takes back.
    previous: dict[signal.Signals, Any] = {}
    if threading.current_thread() is threading.main_thread():

        def handler(signum: int, frame: object) -> None:
            """``SIGINT`` or ``SIGTERM``: stop the capture early."""
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.getsignal(sig)
            signal.signal(sig, handler)
    try:
        while not recorder.wait_idle(0.05):
            if stop.is_set():
                with contextlib.suppress(CaptureRefused):  # it ended meanwhile
                    recorder.stop("stop")
    finally:
        for restored, action in previous.items():
            signal.signal(restored, action)
