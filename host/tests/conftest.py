# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""A registry of the tests' own: the real schemas and configuration, a simulated instrument.

Also the bench options, off by default: ``--run-bench`` runs the tests marked
``bench``, which talk to the meter on the bench (``--meter-port``, default
``KLEIDOS_METER_PORT``) under the bench lock (``--bench-lock``, default
``KLEIDOS_METER_LOCK``); without it they skip and say so. The names are the
repository's own bench suite's (``tests/conftest.py`` at the root).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, NoReturn

import pytest
from kleidos_meter.registry import ROOT
from kleidos_meter.sim_table import Row


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("bench", "the meter on the bench")
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
        "--bench-shots",
        default=None,
        help="a directory for the panel's bench images (Live, Pins, Calibration and health),"
        " each named by the date and the image's elf=; none by default",
    )


def _serial_on_the_bench(config: pytest.Config) -> None:
    """Make a ``--run-bench`` run serial: the meter's port and the bench lock are one.

    The default is parallel (``-n auto`` in the pytest configuration); a run that
    talks to the instrument must not be split across processes, whatever ``-n``
    said, so xdist is told to stand down before it starts its workers.
    """
    if config.getoption("--run-bench") and getattr(config.option, "dist", "no") != "no":
        config.option.dist = "no"
        config.option.numprocesses = None
        config.option.tx = []


#: The tests a run left out of those it collected (``-k``, ``-m``, ``--lf``), for
#: a gate that judges only a whole module's run (``js_coverage.whole_run``).
DESELECTED: pytest.StashKey[list[pytest.Item]] = pytest.StashKey()


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    _serial_on_the_bench(config)
    config.addinivalue_line("markers", "bench: talks to the meter on the bench (--run-bench)")
    # The bench cases are given the port by the option, which read it above.
    # No test may find it in the environment: a command line run without --sim
    # would open the instrument, with no lock held (it did, on 2026-11-24,
    # from a shell that had exported it).
    os.environ.pop("KLEIDOS_METER_PORT", None)


def pytest_deselected(items: Sequence[pytest.Item]) -> None:
    for item in items:
        item.config.stash.setdefault(DESELECTED, []).append(item)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-bench"):
        return
    skip = pytest.mark.skip(reason="talks to the meter on the bench: give --run-bench")
    for item in items:
        if item.get_closest_marker("bench") is not None:
            item.add_marker(skip)


#: What makes a missing native bridge a failure (``scripts/run_tests.sh``, after it
#: builds it), and a missing browser one (CI's panel job).
BRIDGE_REQUIRED = "METER_BRIDGE_REQUIRED"
BROWSER_REQUIRED = "METER_BROWSER_REQUIRED"


def missing(message: str, required: str) -> NoReturn:
    """Skip for *message*, or fail with it when the variable *required* is ``1``."""
    if os.environ.get(required) == "1":
        pytest.fail(f"{message} ({required}=1, so a skip is a failure)", pytrace=False)
    pytest.skip(message)


#: The repository's one reader of the firmware's command table, whose rows the
#: simulator and the panel's schema follow.
SCPI_TABLE = Path(__file__).resolve().parents[2] / "scripts" / "scpi_table.py"


def scpi_table() -> ModuleType:
    """``scripts/scpi_table.py`` as a module, loaded once and from its file.

    It is not installed with the package and ``scripts/`` is not a package, so
    the file is loaded by its path and no directory is added to ``sys.path``.
    """
    name = "meter_scpi_table"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, SCPI_TABLE)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # a dataclass looks its own module up
        spec.loader.exec_module(module)
    return sys.modules[name]


def firmware_commands() -> list[Row]:
    """``kCommands`` as (pattern, flag letters, most parameters), in the firmware's order.

    The census build's queries are not among them: no other image answers them.
    """
    return [(entry.pattern, entry.flags, entry.most) for entry in scpi_table().commands()]


#: The simulator's serial: the digest of its made-up MAC (sim.py, identity.h).
SIM_SERIAL = "KM-EE529C1B03"


class FakeClock:
    """A monotonic clock that moves only when told to, or when slept on."""

    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        """Let *seconds* pass at once."""
        self.now += seconds


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def build_registry(parent: Path) -> Path:
    """A ``calibration/`` under *parent* with the real schemas and the Relay 2 Click configuration.

    The simulator's rig: the one image drives the Click (0.8.0 and later).
    The simulator is filed as built, with no calibration yet.
    """
    root = parent / "calibration"
    shutil.copytree(ROOT / "schemas", root / "schemas")
    (root / "configurations").mkdir(parents=True)
    for name in ("mjkdz-r100-relay2click.json", "mjkdz-r100-hw316.json"):
        shutil.copy2(ROOT / "configurations" / name, root / "configurations" / name)
    write_json(
        root / "instruments" / f"{SIM_SERIAL}.json",
        {
            "schema": "kleidos.meter.instrument/1",
            "serial": SIM_SERIAL,
            "label": "simulated",
            "events": [
                {
                    "id": "2026-11-01-00",
                    "kind": "hardware_change",
                    "configuration": "mjkdz-r100-relay2click",
                    "voids_calibration": True,
                    "what": "built",
                }
            ],
        },
    )
    return root


@pytest.fixture
def registry_root(tmp_path: Path) -> Path:
    """:func:`build_registry` in the test's own directory."""
    return build_registry(tmp_path)


@pytest.fixture(autouse=True)
def no_serial_aliases(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """No alias from the shell or the repository's ``.env``: set empty, the variable wins.

    A ``--run-bench`` run keeps the bench's own: the meter is filed under the
    placeholder, and only its alias finds it (``calibration/README.md``).
    """
    if not request.config.getoption("--run-bench"):
        monkeypatch.setenv("KLEIDOS_METER_SERIAL_ALIASES", "")


@pytest.fixture(autouse=True)
def state_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A state directory of the test's own, so no run is ever recorded in the user's."""
    directory = tmp_path_factory.mktemp("state")
    monkeypatch.setenv("XDG_STATE_HOME", str(directory))
    return directory
