# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The options every run under tests/ knows: the bench suite's, off by default.

``tests/bench`` holds tests that talk to the meter on the bench. They are
marked ``bench`` and skipped unless ``--run-bench`` is given, so a run of
every guard (``scripts/run_tests.sh``, CI) never reaches for hardware; the
options live here, at the root of the tests, because pytest reads options
only from the conftest files it loads before collecting.
"""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("bench", "the meter on the bench (tests/bench)")
    group.addoption(
        "--run-bench",
        action="store_true",
        default=False,
        help="run the tests marked bench, which talk to the meter",
    )
    group.addoption(
        "--meter-port",
        default=os.environ.get("KLEIDOS_METER_PORT"),
        help="the meter's CH343 port (default: KLEIDOS_METER_PORT)",
    )
    group.addoption(
        "--bench-lock",
        default=os.environ.get("KLEIDOS_METER_LOCK"),
        help="the bench lock file to hold while the meter is used (default: KLEIDOS_METER_LOCK)",
    )
    group.addoption(
        "--bench-long",
        action="store_true",
        default=False,
        help="also run the bench cases that take minutes (a 600 s idle, a full TRACE ring)",
    )
    group.addoption(
        "--bench-record",
        default=None,
        help="write what the heap sweep measured to this JSON file",
    )
    group.addoption(
        "--bench-session-record",
        default=None,
        help="write what the heap session of one boot measured to this JSON file",
    )


def _serial_on_the_bench(config: pytest.Config) -> None:
    """Make a ``--run-bench`` run serial: the meter's port and the bench lock are one.

    The default is parallel (``-n auto`` in ``tests/pytest.ini``); a run that
    talks to the instrument must not be split across processes, whatever ``-n``
    said, so xdist is told to stand down before it starts its workers.
    """
    if config.getoption("--run-bench") and getattr(config.option, "dist", "no") != "no":
        config.option.dist = "no"
        config.option.numprocesses = None
        config.option.tx = []


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    _serial_on_the_bench(config)
    config.addinivalue_line("markers", "bench: talks to the meter on the bench (--run-bench)")
    config.addinivalue_line(
        "markers", "bench_long: a bench case that takes minutes (--run-bench --bench-long)"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-bench"):
        if config.getoption("--bench-long"):
            return
        long = pytest.mark.skip(reason="takes minutes on the bench: give --bench-long")
        for item in items:
            if item.get_closest_marker("bench_long") is not None:
                item.add_marker(long)
        return
    skip = pytest.mark.skip(reason="talks to the meter on the bench: give --run-bench")
    for item in items:
        if item.get_closest_marker("bench") is not None:
            item.add_marker(skip)
