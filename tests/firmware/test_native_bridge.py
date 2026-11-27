# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native bridge answers as the instrument does (FW-30).

``make -C firmware/test/native bridge`` builds ``bridge.cpp``: the firmware,
booted on the harness's board, whose UART0 is the process's standard input and
output. The host client's conformance suite talks to it, so what that suite
takes for granted is held here: a message in pieces, a block, a capture that
streams at its profile's rate, an input that ends with a message in flight, and
an exit status that tells a clean run.

It needs the binary: without it every case skips, and fails with
``METER_BRIDGE_REQUIRED=1``, which ``scripts/run_tests.sh`` sets after building
it.
"""

from __future__ import annotations

import os
import select
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "firmware" / "test" / "native" / "build" / "bridge"

#: The environment variable that makes a missing bridge a failure.
REQUIRED = "METER_BRIDGE_REQUIRED"


@pytest.fixture(autouse=True)
def _built() -> None:
    if BRIDGE.exists():
        return
    message = "no bridge: make -C firmware/test/native bridge builds it"
    if os.environ.get(REQUIRED) == "1":
        pytest.fail(message)
    pytest.skip(message)


def run(messages: bytes) -> subprocess.CompletedProcess[bytes]:
    """The bridge given *messages* and the end of its input at once."""
    return subprocess.run([BRIDGE], input=messages, capture_output=True, timeout=60, check=False)


class Session:
    """A bridge left running, its pipes unbuffered."""

    def __init__(self) -> None:
        #: When it was started, before any of its virtual time had run.
        self.started = time.monotonic()
        self.process = subprocess.Popen(
            [BRIDGE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0
        )

    def send(self, data: bytes) -> None:
        """Write *data* as it is."""
        assert self.process.stdin is not None
        self.process.stdin.write(data)

    def read(self, seconds: float) -> bytes:
        """What it writes in the next *seconds*."""
        assert self.process.stdout is not None
        out = b""
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0:
            if select.select([self.process.stdout], [], [], left)[0]:
                out += os.read(self.process.stdout.fileno(), 65536)
        return out

    def line(self, seconds: float = 20.0) -> bytes:
        """The next line it writes, waiting at most *seconds*."""
        assert self.process.stdout is not None
        out = b""
        deadline = time.monotonic() + seconds
        while not out.endswith(b"\n"):
            left = deadline - time.monotonic()
            assert left > 0, f"no line in {seconds} s, only {out!r}"
            if select.select([self.process.stdout], [], [], left)[0]:
                out += os.read(self.process.stdout.fileno(), 1)
        return out

    def end(self) -> int:
        """Close its input and wait for it; its exit status."""
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.close()
        status = self.process.wait(30)
        self.process.stdout.close()
        return status


@pytest.fixture
def session() -> Iterator[Session]:
    """A running bridge, killed if the case leaves it so."""
    opened = Session()
    yield opened
    if opened.process.poll() is None:
        opened.process.kill()
        opened.end()


def test_it_answers_its_identity_and_ends_with_its_input() -> None:
    done = run(b"*IDN?\n")
    version = (ROOT / "firmware" / "version.txt").read_text(encoding="utf-8").strip()
    assert done.stdout == f"KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,{version}\n".encode()
    assert (done.returncode, done.stderr) == (0, b"")


def test_nothing_is_written_before_it_is_asked() -> None:
    done = run(b"")
    assert (done.stdout, done.stderr, done.returncode) == (b"", b"", 0)


def test_a_message_in_flight_when_the_input_ends_is_answered() -> None:
    done = run(b"MEAS:CURR?;:SYST:ERR:ALL?\n*OPC?\n")
    assert done.stdout == b'0.05000625;0,"No error"\n1\n'


def test_it_is_a_fresh_instrument_with_the_devices_ring() -> None:
    answers = run(b"*ESR?;:SYST:ERR:COUN?;:TRAC:POIN:MAX?;:SYST:STAT?\n").stdout.decode()
    assert answers.startswith("128;0;1048576;running=0 sink=idle profile=TRACE ")
    assert " q=uncal " in answers


def test_a_long_line_and_a_burst_are_the_consoles_to_judge() -> None:
    assert run(b"X" * 300 + b"\nSYST:ERR?\n").stdout.startswith(b"-363,")
    # 200 messages at once: none is lost on the way, as none is on a line
    # whose host waits for no answer at the console's pace.
    assert run(b"*OPC?\n" * 200).stdout == b"1\n" * 200


def test_a_message_in_pieces_is_one_message(session: Session) -> None:
    session.send(b"*ID")
    assert session.read(0.2) == b""
    session.send(b"N?\n")
    assert session.line().startswith(b"KLEIDOS,KLEIDOS-METER,")
    assert session.end() == 0


def test_a_capture_streams_at_its_profiles_rate(session: Session) -> None:
    session.send(b"SENS:PROF STATE;:SENS:FUNC:ON CURR;:FORM CSV;:SYST:ERR:ALL?\n")
    assert session.line() == b'0,"No error"\n'
    session.send(b"INIT\n")
    session.read(2.0)
    session.send(b"SYST:STAT?\n")
    status = b""
    while b"samples=" not in status:
        status = session.line()
    captured = int(status.split(b" samples=")[1].split()[0]) * 1152e-6
    # Its virtual time never runs ahead of this machine's, and a machine too
    # busy to keep up may leave it a little behind or lose some of it.
    assert 0.2 <= captured <= time.monotonic() - session.started + 0.05
    session.send(b"ABOR;:SYST:ERR:ALL?\n")
    while not status.startswith(b"0,"):
        status = session.line()
    assert session.end() == 0


def test_a_block_comes_byte_for_byte(session: Session) -> None:
    session.send(b"SENS:PROF TRAC;:SENS:CAPT:LIM OFF;:INIT\n")
    session.read(0.3)
    session.send(b"ABOR;:TRAC:DATA? RAW,0,32;:SYST:ERR:ALL?\n")
    head = session.line()
    assert head.startswith(b"#3"), head
    size = int(head[2:5])
    got = head[5:]
    while len(got) < size:
        got += session.line()
    assert got[:size].splitlines()[-1].startswith(b"METER_DUMP_END crc32=")
    assert got[size:] + session.line() == b';0,"No error"\n'
    assert session.end() == 0


def test_an_argument_is_refused() -> None:
    done = subprocess.run([BRIDGE, "x"], capture_output=True, timeout=60, check=False)
    assert done.returncode == 2
    assert done.stderr.startswith(b"usage:")


def test_it_ends_when_nobody_reads_it() -> None:
    process = subprocess.Popen([BRIDGE], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
    assert process.stdin is not None and process.stdout is not None
    process.stdout.close()
    process.stdin.write(b"*IDN?\n")
    assert process.wait(30) == 0
    process.stdin.close()
