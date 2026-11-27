# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The host package's branch floors: one per module, and they only rise (FW-31).

``host/branch_floors.toml`` gives each module of ``host/kleidos_meter`` the
smallest share of its branch sides the tests may take, and
``scripts/test_report.py --gate`` fails a run where one takes less, a module
with no floor and a floor whose module is gone. Held here: the verdict on
planted reports, the file's shape (a floor for every module of the package, the
panel's at 90 % or more, its measured figure where that is higher), and its
history, over which no floor ever fell.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import test_report  # noqa: E402

PACKAGE = ROOT / "host" / "kleidos_meter"

#: The least a module of the panel may have.
PANEL_FLOOR = 90.0


def measured(**branches: tuple[int, int]) -> dict[str, Any]:
    """coverage.py's JSON, cut to what the gate reads: module to (covered, sides)."""
    return {
        "files": {
            f"kleidos_meter/{name}.py": {
                "summary": {
                    "covered_lines": 1,
                    "num_statements": 1,
                    "covered_branches": covered,
                    "num_branches": sides,
                }
            }
            for name, (covered, sides) in branches.items()
        },
        "totals": {"covered_lines": 1, "num_statements": 1},
    }


def floors(**values: float) -> dict[str, float]:
    """Floors by module name."""
    return {f"kleidos_meter/{name}.py": value for name, value in values.items()}


# -- The verdict ------------------------------------------------------------------


def test_a_module_at_its_floor_passes_and_one_under_it_does_not() -> None:
    assert test_report.host_floor_verdict(measured(a=(95, 100)), floors(a=95)) == ([], [])
    problems, notes = test_report.host_floor_verdict(measured(a=(94, 100)), floors(a=94.5))
    assert problems == [
        "kleidos_meter/a.py: 94.00 % of its branch sides, under its floor of 94.5 %"
    ]
    assert notes == []


def test_a_module_well_over_its_floor_is_asked_to_raise_it() -> None:
    problems, notes = test_report.host_floor_verdict(measured(a=(99, 100)), floors(a=95))
    assert problems == []
    assert notes == ["kleidos_meter/a.py: 99.00 %, over its floor of 95 %: raise it"]
    # Three sides over it is what a lost race may cost: no note.
    assert test_report.host_floor_verdict(measured(a=(98, 100)), floors(a=95)) == ([], [])


def test_a_module_without_a_branch_is_whole() -> None:
    assert test_report.host_floor_verdict(measured(a=(0, 0)), floors(a=100)) == ([], [])


def test_a_module_without_a_floor_and_a_floor_without_a_module_fail() -> None:
    problems, _ = test_report.host_floor_verdict(measured(new=(1, 1)), floors(gone=90))
    assert problems == [
        "kleidos_meter/new.py: no floor",
        "kleidos_meter/gone.py: a floor, and no such module was measured",
    ]


@pytest.mark.parametrize("value", ["'90'", "true", "-1", "100.5"])
def test_a_floor_that_is_no_percentage_is_refused(tmp_path: Path, value: str) -> None:
    path = tmp_path / "floors.toml"
    path.write_text(f'[floors]\n"kleidos_meter/a.py" = {value}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="is not a percentage"):
        test_report.load_host_floors(path)


def test_the_gate_fails_on_a_planted_module_under_its_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "host.xml").write_text(
        '<testsuite name="pytest"><testcase classname="u" name="ok"/></testsuite>',
        encoding="utf-8",
    )
    (tmp_path / "host-coverage.json").write_text(
        json.dumps(measured(a=(8, 10), b=(10, 10))), encoding="utf-8"
    )
    path = tmp_path / "floors.toml"
    monkeypatch.setattr(test_report, "HOST_FLOORS", path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    path.write_text(
        '[floors]\n"kleidos_meter/a.py" = 80\n"kleidos_meter/b.py" = 100\n', encoding="utf-8"
    )
    assert test_report.main(["--dir", str(tmp_path), "--gate"]) == 0
    assert "| `a.py` | 100.0 % | 80.0 % | 80 % |" in (tmp_path / "report.md").read_text("utf-8")
    path.write_text('[floors]\n"kleidos_meter/a.py" = 80.1\n', encoding="utf-8")
    assert test_report.main(["--dir", str(tmp_path), "--gate"]) == 1
    err = capsys.readouterr().err
    assert "HOST FLOOR: kleidos_meter/a.py: 80.00 %" in err
    assert "HOST FLOOR: kleidos_meter/b.py: no floor" in err


# -- The file ---------------------------------------------------------------------


def test_every_module_of_the_package_has_a_floor_and_nothing_else_has() -> None:
    modules = {
        path.relative_to(ROOT / "host").as_posix()
        for path in PACKAGE.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    assert set(test_report.load_host_floors()) == modules


def test_the_panels_floors_are_ninety_or_more() -> None:
    panel = {m: f for m, f in test_report.load_host_floors().items() if "/panel/" in m}
    assert len(panel) >= 15
    assert {m: f for m, f in panel.items() if f < PANEL_FLOOR} == {}


def _load_bench_support() -> ModuleType:
    path = ROOT / "tests" / "bench" / "bench_support.py"
    spec = importlib.util.spec_from_file_location("bench_support_for_floors", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_support_for_floors"] = module
    spec.loader.exec_module(module)
    return module


def fallen(old: dict[str, float], new: dict[str, float]) -> list[str]:
    """The modules whose floor is lower in *new* than in *old*; a module gone has none."""
    return [
        f"{module}: {old[module]:g} -> {new[module]:g}"
        for module in sorted(old.keys() & new.keys())
        if new[module] < old[module]
    ]


def test_fallen_finds_a_lowered_floor() -> None:
    assert fallen({"a": 90, "b": 95, "gone": 99}, {"a": 91, "b": 94.5, "new": 1}) == [
        "b: 95 -> 94.5"
    ]


def test_the_floors_only_rise_over_the_files_history(tmp_path: Path) -> None:
    current = test_report.load_host_floors()
    lowered = []
    for commit, text in _load_bench_support().committed_versions(test_report.HOST_FLOORS):
        version = tmp_path / "floors.toml"
        version.write_text(text, encoding="utf-8")
        lowered += [
            f"{commit[:10]}: {fall}"
            for fall in fallen(test_report.load_host_floors(version), current)
        ]
    assert not lowered, "a floor only rises:\n" + "\n".join(lowered)
