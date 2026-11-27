# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""``kleidos-meter serve``: hold the meter's port for the panel, and hand it back as found.

Usage::

    kleidos-meter serve --port /dev/cu.usbmodemXXXX1
    kleidos-meter serve --sim            # the clock-driven simulated instrument

The port defaults to ``KLEIDOS_METER_PORT``. Serve is the port's one opener:
it opens it exclusively (a second opener, a second serve among them, is
refused at once and told which process holds it, from the note
:mod:`~kleidos_meter.panel.lockinfo` keeps), and every message goes through
its :class:`~kleidos_meter.panel.executor.Executor`. On start it waits until
the instrument answers, reads what it is (``*IDN?``, ``SYSTem:INFormation?``)
and its health (``SYSTem:HEALth?``), reports what the instrument had queued,
and takes a snapshot of the capture-shaping settings
(:mod:`~kleidos_meter.panel.shaping`). A capture or a null window already
open is reported and left alone: serve never closes a window it did not
open. It then holds the port until it is stopped (Ctrl-C, ``SIGTERM``).

On the way out it aborts an acquisition of its own still in flight, closes a
window it opened, writes back the settings it changed, releases the port and
removes its note. Serve never changes ``FORMat``, and never sends ``*RST``.

While it holds the port it serves the panel's page
(:mod:`~kleidos_meter.panel.static`, at ``/``) and its HTTP API
(:mod:`~kleidos_meter.panel.http`, below ``/api/``) on ``--bind`` and
``--http``, 127.0.0.1 port 8040 by default (0 takes any free port, and both
addresses are printed).
The panel is unauthenticated, so an address beyond the loopback is refused
unless ``--allow-remote`` is given too, and then serve warns that whoever
reaches it can move the relays. ``--scpi-passthrough [PORT]`` also listens
on 127.0.0.1, port 5025 unless it names one, and gives the line to one SCPI
client at a time, byte for byte (:mod:`~kleidos_meter.panel.passthrough`):
a QA run or a CLI tool opens ``socket://127.0.0.1:5025`` while serve holds
the port, and the panel watches meanwhile. The live samples and events stream from
``/api/events`` (:mod:`~kleidos_meter.panel.live`), and every capture
started through the API is recorded (:mod:`~kleidos_meter.panel.recorder`)
under ``--data-dir``, ``$XDG_STATE_HOME/kleidos-meter/runs`` by default; a
directory inside a git work tree is a usage error. ``--registry`` names the
calibration registry, the repository's ``calibration/`` by default: each
range's shunt is shown with the registry's figures and uncertainty, and an
override the panel writes is kept beside the data directory, in its parent's
``overrides/`` (:mod:`~kleidos_meter.panel.settings`). The measurement
sequences run on :mod:`~kleidos_meter.panel.sequences`. ``--elf`` names the
build whose ``firmware.elf`` the Health view holds the instrument's ``elf=``
to, the repository's ``meter`` build by default
(:mod:`~kleidos_meter.panel.local_elf`).

On the way out a passthrough client is detached, a sequence that runs is
aborted and its cleanup run, then a capture serve started that still runs is
stopped and its run finished, and the event streams end.

The exit statuses are :data:`EXIT_STATUS`.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from .. import pins
from ..client import MeterError, MeterTimeout, PortBusyError, SerialTransport, Transport
from ..protocol import ErrorEntry, parse_fields
from ..registry import ROOT as REGISTRY_ROOT
from ..sim import SimulatedMeter
from . import lockinfo
from .executor import DeviceErrors, Event, Executor, MessageInfo, Rebooted, Status
from .guards import is_loopback_bind
from .http import Api, PanelServer
from .live import LiveHub
from .local_elf import LocalElf
from .passthrough import DEFAULT_PORT as PASSTHROUGH_PORT
from .passthrough import Passthrough
from .recorder import DataDirRefused, Recorder, check_data_dir
from .sequences import SequenceRunner
from .settings import Calibration, OverrideStore
from .shaping import ShapingGuard
from .static import packaged_page

__all__ = [
    "DEFAULT_BIND",
    "DEFAULT_ELF",
    "DEFAULT_HTTP",
    "EXIT_STATUS",
    "TIMEOUT_CLOSE",
    "Session",
    "build_parser",
    "default_data_dir",
    "main",
]

#: Where the panel's HTTP API listens by default: the loopback only.
DEFAULT_BIND = "127.0.0.1"
DEFAULT_HTTP = 8040

#: The build the Health view compares the instrument's ``elf=`` with by
#: default: the repository's own ``meter`` build, beside the registry.
DEFAULT_ELF = REGISTRY_ROOT.parent / "firmware" / ".pio" / "build" / "meter" / "firmware.elf"

#: Seconds of real time each step of handing the instrument back may take.
TIMEOUT_CLOSE = 30.0


#: What each exit status says.
EXIT_STATUS = {
    0: "a clean stop",
    1: "the port is held elsewhere, the panel's or the passthrough's address cannot be bound, the"
    " instrument does not answer, or the settings could not be written back",
    2: "a usage error (a data directory inside a git work tree among them)",
}


def default_data_dir() -> Path:
    """``$XDG_STATE_HOME/kleidos-meter/runs``, or ``~/.local/state/kleidos-meter/runs``."""
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "kleidos-meter" / "runs"


def _tcp_port(text: str) -> int:
    """A TCP port number, 0 to 65535 (0: any free port).

    :raises argparse.ArgumentTypeError: For anything else.
    """
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"{text!r} is not a TCP port (0..65535)")
    return value


def build_parser() -> argparse.ArgumentParser:
    """The command line of ``kleidos-meter serve``.

    :returns: The parser.
    """
    parser = argparse.ArgumentParser(
        prog="kleidos-meter serve",
        description="Hold the meter's port for the panel, and hand it back as found.",
    )
    parser.add_argument(
        "--port", default=os.environ.get("KLEIDOS_METER_PORT"), help="the device node or URL"
    )
    parser.add_argument("--sim", action="store_true", help="serve the simulated instrument")
    parser.add_argument(
        "--sim-wiring",
        type=pins.wiring_argument,
        default=pins.RIG_WIRING,
        metavar="PAIRS",
        help="where the simulated board's parts are, key=value pairs over the rig's"
        " (sda=18,addr=0x41); the rig by default",
    )
    parser.add_argument("--bind", default=DEFAULT_BIND, help="the panel's address (127.0.0.1)")
    parser.add_argument(
        "--http", type=_tcp_port, default=DEFAULT_HTTP, help="the panel's port (8040; 0: any)"
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="let --bind name an address beyond the loopback (the panel is unauthenticated)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="where recordings go ($XDG_STATE_HOME/kleidos-meter/runs)",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="the calibration registry (the repository's calibration/)",
    )
    parser.add_argument(
        "--elf",
        type=Path,
        default=DEFAULT_ELF,
        help="the build's firmware.elf the instrument's elf= is compared with"
        " (the repository's meter build)",
    )
    parser.add_argument(
        "--scpi-passthrough",
        type=_tcp_port,
        nargs="?",
        const=PASSTHROUGH_PORT,
        default=None,
        metavar="PORT",
        help="give the line to one SCPI client at a time on 127.0.0.1 (port 5025; 0: any)",
    )
    parser.add_argument(
        "--ready-timeout",
        type=float,
        default=15.0,
        help="seconds to wait for the instrument to answer (15)",
    )
    return parser


class Session:
    """What serve knows of the instrument, and what it owes it on the way out.

    Built before the executor starts, so that what the first sync finds
    queued is kept as :attr:`startup_errors`.

    :param executor: The line's owner, not yet started.
    :param config: Where the panel listens and keeps its data, for the HTTP
        API and the recorder: ``bind``, ``http``, ``data_dir``, ``registry``.
    """

    def __init__(self, executor: Executor, config: dict[str, object] | None = None) -> None:
        self.executor = executor
        self.config = dict(config or {})
        self.shaping = ShapingGuard(executor)
        self._lock = threading.Lock()
        self._opened = False
        #: ``*IDN?``, ``SYSTem:INFormation?`` and ``SYSTem:HEALth?`` at the start.
        self.idn = ""
        self.info: dict[str, str] = {}
        self.health: dict[str, str] = {}
        #: What the instrument had queued before serve asked anything.
        self.startup_errors: list[ErrorEntry] = []
        #: ``SYSTem:STATus?`` of a window serve found open, until it closes.
        self.foreign_window: dict[str, str] | None = None
        #: How many reboots the polls saw.
        self.reboots = 0
        executor.add_listener(self._on_event)
        executor.on_sent(self._on_sent)

    def open(self) -> None:
        """Read what the instrument is and how it is, and take the settings' snapshot.

        :raises MeterError: When a query is refused or unanswered.
        """
        idn, info, health, status = self.executor.query_all(
            ["*IDN?", "SYST:INF?", "SYST:HEAL?", "SYST:STAT?"]
        )
        self.idn, self.info, self.health = idn, parse_fields(info), parse_fields(health)
        fields = parse_fields(status)
        with self._lock:
            self.foreign_window = fields if fields.get("sink", "idle") != "idle" else None
            self._opened = True
        self.shaping.take()

    def summary(self) -> list[str]:
        """What serve found, one line each, for its standard output."""
        lines = [
            f"{self.info.get('serial', '?')}, firmware {self.info.get('fw', '?')},"
            f" image {self.info.get('image', '?')}, elf {self.info.get('elf', '?')},"
            f" q={self.info.get('q', '?')}",
            f"reset {self.health.get('reset', '?')},"
            f" up {int(self.health.get('uptime_us', '0') or 0) / 1e6:.1f} s",
        ]
        lines += [f"the instrument had queued {entry}" for entry in self.startup_errors]
        window = self.foreign_window
        if window is not None:
            lines.append(
                f"a {window.get('sink')} window is open (profile {window.get('profile')}):"
                " serve leaves it as it found it"
            )
        return lines

    def close(self) -> tuple[list[str], bool]:
        """Abort serve's own acquisition and window, and write back the settings it changed.

        :returns: One line for each thing done, and whether all of it was done.
        """
        executor = self.executor
        executor.polling = False
        notes: list[str] = []
        try:
            in_flight = executor.in_flight
            if in_flight is not None and in_flight.acquisition:
                executor.abort().result(TIMEOUT_CLOSE)
                notes.append("aborted the acquisition in flight")
            reply = executor.call("SYST:STAT?", wait=TIMEOUT_CLOSE)
            status = parse_fields(reply.units[0] if reply.units else "")
            sink = status.get("sink", "idle")
            if sink != "idle" and self.foreign_window is None:
                executor.call("ABOR", wait=TIMEOUT_CLOSE)
                notes.append(f"closed the {sink} window serve had opened")
            elif sink != "idle":
                notes.append(f"left the {sink} window open, as serve found it")
            restored = self.shaping.restore()
        except (MeterError, TimeoutError) as error:
            notes.append(f"could not hand the instrument back as found: {error or 'no answer'}")
            return notes, False
        notes += [f"wrote back {name} {then} (it was {now})" for name, now, then in restored]
        return notes, True

    def note_window(self, status: dict[str, str]) -> None:
        """Take a window serve did not open for one it found: a passthrough client left it open.

        Serve leaves it open on the way out, as it leaves a window it found.

        :param status: ``SYSTem:STATus?``'s fields, its ``sink`` other than ``idle``.
        """
        with self._lock:
            self.foreign_window = dict(status)

    def _on_event(self, event: Event) -> None:
        """Keep what the polls and syncs learn: errors, reboots, a window that closed."""
        with self._lock:
            if isinstance(event, DeviceErrors) and not self._opened:
                self.startup_errors += event.errors
            elif isinstance(event, Rebooted):
                self.reboots += 1
                self.foreign_window = None  # a reboot closes every window
            elif isinstance(event, Status) and not event.window_open:
                self.foreign_window = None

    def _on_sent(self, info: MessageInfo) -> None:
        """An ``ABORt`` closes whatever window is open, the one serve found included."""
        if "ABORt" in info.patterns:
            with self._lock:
                self.foreign_window = None


def _open(port: str) -> Transport:
    """The real port, opened exclusively."""
    return SerialTransport(port)


def main(
    argv: list[str] | None = None,
    *,
    stop: threading.Event | None = None,
    open_transport: Callable[[str], Transport] = _open,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run ``kleidos-meter serve``.

    :param argv: The arguments after ``serve``, ``sys.argv[2:]`` when None.
    :param stop: Set to stop serve; on the main thread ``SIGINT`` and
        ``SIGTERM`` set it too.
    :param open_transport: How a port is opened (a test's fake).
    :param out: Where the session's lines go, standard output by default.
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
    try:
        data_dir = check_data_dir(args.data_dir or default_data_dir())
    except DataDirRefused as refused:
        parser.error(str(refused))
    remote = not is_loopback_bind(args.bind)
    if remote and not args.allow_remote:
        parser.error(
            f"--bind {args.bind} is beyond the loopback and the panel is unauthenticated:"
            " add --allow-remote to listen there anyway"
        )

    def say(line: str) -> None:
        print(f"serve: {line}", file=out, flush=True)

    if remote:
        print(
            f"# warning: the panel is unauthenticated; whoever reaches {args.bind} port"
            f" {args.http} can move the relays, start captures and change the settings",
            file=err,
        )

    try:
        transport = (
            SimulatedMeter(clocked=True, wiring=args.sim_wiring)
            if args.sim
            else open_transport(args.port)
        )
    except PortBusyError as error:
        holder = lockinfo.read_holder(args.port)
        if holder is None:
            print(f"# {error}", file=err)
        else:
            print(f"# the port is held by {lockinfo.describe(holder)}", file=err)
        return 1
    executor = Executor(transport)
    session = Session(
        executor,
        {
            "bind": args.bind,
            "http": args.http,
            "data_dir": data_dir,
            "registry": args.registry or REGISTRY_ROOT,
        },
    )
    live = LiveHub()
    executor.add_listener(live.on_event)
    executor.on_sent(live.on_sent)
    recorder = Recorder(session, data_dir, live=live)
    calibration = Calibration(
        executor, args.registry or REGISTRY_ROOT, OverrideStore(data_dir.parent / "overrides")
    )
    sequences = SequenceRunner(executor, recorder, calibration=calibration, live=live)
    api = Api(
        session,
        remote=remote,
        live=live,
        recorder=recorder,
        calibration=calibration,
        sequences=sequences,
        page=packaged_page(),
        local_elf=LocalElf(args.elf),
    )
    try:
        panel = PanelServer(api, args.bind, args.http)
    except OSError as error:
        print(f"# cannot listen on {args.bind} port {args.http}: {error}", file=err)
        transport.close()
        return 1
    passthrough: Passthrough | None = None
    if args.scpi_passthrough is not None:
        try:
            passthrough = Passthrough(
                session,
                api,
                recorder=recorder,
                sequences=sequences,
                live=live,
                port=args.scpi_passthrough,
                say=say,
            )
        except OSError as error:
            print(f"# cannot listen on 127.0.0.1 port {args.scpi_passthrough}: {error}", file=err)
            panel.stop()
            transport.close()
            return 1
        api.passthrough = passthrough
    executor.start()
    live.start()
    status = 0
    noted = False
    signals = _Signals(stop or threading.Event(), say)
    signals.__enter__()
    try:
        executor.wait_ready(args.ready_timeout)
        session.open()
        if not args.sim:
            try:
                note: dict[str, object] = {
                    "instrument": session.info.get("serial", ""),
                    "panel": panel.url,
                }
                if passthrough is not None:
                    note["passthrough"] = passthrough.url
                lockinfo.write_info(args.port, note)
                noted = True
            except OSError as error:
                print(f"# could not leave a note of who holds the port: {error}", file=err)
        for line in session.summary():
            say(line)
        panel.start()
        say(f"panel at {panel.url}")
        say(f"panel API at {panel.url}api/")
        if passthrough is not None:
            passthrough.start()
            say(f"SCPI passthrough at {passthrough.url}")
        say(f"recordings in {data_dir}")
        say("holding the port; stop with Ctrl-C")
        _hold(signals.stop)
        panel.stop()
        if passthrough is not None:
            passthrough.close()
        if sequences.running:
            say("aborting the sequence; its cleanup runs")
        sequences.close()
        api.pins.close()
        last = recorder.last_meta
        recorder.close()
        if recorder.last_meta is not last and recorder.last_meta is not None:
            say(f"stopped the capture and finished run {recorder.last_meta['id']}")
        live.close()
        notes, clean = session.close()
        for line in notes:
            say(line)
        status = 0 if clean else 1
    except MeterTimeout as error:
        print(f"# the instrument is not answering: {error}", file=err)
        status = 1
    except MeterError as error:
        print(f"# {error}", file=err)
        status = 1
    except _Abandoned:
        print(
            "# abandoned: the instrument was not handed back (a setting serve changed, a"
            " window it opened or a sequence's relays may be as it left them)",
            file=err,
        )
        status = 1
    finally:
        panel.stop()
        if passthrough is not None:
            passthrough.close()
        sequences.close()
        api.pins.close()
        recorder.close()
        live.close()
        executor.close()
        transport.close()
        if noted:
            lockinfo.remove_info(args.port)
        signals.__exit__()
    say("released the port")
    return status


class _Abandoned(BaseException):
    """A third stop signal: the hand-back is abandoned."""


class _Signals:
    """``SIGINT`` and ``SIGTERM`` on the main thread, from the hold to the port's release.

    The first sets *stop*; a second, while the instrument is handed back
    (a sequence's cleanup, a ring's dump, the settings written back), is told
    to wait; a third abandons the hand-back (:class:`_Abandoned`), and any
    later one is ignored while the port is released. Off the main thread
    nothing is installed and only *stop* ends the hold.

    :param stop: What the first signal sets.
    :param say: Prints one line of serve's own.
    """

    def __init__(self, stop: threading.Event, say: Callable[[str], None]) -> None:
        self.stop = stop
        self.say = say
        self.count = 0
        self._previous: dict[int, Any] = {}

    def __enter__(self) -> _Signals:
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                self._previous[sig] = signal.getsignal(sig)
                signal.signal(sig, self.handler)
        return self

    def __exit__(self, *_: object) -> None:
        for sig, action in self._previous.items():
            signal.signal(sig, action)
        self._previous.clear()

    def handler(self, signum: int, frame: object) -> None:
        """One stop signal: stop, then wait, then abandon.

        A stop the caller set counts as the first signal.
        """
        self.count = max(self.count, 1 if self.stop.is_set() else 0) + 1
        if self.count == 1:
            self.stop.set()
        elif self.count == 2:
            self.say(
                "still handing the instrument back (its settings, a window it opened, a"
                " sequence's cleanup); a third Ctrl-C or SIGTERM abandons it"
            )
        elif self.count == 3:
            raise _Abandoned


def _hold(stop: threading.Event) -> None:
    """Wait for *stop* (set by the first signal :class:`_Signals` takes, or by the caller)."""
    while not stop.wait(0.2):
        pass
