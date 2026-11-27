# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The console's own UART0 driver on the meter (FW-12).

UART0 is set and served on its registers (``firmware/src/uart0.h``,
``docs/architecture.md`` §4.2): a receive interrupt routed with nothing
allocated fills a static ring, and the transmit is polled. On the meter:

* ``*IDN?`` 1,000 times, each answered within its timeout;
* a 60 s ``STATE`` stream with every record on the line and ``dropped=0``;
* input sent while a dump holds the console overflows the FIFO and answers
  one ``-363`` for the message it fell in, and the console then answers;
* with ``--bench-long``, a full ``TRACE`` ring dumped with a correct CRC-32.

    pytest tests/bench/test_uart_bench.py --run-bench [--bench-long] \\
        --meter-port "$KLEIDOS_METER_PORT" --bench-lock <lock file>
"""

from __future__ import annotations

import time

import pytest
from bench_support import BenchLine
from kleidos_meter.client import Meter

#: The ``*IDN?`` round trips, and the longest any one may take.
IDN_ROUNDS = 1000
IDN_TIMEOUT_S = 1.0

#: How long the stream runs, and the ``STATE`` rate it is held to (the
#: profile's 868 samples a second, the INA228's oscillator a little off).
STREAM_S = 60.0
STATE_RATE = 868.0

#: The bytes sent while a dump runs: well past the ring's 1,024 and the
#: FIFO's 128, so the FIFO overflows.
BURST_BYTES = 4096

#: The overrun's error, as the console words it.
OVERRUN = '-363,"Input buffer overrun;receive overrun: input lost while the console was busy'

#: The TRACE ring's samples and the time it takes to fill at 280 us.
RING_SAMPLES = 1_048_576
RING_FILL_S = 300.0


#: How long the line must stay silent for a stream to count as ended.
QUIET_S = 0.3


def drain_until_quiet(line: BenchLine, quiet: float = QUIET_S, limit: float = 20.0) -> bytes:
    """What *line* receives until it has been silent for *quiet* seconds.

    :param limit: The longest it reads in all, so that a stream that never
        stops fails the test rather than hanging it.
    """
    out = bytearray()
    silent_since = time.monotonic()
    deadline = silent_since + limit
    while time.monotonic() - silent_since < quiet:
        assert time.monotonic() < deadline, "the line never fell quiet"
        chunk = line.read(65536)
        if chunk:
            out += chunk
            silent_since = time.monotonic()
    return bytes(out)


@pytest.mark.bench
def test_idn_answers_a_thousand_times_within_its_timeout(meter: Meter) -> None:
    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    first = meter.idn()
    assert first.startswith("KLEIDOS,KLEIDOS-METER,")
    for _ in range(IDN_ROUNDS):
        assert meter.send("*IDN?", timeout=IDN_TIMEOUT_S).units == first.split(";")
    assert meter.query("SYST:ERR:COUN?") == "0"


@pytest.mark.bench
def test_a_60_s_state_stream_reaches_the_line_whole(meter: Meter, meter_line: BenchLine) -> None:
    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    meter.exchange(["*RST", "FORM TEXT", "SENS:PROF STAT"], timeout=20.0)
    meter_line.write(b"INIT\n")
    received = bytearray()
    end = time.monotonic() + STREAM_S
    while time.monotonic() < end:
        received += meter_line.read(65536)
    # The window ends anywhere in a record. The stream's tail is read here,
    # raw, until the line falls quiet after ABORt: handed to the client, the
    # rest of a record cut in two is not a record and reads as the answer to
    # the next message, which leaves the session's client one answer behind
    # (the cascade of 2026-11-23, FW-18's first bench run).
    meter_line.write(b"ABOR\n")
    received += drain_until_quiet(meter_line)
    assert meter.query("SYST:ERR:COUN?") == "0"
    status = meter.status()
    assert status["dropped"] == "0", status
    lines = bytes(received).split(b"\n")[1:-1]  # the first and last may be cut by the window
    records = [line for line in lines if line.startswith(b"METER ")]
    assert len(records) == len(lines), "a line on the stream that is not a record"
    assert all(line.isascii() and b" t=" in line and b" v=" in line for line in records)
    assert len(records) >= STATE_RATE * STREAM_S * 0.97, f"{len(records)} records in 60 s"


@pytest.mark.bench
def test_input_sent_during_a_dump_overflows_and_answers_one_overrun(
    meter: Meter, meter_line: BenchLine
) -> None:
    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    meter.exchange(["*RST", "FORM TEXT", "SENS:PROF TRAC", "INIT"], timeout=20.0)
    time.sleep(10.0)  # about 35,700 samples
    meter.exchange(["ABOR", "*CLS"], timeout=20.0)
    # The dump holds the console for seconds, reading nothing from the line;
    # the burst arrives in its first second.
    meter_line.write(b"TRAC:DATA? SCAL\n")
    time.sleep(0.3)
    meter_line.write(b"x" * BURST_BYTES + b"\n")
    response = meter.send("", timeout=120.0)  # an empty line, then the dump's response
    assert response.block is not None and b"METER_DUMP_END" in response.block
    errors = meter.send("SYST:ERR:ALL?", timeout=20.0).units
    assert len(errors) == 1 and errors[0].startswith(OVERRUN), errors
    assert meter.idn().startswith("KLEIDOS,KLEIDOS-METER,")
    assert meter.query("SYST:ERR:COUN?") == "0"


@pytest.mark.bench
@pytest.mark.bench_long
def test_a_full_trace_ring_dumps_with_its_crc(meter: Meter) -> None:
    from kleidos_meter.protocol import check_dump

    meter.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    meter.exchange(["*RST", "FORM TEXT", "SENS:PROF TRAC", "INIT"], timeout=20.0)
    started = time.monotonic()
    while int(meter.query("TRAC:POIN?")) < RING_SAMPLES:
        assert time.monotonic() - started < RING_FILL_S + 60.0, "the ring did not fill"
        time.sleep(15.0)
    meter.exchange(["ABOR"], timeout=20.0)
    response = meter.send("TRAC:DATA? RAW", timeout=600.0)
    assert response.block is not None
    assert check_dump(response.block) is True
    assert meter.query("SYST:ERR:COUN?") == "0"
    meter.exchange(["*RST"], raise_errors=False, timeout=20.0)
