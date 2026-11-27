# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The decision-coverage gate of scripts/test_report.py: a ratchet over untaken branch sides.

A planted untaken branch fails the gate unless a reason names its side or
the baseline allows it; a branch exported once per binary counts as taken
when any binary took it; the baseline docs/testing.md states is the one the
gate reads; and a coverage export that LLVM's tools warned about (or one with
no log to say they did not) fails the gate too.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import test_report  # noqa: E402

BRANCH = "if ( ready ) {"


def export(tmp_path: Path, *records: list[int]) -> dict[str, Any]:
    """LLVM's export shape, cut to what the gate reads: one file, its branch regions."""
    source = tmp_path / "unit.cpp"
    source.write_text(f"void f() {{\n    {BRANCH}\n    }}\n}}\n", encoding="utf-8")
    return {
        "data": [
            {
                "files": [
                    {
                        "filename": str(source),
                        "summary": {
                            "lines": {"count": 1, "covered": 1},
                            "branches": {"count": 2, "covered": 1},
                            "mcdc": {"count": 0, "covered": 0},
                        },
                        "branches": [list(r) for r in records],
                    }
                ]
            }
        ]
    }


def region(taken: int, not_taken: int) -> list[int]:
    return [2, 5, 2, 14, taken, not_taken, 0, 0, 4]


def reason(sides: list[str]) -> dict[str, Any]:
    return {
        "file": "unit.cpp",
        "decision": BRANCH,
        "sides": sides,
        "kind": "defensive",
        "reason": "never false",
    }


def test_an_untaken_side_is_a_gap_and_a_taken_one_is_not(tmp_path: Path) -> None:
    gaps = test_report.native_branches(export(tmp_path, region(3, 0)), [])
    assert [(g.line, g.side, g.reason) for g in gaps] == [(2, "false", None)]
    assert test_report.native_branches(export(tmp_path, region(3, 1)), []) == []


def test_a_region_one_binary_took_is_taken(tmp_path: Path) -> None:
    # The same region exported by two binaries: each took a different side.
    gaps = test_report.native_branches(export(tmp_path, region(3, 0), region(0, 2)), [])
    assert gaps == []


@pytest.mark.parametrize(("condition", "sides"), [("true", []), ("busy", ["false"])])
def test_a_constant_condition_is_no_branch(
    tmp_path: Path, condition: str, sides: list[str]
) -> None:
    """A task's ``while ( true )`` has no false side; a condition that can fall has one."""
    data = export(tmp_path)
    source = Path(data["data"][0]["files"][0]["filename"])
    source.write_text(f"void f() {{\n    while ( {condition} ) {{\n    }}\n}}\n", "utf-8")
    data["data"][0]["files"][0]["branches"] = [[2, 13, 2, 17, 5, 0, 0, 0, 4]]
    gaps = test_report.native_branches(data, [])
    assert [(g.side, g.operand) for g in gaps] == [(side, condition) for side in sides]


@pytest.mark.parametrize(("sides", "justified"), [(["false"], True), (["true"], False)])
def test_a_reason_covers_only_the_sides_it_names(
    tmp_path: Path, sides: list[str], justified: bool
) -> None:
    stale: list[dict[str, Any]] = []
    gaps = test_report.native_branches(export(tmp_path, region(3, 0)), [reason(sides)], stale)
    assert (gaps[0].reason is not None) is justified
    assert (stale == []) is justified


def test_the_ratchet_fails_over_its_baseline_and_asks_to_fall_under_it(tmp_path: Path) -> None:
    gaps = test_report.native_branches(export(tmp_path, region(0, 0)), [])
    assert len(gaps) == 2
    assert test_report.branch_verdict(gaps, 1)[0] is False
    holds, sentence = test_report.branch_verdict(gaps, 2)
    assert holds and "the baseline" in sentence
    holds, sentence = test_report.branch_verdict(gaps, 5)
    assert holds and "lower it to 2" in sentence


def test_the_gate_fails_on_a_planted_untaken_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report"
    (report / "native").mkdir(parents=True)
    (report / "native" / "unit.xml").write_text(
        '<testsuite name="unit"><testcase classname="u" name="ok"/></testsuite>', encoding="utf-8"
    )
    (report / "coverage.json").write_text(json.dumps(export(tmp_path, region(3, 0))), "utf-8")
    (report / test_report.COVERAGE_LOG).write_text("", encoding="utf-8")
    policy = tmp_path / "branch_exclusions.toml"
    monkeypatch.setattr(test_report, "BRANCH_EXCLUSIONS", policy)
    monkeypatch.setattr(test_report, "EXCLUSIONS", tmp_path / "none.toml")
    policy.write_text("baseline = 0\n", encoding="utf-8")
    assert test_report.main(["--dir", str(report), "--gate"]) == 1
    policy.write_text("baseline = 1\n", encoding="utf-8")
    assert test_report.main(["--dir", str(report), "--gate"]) == 0
    policy.write_text(
        'baseline = 0\n[[exclusion]]\nfile = "unit.cpp"\ndecision = "if ( ready ) {"\n'
        'sides = ["false"]\nkind = "defensive"\nreason = "never false"\n',
        encoding="utf-8",
    )
    assert test_report.main(["--dir", str(report), "--gate"]) == 0


def test_every_committed_reason_names_its_sides() -> None:
    for item in test_report.load_branch_policy().exclusions:
        assert item.get("sides") and set(item["sides"]) <= {"true", "false"}, item["decision"]
        assert item["kind"] in {"infeasible", "defensive"} and item["reason"].strip(), item[
            "decision"
        ]
        assert set(item) <= {"file", "decision", "condition", "sides", "kind", "reason"}, item


def two_operands(tmp_path: Path, left: tuple[int, int], right: tuple[int, int]) -> dict[str, Any]:
    """One line, ``if ( a && b ) {``, whose two operands LLVM exports as two regions."""
    data = export(tmp_path)
    source = Path(data["data"][0]["files"][0]["filename"])
    source.write_text("void f() {\n    if ( a && b ) {\n    }\n}\n", encoding="utf-8")
    # Columns are 1-based and the end is one past: `a` is column 10, `b` 15.
    data["data"][0]["files"][0]["branches"] = [
        [2, 10, 2, 11, *left, 0, 0, 4],
        [2, 15, 2, 16, *right, 0, 0, 4],
    ]
    return data


def test_a_condition_narrows_a_reason_to_its_operand(tmp_path: Path) -> None:
    """An entry naming `b` justifies b's false side and leaves a's own gap standing."""
    data = two_operands(tmp_path, (3, 0), (2, 0))
    entry = {
        "file": "unit.cpp",
        "decision": "if ( a && b ) {",
        "condition": "b",
        "sides": ["false"],
        "kind": "infeasible",
        "reason": "b is never false",
    }
    gaps = test_report.native_branches(data, [entry])
    assert [(g.operand, g.side, g.reason is not None) for g in gaps] == [
        ("a", "false", False),
        ("b", "false", True),
    ]
    # Without the condition the same entry covers both operands' false sides.
    del entry["condition"]
    gaps = test_report.native_branches(data, [entry])
    assert all(g.reason is not None for g in gaps)


def test_region_text_spans_lines() -> None:
    source = ["    if ( first &&", "         second ) {"]
    assert test_report.region_text(source, (1, 10, 2, 16)) == "first && second"
    assert test_report.region_text(source, (1, 10, 1, 15)) == "first"
    assert test_report.region_text(source, (3, 1, 3, 2)) == ""


def test_every_committed_reason_matches_a_current_line() -> None:
    """Each entry's decision is on a line of its file, and its condition is part of it.

    A reason whose code moved away would otherwise wait, unmatched, for a line
    that happens to read the same: the gate only notes an entry no gap used.
    """
    sources = ROOT / "firmware" / "src"
    for item in test_report.load_branch_policy().exclusions:
        path = sources / item["file"]
        assert path.is_file(), f"{item['file']}: no such file in firmware/src"
        decision = test_report.normalize(item["decision"])
        text = path.read_text(encoding="utf-8")
        lines = [test_report.normalize(line) for line in text.splitlines()]
        assert any(decision in line for line in lines), f"{item['file']}: {item['decision']}"
        condition = test_report.normalize(item.get("condition", ""))
        assert condition in decision, f"{item['file']}: {item.get('condition')} not in its decision"


def test_no_committed_reason_shadows_another() -> None:
    """One entry per decision (or operand): a second for the same side would never be read."""
    seen: set[tuple[str, str, str, str]] = set()
    for item in test_report.load_branch_policy().exclusions:
        for side in item["sides"]:
            key = (
                item["file"],
                test_report.normalize(item["decision"]),
                test_report.normalize(item.get("condition", "")),
                side,
            )
            assert key not in seen, key
            seen.add(key)


def test_docs_state_the_baseline_the_gate_reads() -> None:
    text = (ROOT / "docs" / "testing.md").read_text(encoding="utf-8")
    stated = re.search(r"baseline is \*\*(\d+)\*\* untaken branch sides", text)
    assert stated, "docs/testing.md must state the branch baseline"
    assert int(stated.group(1)) == test_report.load_branch_policy().baseline


#: A branch baseline stated as the present one: "the branch baseline stays at
#: 122", "the branch baseline is 0", "the baseline is **0** untaken branch
#: sides". A figure stated as history ("fell to 120 in FW-16", "was 122") is
#: not matched.
PRESENT_BASELINE = re.compile(
    r"(?:branch baseline (?:stays at|is|remains|holds at)|"
    r"baseline (?:stays at|is|remains|holds at) \*\*(?=\d+\*\* untaken branch))"
    r" ?\**(\d[\d,]*)\**",
)


def test_every_page_states_the_baseline_the_gate_reads() -> None:
    """A branch baseline any page states as the present one is the file's."""
    baseline = test_report.load_branch_policy().baseline
    found: list[Any] = []
    for page in sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md", ROOT / "AGENTS.md"]:
        found.extend(
            (page.name, int(match.group(1).replace(",", "")))
            for match in PRESENT_BASELINE.finditer(page.read_text(encoding="utf-8"))
        )
    assert ("testing.md", baseline) in found, found
    wrong = [item for item in found if item[1] != baseline]
    assert not wrong, f"stated as the present baseline, but the file's is {baseline}: {wrong}"


def test_the_present_baseline_pattern_tells_history_from_the_present() -> None:
    """Planted sentences: the present forms match, the historical ones do not."""

    def figures(sentence: str) -> list[str]:
        return [m.group(1) for m in PRESENT_BASELINE.finditer(sentence)]

    assert figures("and the branch baseline stays at 122.") == ["122"]
    assert figures("The baseline is **0** untaken branch sides") == ["0"]
    assert not list(PRESENT_BASELINE.finditer("the branch baseline fell to 120 in FW-16"))
    assert not list(PRESENT_BASELINE.finditer("the branch baseline was 122 after FW-14"))


def clean_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A report directory the gate passes: one passing case, every branch taken both ways."""
    report = tmp_path / "report"
    (report / "native").mkdir(parents=True)
    (report / "native" / "unit.xml").write_text(
        '<testsuite name="unit"><testcase classname="u" name="ok"/></testsuite>', encoding="utf-8"
    )
    (report / "coverage.json").write_text(json.dumps(export(tmp_path, region(3, 2))), "utf-8")
    policy = tmp_path / "branch_exclusions.toml"
    policy.write_text("baseline = 0\n", encoding="utf-8")
    monkeypatch.setattr(test_report, "BRANCH_EXCLUSIONS", policy)
    monkeypatch.setattr(test_report, "EXCLUSIONS", tmp_path / "none.toml")
    return report


def test_the_gate_fails_on_mismatched_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """llvm-cov's "mismatched data" is coverage the export dropped (FW-01, 2026-11-22)."""
    report = clean_report(tmp_path, monkeypatch)
    log = report / test_report.COVERAGE_LOG
    log.write_text("", encoding="utf-8")
    assert test_report.main(["--dir", str(report), "--gate"]) == 0
    log.write_text("warning: 7 functions have mismatched data\n", encoding="utf-8")
    assert test_report.main(["--dir", str(report), "--gate"]) == 1
    assert "mismatched data" in capsys.readouterr().err
    assert "mismatched data" in (report / "report.md").read_text(encoding="utf-8")


def test_the_gate_fails_without_the_coverage_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing log cannot say the export was clean, so it is not taken to say so."""
    report = clean_report(tmp_path, monkeypatch)
    assert test_report.main(["--dir", str(report), "--gate"]) == 1


def test_run_tests_hands_the_log_to_the_report() -> None:
    """The Makefile writes the log and scripts/run_tests.sh copies it where the report reads it."""
    makefile = (ROOT / "firmware" / "test" / "native" / "Makefile").read_text(encoding="utf-8")
    assert "COVLOG        := $(COV)/llvm-cov.log" in makefile
    assert makefile.count("2>>$(COVLOG)") == 4, "every llvm-profdata and llvm-cov call is logged"
    script = (ROOT / "scripts" / "run_tests.sh").read_text(encoding="utf-8")
    assert f'"$out/{test_report.COVERAGE_LOG}"' in script
