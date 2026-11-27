# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The native test runner's suites, run one by one, are the whole run (FW-30).

The 32-bit leg gives each suite a process of its own (``PER_SUITE=1``,
``firmware/test/native/Makefile``): it reads the names from ``--suites`` and
runs each with ``--suite``. A suite the listing left out would never run there,
and a name that matched nothing would pass with no case, so both are held here:
the listings of both binaries against the sources, and the suites one by one on
the smaller binary.

It needs the binaries: without them every case skips, and fails with
``METER_BRIDGE_REQUIRED=1``, which ``scripts/run_tests.sh`` sets after building
them.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

NATIVE = Path(__file__).resolve().parents[2] / "firmware" / "test" / "native"
BUILD = NATIVE / "build"
BINARIES = ("test_scpi", "test_firmware")

#: A case's registration in a test source, whose first argument is its suite.
CASE = re.compile(r'^TEST\( "([^"]+)"', re.MULTILINE)


@pytest.fixture(autouse=True)
def _built() -> None:
    if all((BUILD / binary).exists() for binary in BINARIES):
        return
    message = "no test binaries: make -C firmware/test/native test builds them"
    if os.environ.get("METER_BRIDGE_REQUIRED") == "1":
        pytest.fail(message)
    pytest.skip(message)


def run(binary: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    """*binary* run to its end with *arguments*."""
    return subprocess.run(
        [BUILD / binary, *arguments], capture_output=True, text=True, timeout=120, check=False
    )


def cases(output: str) -> list[str]:
    """The cases a run printed, passed or failed, in order."""
    return re.findall(r"^\[(?: ok |FAIL)\] (.*)$", output, re.MULTILINE)


def test_the_listings_name_every_suite_of_the_sources_once() -> None:
    listed = [name for binary in BINARIES for name in run(binary, "--suites").stdout.split("\n")]
    listed = [name for name in listed if name]
    sources = {
        suite
        for path in NATIVE.glob("test_*.cpp")
        for suite in CASE.findall(path.read_text(encoding="utf-8"))
    }
    assert set(listed) == sources
    assert len(listed) == len(sources), f"listed twice: {sorted(listed)}"
    assert [name for name in listed if not re.fullmatch(r"[\w-]+", name)] == [], (
        "the Makefile's loop splits the names at whitespace"
    )


def test_the_suites_one_by_one_are_the_whole_run() -> None:
    whole = run("test_scpi")
    assert whole.returncode == 0 and cases(whole.stdout), whole.stdout + whole.stderr
    one_by_one: list[str] = []
    for suite in run("test_scpi", "--suites").stdout.split():
        part = run("test_scpi", "--suite", suite)
        assert part.returncode == 0, part.stdout + part.stderr
        assert all(case.startswith(f"{suite}/") for case in cases(part.stdout)), part.stdout
        one_by_one += cases(part.stdout)
    assert sorted(one_by_one) == sorted(cases(whole.stdout))


def test_a_suite_that_does_not_exist_fails_the_run() -> None:
    refused = run("test_scpi", "--suite", "no-such-suite")
    assert refused.returncode == 2
    assert "no suite no-such-suite" in refused.stderr
