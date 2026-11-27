# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The MC/DC gate of scripts/test_report.py: an exclusion justifies only the conditions it names."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import test_report  # noqa: E402

DECISION = "if ( a > 0 || b ) {"


def coverage(tmp_path: Path, covered: list[bool]) -> dict[str, Any]:
    """LLVM's export shape, cut to what the report reads: one file, one decision."""
    source = tmp_path / "unit.cpp"
    source.write_text(f"void f() {{\n    {DECISION}\n    }}\n}}\n", encoding="utf-8")
    record = [2, 5, 2, 20, 0, 0, 0, 0, 0, covered]
    summary = {
        "lines": {},
        "branches": {},
        "mcdc": {"count": len(covered), "covered": sum(covered)},
    }
    return {
        "data": [
            {"files": [{"filename": str(source), "summary": summary, "mcdc_records": [record]}]}
        ]
    }


def entry(conditions: list[int]) -> dict[str, Any]:
    return {
        "file": "unit.cpp",
        "decision": "a > 0 || b",
        "conditions": conditions,
        "kind": "infeasible",
        "reason": "coupled",
    }


def test_the_named_condition_is_justified(tmp_path: Path) -> None:
    modules, gaps = test_report.native_coverage(coverage(tmp_path, [True, False]), [entry([2])])
    assert gaps[0].reason == "coupled"
    assert modules[0]["justified"] == 1


@pytest.mark.parametrize("conditions", [[2], []])
def test_a_further_uncovered_condition_is_a_new_gap(tmp_path: Path, conditions: list[int]) -> None:
    _, gaps = test_report.native_coverage(coverage(tmp_path, [False, False]), [entry(conditions)])
    assert gaps[0].reason is None
    assert gaps[0].note is not None and "[1, 2]" in gaps[0].note


def test_an_entry_no_gap_uses_is_reported_stale(tmp_path: Path) -> None:
    stale: list[dict[str, Any]] = []
    other = {**entry([1]), "decision": "c && d"}
    test_report.native_coverage(coverage(tmp_path, [True, False]), [entry([2]), other], stale)
    assert stale == [other]


def test_every_committed_entry_names_its_conditions() -> None:
    for item in test_report.load_exclusions():
        conditions = item.get("conditions")
        assert conditions and all(isinstance(c, int) and c >= 1 for c in conditions), item[
            "decision"
        ]
        assert item["kind"] in {"infeasible", "defensive"} and item["reason"].strip(), item[
            "decision"
        ]
