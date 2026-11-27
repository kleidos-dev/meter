# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The bench suite's fixtures: the bench lock, then the meter's line.

Every test that talks to the meter is marked ``bench`` and asks for
:func:`meter`, which first takes the bench lock (:class:`bench_support.BenchLock`,
the file ``--bench-lock`` or ``KLEIDOS_METER_LOCK`` names; a caller that
already holds it for this run names its process in ``KLEIDOS_METER_LOCK_PID``),
then opens the port ``--meter-port`` or ``KLEIDOS_METER_PORT`` names, and
after the last test puts the instrument back to rest (``*RST``: the relays on
the measuring circuit, ``FORMat TEXT``) before it lets the line and the lock
go. Neither the port nor the lock has a default: the suite refuses to guess
which device is the meter or whose lock it is.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from bench_support import BenchLine, BenchLock
from kleidos_meter.client import Meter


@pytest.fixture(scope="session")
def bench_lock(request: pytest.FixtureRequest) -> Iterator[BenchLock]:
    """The bench lock, held for the session."""
    path = request.config.getoption("--bench-lock")
    if not path:
        pytest.fail("name the bench lock with --bench-lock or KLEIDOS_METER_LOCK")
    declared = os.environ.get("KLEIDOS_METER_LOCK_PID")
    lock = BenchLock(Path(path), declared=int(declared) if declared else None)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


@pytest.fixture(scope="session")
def meter_line(request: pytest.FixtureRequest, bench_lock: BenchLock) -> Iterator[BenchLine]:
    """The meter's line, opened under the lock and closed before it is let go."""
    del bench_lock  # held for as long as the line is open
    port = request.config.getoption("--meter-port")
    if not port:
        pytest.fail("name the meter's port with --meter-port or KLEIDOS_METER_PORT")
    line = BenchLine(port)
    try:
        yield line
    finally:
        line.close()


@pytest.fixture(scope="session")
def meter(meter_line: BenchLine) -> Iterator[Meter]:
    """The instrument, put back to rest after the session."""
    instrument = Meter(meter_line)
    instrument.wait_ready()
    yield instrument
    instrument.exchange(["ABOR"], raise_errors=False, timeout=20.0)
    instrument.exchange(["*RST", "FORM TEXT"], timeout=20.0)
