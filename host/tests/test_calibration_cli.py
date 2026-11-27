# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""``kleidos-meter calibration verify|restore`` against the simulated instrument."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from kleidos_meter import Meter, SimulatedMeter
from kleidos_meter.calibration_cli import load_index, main, restore, verify
from kleidos_meter.cli import main as cli_main
from kleidos_meter.registry import Registry, build_index, dump_index
from test_registry import accepted, add_events

DAY = dt.date(2026, 11, 5)


@pytest.fixture
def index(registry_root: Path) -> dict[str, Any]:
    add_events(registry_root, accepted(registry_root, "2026-11-02-01"))
    return build_index(Registry.from_directory(registry_root))


def test_a_flashed_instrument_is_restored_from_the_registry(index: dict[str, Any]) -> None:
    sim = SimulatedMeter()  # NVS erased: the defaults, nothing measured
    meter = Meter(sim)
    assert verify(meter, index, DAY).status == "working_copy_lost"
    said: list[str] = []
    after = restore(meter, index, DAY, ask=lambda prompt: "y", say=said.append)
    assert after.status == "applied", after.differences
    assert sim.shunt_ohm == pytest.approx(0.100116)
    assert sim.tag == "R100_20261120_HR" and sim.cal_date == (2026, 11, 2)
    assert sim.vbus_side == "source"
    assert not any("ZERO" in m for m in sim.messages)  # a null is never restored
    assert any("Will send" in line for line in said)
    # Nothing left to do the second time.
    said.clear()
    assert restore(meter, index, DAY, ask=lambda prompt: "y", say=said.append).ok
    assert any("Nothing to restore" in line for line in said)


def test_a_restore_declined_or_refused_writes_nothing(index: dict[str, Any]) -> None:
    sim = SimulatedMeter()
    meter = Meter(sim)
    assert restore(meter, index, DAY, ask=lambda prompt: "n", say=lambda t: None).status == (
        "working_copy_lost"
    )
    assert not sim.shunt_measured
    sim.bypass_part = "hw316"
    said: list[str] = []
    verdict = restore(meter, index, DAY, ask=lambda prompt: "y", say=said.append)
    assert verdict.status == "configuration_mismatch" and not sim.shunt_measured
    assert any("Not restored" in line for line in said)


def test_the_command_line(
    registry_root: Path, index: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    (registry_root / "index.json").write_text(dump_index(index))
    common = ["--sim", "--registry", str(registry_root), "--date", DAY.isoformat()]
    assert main(["verify", *common]) == 3
    assert "working_copy_lost" in capsys.readouterr().out
    assert main(["verify", "--json", *common]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "working_copy_lost"
    # Each run is a fresh simulator, so a restore ends applied.
    assert main(["restore", "--yes", *common]) == 0
    assert cli_main(["calibration", "restore", "--yes", *common]) == 0
    with pytest.raises(SystemExit):
        main(["verify"])


def test_the_index_at_a_revision(registry_root: Path, index: dict[str, Any]) -> None:
    (registry_root / "index.json").write_text(dump_index(index))
    repository = registry_root.parent
    git = ["git", "-C", str(repository), "-c", "user.email=", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (repository / ".gitignore").write_text("!*.log\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "--no-gpg-sign", "-m", "fixture"], check=True)
    assert load_index(registry_root, "HEAD") == index
    with pytest.raises(SystemExit, match="no calibration/index.json"):
        load_index(registry_root, "no-such-rev")


def test_a_restore_with_no_terminal_and_no_yes_is_a_usage_error(
    registry_root: Path,
    index: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io

    from kleidos_meter import calibration_cli

    (registry_root / "index.json").write_text(dump_index(index))
    common = ["--sim", "--registry", str(registry_root), "--date", DAY.isoformat()]
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert main(["restore", *common]) == 2
    assert "needs a terminal; pass --yes" in capsys.readouterr().err

    def end_of_input(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", end_of_input)
    assert calibration_cli.ask_terminal("? ") == ""
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert calibration_cli.ask_terminal("? ") == "y"
