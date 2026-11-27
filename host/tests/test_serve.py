# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""``kleidos-meter serve``: the port held, the instrument handed back as found.

The instrument is the clock-driven simulator on a fake clock throughout; a
port another opener holds is a fake opener that raises ``PortBusyError`` as
``SerialTransport`` does when the port's ``flock`` is taken.
"""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import pytest
from conftest import SIM_SERIAL, FakeClock
from kleidos_meter import cli
from kleidos_meter.client import MeterError, PortBusyError
from kleidos_meter.panel import lockinfo, serve
from kleidos_meter.panel.executor import Executor, Rebooted, Status, analyze
from kleidos_meter.panel.serve import Session, default_data_dir, main
from kleidos_meter.panel.shaping import (
    SETTINGS,
    ShapingGuard,
    changed_settings,
    changes_averaging,
)
from kleidos_meter.pins import RIG_WIRING
from kleidos_meter.sim import SimulatedMeter
from panel_harness import serving as serving_panel

#: Real seconds a test waits for anything before it fails instead of hanging.
WAIT = 10.0


@pytest.fixture(autouse=True)
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A runtime directory of the test's own, for the notes of who holds a port."""
    directory = tmp_path / "run"
    directory.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(directory))
    monkeypatch.setattr(serve, "DEFAULT_HTTP", 0)  # any free port: never a running panel's
    return directory


def clocked() -> tuple[SimulatedMeter, FakeClock]:
    """A clock-driven simulator on a fake clock that its own reads move."""
    clock = FakeClock()
    return SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep), clock


def until(condition: Callable[[], bool], what: str) -> None:
    """Wait in real time until *condition* holds."""
    deadline = time.monotonic() + WAIT
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.001)


@contextmanager
def session_on(sim: SimulatedMeter, clock: FakeClock) -> Iterator[Session]:
    """A session on *sim*, opened; its executor closed after."""
    executor = Executor(sim, clock=clock)
    session = Session(executor)
    executor.start()
    try:
        executor.wait_ready(WAIT)
        session.open()
        yield session
    finally:
        executor.close()


class Ports:
    """Fake openers: each port opens once, as an exclusive ``flock`` lets it."""

    def __init__(self) -> None:
        self.held: set[str] = set()
        self.made: list[SimulatedMeter] = []

    def open(self, port: str) -> SimulatedMeter:
        if port in self.held:
            raise PortBusyError(f"{port} is held by another opener (kleidos-meter serve, ...)")
        self.held.add(port)
        clock = FakeClock()
        sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep)
        original = sim.close

        def close() -> None:
            self.held.discard(port)
            original()

        sim.close = close  # type: ignore[method-assign]  # a planted close
        self.made.append(sim)
        return sim


@contextmanager
def serving(argv: list[str], **kwargs: Any) -> Iterator[tuple[io.StringIO, list[int]]]:
    """``serve`` running in a thread until the block ends; its output and status."""
    stop = threading.Event()
    out = io.StringIO()
    status: list[int] = []
    thread = threading.Thread(
        target=lambda: status.append(main(argv, stop=stop, out=out, **kwargs))
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


# -- the process --------------------------------------------------------------------


def test_serve_on_the_simulator_reports_what_it_found_and_releases_the_port() -> None:
    with serving(["--sim", "--http", "0"]) as (out, status):
        pass
    lines = out.getvalue().splitlines()
    assert status == [0]
    assert (
        lines[0]
        == f"serve: {SIM_SERIAL}, firmware 0.18.0, image meter, elf 5e1f0ba47c2291d3, q=cal"
    )
    assert lines[1].startswith("serve: reset poweron, up ")
    assert lines[-1] == "serve: released the port"


def test_a_second_serve_on_the_same_port_is_refused_with_the_holder_named() -> None:
    ports = Ports()
    with serving(["--port", "/dev/cu.fake-meter"], open_transport=ports.open) as (_, status):
        assert status == []
        note = json.loads(lockinfo.info_path("/dev/cu.fake-meter").read_text())
        assert note["pid"] == os.getpid() and note["instrument"] == SIM_SERIAL
        err = io.StringIO()
        refused = main(["--port", "/dev/cu.fake-meter"], open_transport=ports.open, err=err)
        assert refused == 1
        assert err.getvalue().startswith(
            f"# the port is held by kleidos-meter serve (pid {os.getpid()}, since "
        )
        assert "fake-meter" not in note.get("port", "")  # the path stays out of the note
    assert status == [0]
    assert not lockinfo.info_path("/dev/cu.fake-meter").exists()  # removed on the way out


def test_a_busy_port_with_no_live_holder_is_reported_as_the_port_reports_it() -> None:
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    path = lockinfo.info_path("/dev/cu.fake-meter")
    path.write_text(json.dumps({"holder": "kleidos-meter serve", "pid": gone.pid}))
    ports = Ports()
    ports.held.add("/dev/cu.fake-meter")  # held by some other tool
    err = io.StringIO()
    assert main(["--port", "/dev/cu.fake-meter"], open_transport=ports.open, err=err) == 1
    assert err.getvalue().startswith("# /dev/cu.fake-meter is held by another opener")
    assert not path.exists()  # the stale note is gone


def test_no_port_and_no_sim_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KLEIDOS_METER_PORT", raising=False)
    with pytest.raises(SystemExit) as raised:
        main([])
    assert raised.value.code == 2
    for bad in ("70000", "eighty"):
        with pytest.raises(SystemExit) as raised:
            main(["--sim", "--http", bad])
        assert raised.value.code == 2


def test_a_real_port_is_opened_exclusively_through_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []

    def transport(port: str) -> str:
        opened.append(port)
        return "port"

    monkeypatch.setattr(serve, "SerialTransport", transport)
    opened_by: object = serve._open("/dev/cu.fake-meter")
    assert opened_by == "port"
    assert opened == ["/dev/cu.fake-meter"]


def test_what_serve_did_on_the_way_out_is_printed() -> None:
    ports = Ports()

    def busy(port: str) -> SimulatedMeter:
        sim = ports.open(port)
        sim.write(b"SENS:PROF STAT;:INIT\n")  # a window some earlier client left open
        return sim

    with serving(["--port", "/dev/cu.fake-meter"], open_transport=busy) as (out, status):
        assert "a stream window is open (profile STATE)" in out.getvalue()
    assert status == [0]
    assert "serve: left the stream window open, as serve found it" in out.getvalue()


def test_an_instrument_that_never_answers_ends_serve_with_status_1() -> None:
    def silent(port: str) -> SimulatedMeter:
        sim = SimulatedMeter(clocked=True)
        sim.watchdog_due = float("inf")  # a console that has stopped answers nothing
        return sim

    err, out = io.StringIO(), io.StringIO()
    status = main(
        ["--port", "/dev/cu.fake-meter", "--ready-timeout", "0.3"],
        open_transport=silent,
        out=out,
        err=err,
    )
    assert status == 1
    assert err.getvalue().startswith("# the instrument is not answering")
    assert out.getvalue().splitlines()[-1] == "serve: released the port"


def test_a_refused_identity_query_ends_serve_with_status_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(self: Session) -> None:
        raise MeterError("'*IDN?': -113")

    monkeypatch.setattr(Session, "open", refuse)
    err = io.StringIO()
    assert main(["--sim"], err=err, out=io.StringIO()) == 1
    assert err.getvalue() == "# '*IDN?': -113\n"


def test_a_note_that_cannot_be_written_is_reported_and_serve_goes_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unwritable(port: str, info: dict[str, object]) -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(lockinfo, "write_info", unwritable)
    err = io.StringIO()
    with serving(["--port", "/dev/cu.fake-meter"], open_transport=Ports().open, err=err) as (
        _,
        status,
    ):
        assert err.getvalue().startswith("# could not leave a note of who holds the port")
    assert status == [0]


def test_sigint_stops_serve_on_the_main_thread() -> None:
    out = io.StringIO()
    previous = signal.getsignal(signal.SIGINT)
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        assert main(["--sim", "--http", "0"], out=out) == 0
    finally:
        timer.cancel()
    assert signal.getsignal(signal.SIGINT) is previous  # handed back
    assert out.getvalue().splitlines()[-1] == "serve: released the port"


def _signals_during_the_close(monkeypatch: pytest.MonkeyPatch, count: int) -> list[str]:
    """Make the session's close send *count* SIGINTs first; what the close got to do."""
    from kleidos_meter.panel import serve as serve_module

    original = serve_module.Session.close
    done: list[str] = []

    def close(self: object) -> tuple[list[str], bool]:
        for _ in range(count):
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)  # the handler runs on this, the main, thread
        result = original(self)  # type: ignore[arg-type]  # the real one
        done.append("closed")
        return result

    monkeypatch.setattr(serve_module.Session, "close", close)
    return done


def test_a_second_signal_during_the_hand_back_is_told_and_does_not_cut_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second Ctrl-C used to raise KeyboardInterrupt in the cleanup, skipping the
    hand-back (P5)."""
    done = _signals_during_the_close(monkeypatch, 1)
    out, err = io.StringIO(), io.StringIO()
    previous = signal.getsignal(signal.SIGINT)
    stop = threading.Event()
    stop.set()  # the first stop: serve goes straight to its cleanup
    assert main(["--sim", "--http", "0"], stop=stop, out=out, err=err) == 0
    assert done == ["closed"]
    assert "still handing the instrument back" in out.getvalue()
    assert out.getvalue().splitlines()[-1] == "serve: released the port"
    assert signal.getsignal(signal.SIGINT) is previous


def test_a_third_signal_abandons_the_hand_back_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = _signals_during_the_close(monkeypatch, 2)
    out, err = io.StringIO(), io.StringIO()
    previous = signal.getsignal(signal.SIGINT)
    stop = threading.Event()
    stop.set()
    assert main(["--sim", "--http", "0"], stop=stop, out=out, err=err) == 1
    assert done == []  # abandoned before the settings were written back
    assert "abandoned" in err.getvalue()
    assert out.getvalue().splitlines()[-1] == "serve: released the port"
    assert signal.getsignal(signal.SIGINT) is previous


def test_a_signal_after_the_third_is_ignored_while_the_port_is_released() -> None:
    said: list[str] = []
    signals = serve._Signals(threading.Event(), said.append)
    signals.handler(signal.SIGINT, None)
    assert signals.stop.is_set() and said == []
    signals.handler(signal.SIGTERM, None)
    assert len(said) == 1 and said[0].startswith("still handing the instrument back")
    with pytest.raises(serve._Abandoned):
        signals.handler(signal.SIGINT, None)
    signals.handler(signal.SIGINT, None)  # and nothing more
    assert signals.count == 4 and len(said) == 1


def test_the_cli_dispatches_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def dispatched(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(serve, "main", dispatched)
    assert cli.main(["serve", "--sim", "--bind", "127.0.0.1"]) == 0
    assert seen == [["--sim", "--bind", "127.0.0.1"]]


def test_the_data_directory_defaults_to_the_users_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_data_dir() == tmp_path / "kleidos-meter" / "runs"
    monkeypatch.delenv("XDG_STATE_HOME")
    assert default_data_dir() == Path.home() / ".local/state/kleidos-meter/runs"


# -- what serve owes the instrument -----------------------------------------------


def test_every_setting_a_session_changed_is_written_back_to_the_snapshot() -> None:
    sim, clock = clocked()
    # Not the defaults: serve restores what it found, not what *RST would set.
    with sim._lock:
        sim.profile, sim.channels, sim.sample_count, sim.format = "SLEEP", ["i_ua"], 50, "CSV"
        sim.averaging["SLEEP"] = replace(sim.averaging["SLEEP"], on=True, count=64, shunt_us=280)
    with session_on(sim, clock) as session:
        found = dict(session.shaping.snapshot)
        assert found["profile"] == "SLEEP" and found["format"] == "CSV"
        changes = [
            "SENS:AVER:COUN 128;:SENS:CURR:APER 0.00054;:SENS:AVER OFF",
            "SYST:COMM:I2C:FREQ 1000000",
            "SENS:PROF BURS",
            "SENS:CURR:RANG:IND 1",
            "SENS:CAPT STAT",
            "SENS:CAPT:LIM ON",
            "SENS:CURR:THR 0.05",
            "SENS:CURR:THR:HYST 0.001",
            "SENS:CURR:THR:MARK ON",
            "SAMP:COUN 777",
            "TRIG:SOUR LEV",
            "TRIG:LEV 0.02",
            "TRIG:SLOP NEG",
            "TRIG:PRET 10",
            "FORM TEXT",
        ]
        for change in changes:
            assert session.executor.call(change, wait=WAIT).errors == []
        changed = session.shaping.read()
        assert all(changed[s.name] != found[s.name] for s in SETTINGS)
        assert session.shaping.dirty == {s.name for s in SETTINGS}
        notes, clean = session.close()
        assert clean
        assert session.shaping.read() == found
        assert session.shaping.dirty == set()
    assert "wrote back average of SLEEP 1 (it was 0)" in notes
    assert "wrote back profile SLEEP (it was BURST)" in notes
    assert 'wrote back channels "CURR" (it was "")' in notes


def test_a_session_that_changed_nothing_writes_nothing_back() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        session.executor.call("SENS:PROF?;:SAMP:COUN?", wait=WAIT)
        sent = len(sim.messages)
        assert session.close() == ([], True)
    assert not any("SENS:PROF " in message for message in sim.messages[sent:])


def test_a_setting_put_back_to_its_value_meanwhile_is_not_written() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        session.executor.call("SAMP:COUN 5", wait=WAIT)
        session.executor.call("SAMP:COUN 100", wait=WAIT)
        assert session.shaping.restore() == []
        assert session.shaping.dirty == set()


def test_the_profile_alone_is_written_back_with_the_channels_it_resets() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        session.executor.call("SENS:PROF STAT", wait=WAIT)
        restored = session.shaping.restore()
    assert [name for name, _, _ in restored] == ["profile", "channels"]
    assert sim.profile == "TRACE"


def test_the_channels_are_written_after_a_profile_even_when_they_read_the_same() -> None:
    sim, clock = clocked()
    with sim._lock:
        sim.profile = "STATE"  # its default channel: "CURR"
    with session_on(sim, clock) as session:
        session.executor.call('SENS:PROF SLE;:SENS:FUNC:ON "CURR"', wait=WAIT)
        restored = session.shaping.restore()
    assert restored == [("profile", "SLEEP", "STATE"), ("channels", '"CURR"', '"CURR"')]
    assert sim.profile == "STATE" and sim.channels == ["i_ua"]


def test_serve_closes_its_own_window_and_aborts_its_own_acquisition() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        session.executor.call("SENS:PROF STAT;:INIT", wait=WAIT)
        assert session.close() == (
            [
                "closed the stream window serve had opened",
                "wrote back profile TRACE (it was STATE)",
                'wrote back channels "" (it was "CURR")',
            ],
            True,
        )
    assert sim.capture is None and sim.last_stats is not None

    clock = FakeClock()
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=lambda _: time.sleep(0.0005))
    with session_on(sim, clock) as session:
        session.executor.polling = False
        until(lambda: session.executor.in_flight is None, "a poll already written")
        session.executor.call("SENS:PROF STAT;:SAMP:COUN 500000", wait=WAIT)
        measuring = session.executor.submit("MEAS:CURR?")
        until(lambda: session.executor.in_flight is not None, "the acquisition")
        notes, clean = session.close()
        assert clean and notes[0] == "aborted the acquisition in flight"
        assert measuring.result(WAIT).codes == [-230]
        assert sim.sample_count == 100


def test_a_window_serve_found_open_is_left_open_and_reported() -> None:
    sim, clock = clocked()
    sim.write(b"SENS:PROF STAT;:INIT\n")
    with session_on(sim, clock) as session:
        assert session.foreign_window is not None
        assert session.summary()[-1] == (
            "a stream window is open (profile STATE): serve leaves it as it found it"
        )
        assert session.close() == (["left the stream window open, as serve found it"], True)
    assert sim.capture is not None  # still open


def test_a_setting_serve_cannot_write_back_under_a_window_it_did_not_open_fails_the_close() -> None:
    sim, clock = clocked()
    sim.write(b"SENS:PROF STAT;:INIT\n")
    with session_on(sim, clock) as session:
        session.shaping._note(analyze("SAMP:COUN 5"))  # as a message before the window opened
        with sim._lock:
            sim.sample_count = 5
        notes, clean = session.close()
    assert not clean
    assert notes[-1].startswith("could not hand the instrument back as found: writing back")
    assert "-221" in notes[-1]


def test_a_closed_window_a_reboot_or_an_abort_ends_what_serve_found() -> None:
    sim, clock = clocked()
    sim.write(b"SENS:PROF STAT;:INIT\n")
    with session_on(sim, clock) as session:
        session._on_event(Status({"sink": "stream"}, {}))
        assert session.foreign_window is not None
        session._on_event(Status({"sink": "idle"}, {}))
        assert session.foreign_window is None
        session.foreign_window = {"sink": "stream"}
        session._on_event(Rebooted({"reset": "panic"}))
        assert session.foreign_window is None and session.reboots == 1
        session.foreign_window = {"sink": "stream"}
        session.executor.call("ABOR", wait=WAIT)
        assert session.foreign_window is None


def test_what_the_instrument_had_queued_is_reported_at_the_start() -> None:
    sim, clock = clocked()
    with sim._lock:
        sim._fail(116, "a reset (panic) released k1 (path=none)")
    with session_on(sim, clock) as session:
        assert [entry.code for entry in session.startup_errors] == [116]
        assert session.summary()[2] == (
            'the instrument had queued 116,"Relays released by reset;'
            'a reset (panic) released k1 (path=none)"'
        )


def test_a_line_that_goes_down_before_the_close_is_reported() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        session.executor.close()
        notes, clean = session.close()
    assert not clean
    assert notes == ["could not hand the instrument back as found: the executor has stopped"]


# -- the averaging, each profile's ---------------------------------------------------


def averaging(sim: SimulatedMeter, profile: str) -> tuple[bool, int, int]:
    """A profile's override on the simulator: whether it is on, its count and its time."""
    with sim._lock:
        kept = sim.averaging[profile]
        return kept.on, kept.count, kept.shunt_us


#: Each profile's override as the simulator boots: off, with the profile's own figures.
BOOTED = {profile: averaging(SimulatedMeter(), profile) for profile in ("TRACE", "STATE", "SLEEP")}


def send(session: Session, *messages: str) -> None:
    for message in messages:
        assert session.executor.call(message, wait=WAIT).errors == [], message


def test_serves_exit_writes_the_averaging_back() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        send(session, "SENS:AVER:COUN 16;:SENS:AVER ON")
        assert averaging(sim, "TRACE")[:2] == (True, 16)
        notes, clean = session.close()
        assert clean and session.shaping.dirty == set()
    assert notes == [
        "wrote back average_count of TRACE 1 (it was 16)",
        "wrote back average of TRACE 0 (it was 1)",
    ]
    assert averaging(sim, "TRACE") == BOOTED["TRACE"]
    assert not any("SENS:PROF " in message for message in sim.messages)  # it never left TRACE


def test_an_averaging_found_on_is_put_back_on_with_its_figures() -> None:
    sim, clock = clocked()
    with sim._lock:
        sim.averaging["TRACE"] = replace(sim.averaging["TRACE"], on=True, count=64, shunt_us=280)
    with session_on(sim, clock) as session:
        send(session, "SENS:AVER:COUN 4")  # still on
        assert [name for name, _, _ in session.shaping.restore()] == ["average_count of TRACE"]
        assert averaging(sim, "TRACE") == (True, 64, 280)
        send(session, "SENS:AVER:COUN 4;:SENS:AVER:COUN 64")  # put back by its caller
        sent = len(sim.messages)
        assert session.shaping.restore() == []
    assert not any("SENS:AVER OFF" in message for message in sim.messages[sent:])


def test_each_profile_the_session_may_have_changed_is_put_back() -> None:
    sim, clock = clocked()
    with sim._lock:
        sim.averaging["STATE"] = replace(sim.averaging["STATE"], on=True, count=16)
    with session_on(sim, clock) as session:
        send(session, "SENS:PROF STAT")
        session.shaping.learn()  # as the panel does before it changes it
        send(session, "SENS:AVER OFF;:SENS:AVER:COUN 256;:SENS:PROF SLE;:SENS:AVER ON")
        restored = session.shaping.restore()
    assert [name for name, _, _ in restored] == [
        "average of SLEEP",  # the live one first; nothing was read of it: put back to off
        "average_count of STATE",
        "average of STATE",
        "profile",
        "channels",
    ]
    assert averaging(sim, "STATE")[:2] == (True, 16)
    assert averaging(sim, "SLEEP") == BOOTED["SLEEP"]
    assert averaging(sim, "TRACE") == BOOTED["TRACE"]
    assert sim.profile == "TRACE"


def test_what_is_found_of_a_profile_is_read_once_before_the_panel_changes_it() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        send(session, "SENS:AVER:COUN 16;:SENS:AVER ON")
        session.shaping.learn()  # TRACE was read at the start: what serve wrote is not found
        session.shaping.restore()
    assert averaging(sim, "TRACE") == BOOTED["TRACE"]


def test_a_figure_set_with_the_averaging_off_is_put_back_too_though_it_reads_the_same() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        send(session, "SENS:AVER:COUN 1024")
        assert averaging(sim, "TRACE")[:2] == (False, 1024)
        assert session.shaping.restore() == []  # nothing a query answers differed
    assert averaging(sim, "TRACE") == BOOTED["TRACE"]


def test_the_clock_is_put_back_before_the_averaging_that_needs_it() -> None:
    sim, clock = clocked()
    with sim._lock:
        sim.scl_hz = 1_000_000
        sim.averaging["TRACE"] = replace(sim.averaging["TRACE"], on=True, count=4, shunt_us=50)
    with session_on(sim, clock) as session:
        send(session, "SENS:AVER OFF", "SYST:COMM:I2C:FREQ 50000")
        # At 50 kHz the instrument refuses the averaging serve found (-221).
        reply = session.executor.call("SENS:AVER ON", wait=WAIT)
        assert [entry.code for entry in reply.errors] == [-221]
        session.shaping.restore()
    assert averaging(sim, "TRACE") == (True, 4, 50) and sim.scl_hz == 1_000_000


def test_a_refused_change_on_burst_is_nothing_to_put_back() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        send(session, "SYST:COMM:I2C:FREQ 1000000", "SENS:PROF BURS")
        session.shaping.take()
        reply = session.executor.call("SENS:AVER ON", wait=WAIT)
        assert [entry.code for entry in reply.errors] == [-221]
        assert session.shaping.dirty == {"average_count", "average_aperture", "average"}
        assert session.close() == ([], True)


def test_a_reset_clears_what_was_found_and_it_is_put_back() -> None:
    sim, clock = clocked()
    with sim._lock:
        sim.averaging["TRACE"] = replace(sim.averaging["TRACE"], on=True, count=16)
    with session_on(sim, clock) as session:
        found = session.shaping.read()
        send(session, "*RST")
        notes, clean = session.close()
        assert clean and session.shaping.read() == found
    assert averaging(sim, "TRACE")[:2] == (True, 16)


def test_an_averaging_that_reads_back_wrong_fails_the_restore() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        send(session, "SENS:AVER:COUN 16;:SENS:AVER ON")
        with session.shaping._lock:
            session.shaping._found["TRACE"] = {**session.shaping._found["TRACE"], "average": "2"}
        with pytest.raises(MeterError, match="the averaging of TRACE read back changed"):
            session.shaping.restore()
        assert "average" in session.shaping.dirty


def test_an_averaging_that_cannot_be_put_back_under_a_window_fails_the_close() -> None:
    sim, clock = clocked()
    with session_on(sim, clock) as session:
        send(session, "SENS:AVER ON")
        send(session, "INIT")
        session.note_window({"sink": "ring"})
        notes, clean = session.close()
    assert not clean and "writing back the settings was refused: -221" in notes[-1]


def test_serves_exit_leaves_the_pin_map_alone() -> None:
    clock = FakeClock()
    wiring = replace(RIG_WIRING, relay1=16)
    sim = SimulatedMeter(clocked=True, clock=clock, sleep=clock.sleep, wiring=wiring)
    with session_on(sim, clock) as session:
        send(session, "SYST:PINS:STAG REL1,16,LOW;:SYST:PINS:APPL", "SENS:AVER ON")
        applied = sim.pins_live
        notes, clean = session.close()
    assert clean and notes == ["wrote back average of TRACE 0 (it was 1)"]
    assert sim.pins_live == applied and applied.relay1 == 16
    assert not any("SYST:PINS" in message for message in sim.messages[-6:])


# -- the settings' table ------------------------------------------------------------


def test_what_changes_each_setting() -> None:
    assert changed_settings(analyze("*RST")) == {s.name for s in SETTINGS}
    assert changed_settings(analyze("CONF:VOLT")) == {"profile", "channels", "range"}
    assert changed_settings(analyze("MEAS:CURR? 0.1")) == {"profile", "channels", "range"}
    assert changed_settings(analyze("SENS:PROF?;:SAMP:COUN?")) == set()
    averaged = {"average_count", "average_aperture", "average"}
    for setter in ("SENS:AVER ON", "SENS:AVER:COUN 4", "SENS:CURR:APER 0.00054"):
        assert changed_settings(analyze(setter)) == averaged and changes_averaging(analyze(setter))
    assert changes_averaging(analyze("*RST")) and not changes_averaging(analyze("SENS:PROF STAT"))
    channels = next(s for s in SETTINGS if s.name == "channels")
    assert channels.write_unit('""') == "SENS:FUNC:ON:DEF"
    assert channels.write_unit('"CURR","VOLT"') == 'SENS:FUNC:ON "CURR","VOLT"'


def test_a_setting_that_reads_back_wrong_fails_the_restore() -> None:
    class Stubborn:
        """An executor whose instrument keeps its sample count whatever is written."""

        def __init__(self) -> None:
            self.sent: list[str] = []

        def on_sent(self, hook: Callable[[object], None]) -> None:
            pass

        def query_all(self, units: list[str]) -> list[str]:
            return ["5" for _ in units]

        def call(self, message: str) -> object:
            self.sent.append(message)
            return type("R", (), {"errors": []})()

    stubborn = Stubborn()
    guard = ShapingGuard(stubborn)  # type: ignore[arg-type]  # a stand-in
    guard.snapshot = {"sample_count": "100"}
    guard._note(analyze("SAMP:COUN 5"))
    with pytest.raises(MeterError, match="read back changed: sample_count"):
        guard.restore()
    assert stubborn.sent == ["SAMP:COUN 100"]
    assert guard.dirty == {"sample_count"}


# -- the note of who holds a port -----------------------------------------------------


def test_the_note_is_private_named_by_a_digest_and_removed_only_by_its_writer(
    runtime: Path,
) -> None:
    path = lockinfo.write_info("/dev/cu.usbmodemSECRET1", {"instrument": "KM-0"})
    assert path.parent == runtime / "kleidos-meter"
    assert "SECRET" not in path.name
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    holder = lockinfo.read_holder("/dev/cu.usbmodemSECRET1")
    assert holder is not None and holder["pid"] == os.getpid()
    assert lockinfo.describe(holder).startswith(f"kleidos-meter serve (pid {os.getpid()}, since ")
    path.write_text(json.dumps({"pid": 1}))  # another process's (launchd, init: alive)
    lockinfo.remove_info("/dev/cu.usbmodemSECRET1")
    assert path.exists()
    assert lockinfo.read_holder("/dev/cu.usbmodemSECRET1") == {"pid": 1}
    path.write_text("not json")
    assert lockinfo.read_holder("/dev/cu.usbmodemSECRET1") is None
    lockinfo.remove_info("/dev/cu.usbmodemSECRET1")  # unreadable: left alone
    path.write_text(json.dumps({"pid": "x"}))
    assert lockinfo.read_holder("/dev/cu.usbmodemSECRET1") is None and not path.exists()
    lockinfo.remove_info("/dev/cu.usbmodemSECRET1")  # nothing there


def test_a_url_is_its_own_key_and_a_holder_is_described_with_what_it_tells() -> None:
    assert lockinfo.info_path("socket://127.0.0.1:5025") != lockinfo.info_path("loop://")
    assert lockinfo.describe({"pid": 7, "panel": "http://127.0.0.1:8040/"}) == (
        "another process (pid 7, panel http://127.0.0.1:8040/)"
    )


def test_the_runtime_directory_falls_back_to_the_temporary_one_and_refuses_others(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    uid = os.getuid()
    assert lockinfo.runtime_dir() == tmp_path / f"kleidos-meter-{uid}"
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    (tmp_path / f"kleidos-meter-{uid + 1}").mkdir()
    with pytest.raises(OSError, match="belongs to another user"):
        lockinfo.runtime_dir()
    monkeypatch.undo()
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "linked").mkdir()
    (tmp_path / "linked" / "kleidos-meter").symlink_to(target)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "linked"))
    with pytest.raises(OSError, match="symbolic link"):
        lockinfo.runtime_dir()


def test_a_process_of_another_user_counts_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", denied)
    assert lockinfo._alive(12345)


def test_a_simulated_board_wired_elsewhere_is_served_as_the_wiring_says(tmp_path: Path) -> None:
    argv = ["--sim", "--sim-wiring", "sda=18", "--data-dir", str(tmp_path / "runs")]
    with serving_panel(argv) as served:
        reply = urlopen(served.url + "api/query?q=SYST:INF?", timeout=10)  # noqa: S310
        fields = json.loads(reply.read())["fields"][0]
        reply.close()
    assert fields["chip"] == "none"  # the default map's bus is not where the INA228 is
    assert serve.build_parser().parse_args(["--sim"]).sim_wiring == RIG_WIRING
