#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The test report: every suite's verdict and every module's coverage, in one page.

Reads what ``scripts/run_tests.sh`` leaves in ``build/test-report/``:

- the firmware's host tests: JUnit XML per binary and LLVM's coverage export
  (statements, branches, MC/DC) from ``make -C firmware/test/native coverage``;
- the host package's (the client, the simulator, the panel) and the firmware
  guards' pytest JUnit XML, and the package's branch coverage (``coverage json``).

Writes ``report.md`` and ``report.html`` beside them, and appends the Markdown
to ``$GITHUB_STEP_SUMMARY`` when CI sets it. With ``--gate`` it exits 1 when a
test failed, when an MC/DC condition is uncovered without a written reason in
``firmware/test/native/mcdc_exclusions.toml``, or when more branch sides are
left untaken without a written reason in
``firmware/test/native/branch_exclusions.toml`` than its ``baseline`` allows
(the ratchet: the baseline only falls, until it reaches zero; docs/testing.md),
or when llvm-profdata or llvm-cov printed anything while the coverage was
exported (``native-llvm-cov.log``): their one warning, "N functions have
mismatched data", means coverage the export dropped. It also exits 1 when a
module of the host package takes a smaller share of its branch sides than its
floor in ``host/branch_floors.toml`` (the floors only rise), or has none; and
in CI (``CI=true``) when a test was skipped for a reason :data:`SKIPS` does not
name with where that test runs instead.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "build" / "test-report"
NATIVE = ROOT / "firmware" / "test" / "native"
EXCLUSIONS = NATIVE / "mcdc_exclusions.toml"
BRANCH_EXCLUSIONS = NATIVE / "branch_exclusions.toml"
#: The constant conditions LLVM folds: a branch region that is one has no second side.
FOLDED = frozenset({"true", "false"})
#: The host package's branch floors, module by module.
HOST_FLOORS = ROOT / "host" / "branch_floors.toml"
#: A module that takes more branch sides than this over its floor is worth raising it:
#: the floors of the modules threads run in leave two.
FLOOR_SLACK = 3
#: What ``make coverage`` keeps of llvm-profdata's and llvm-cov's diagnostics
#: (``COVLOG`` in firmware/test/native/Makefile), copied beside
#: ``coverage.json`` by scripts/run_tests.sh.
COVERAGE_LOG = "native-llvm-cov.log"


#: Why a test may be skipped in CI, as a pattern its whole reason matches, and where
#: it runs instead: the job of check.yml, or the one place that has what it needs.
SKIPS: tuple[tuple[str, str], ...] = (
    (r"talks to the meter on the bench: give --run-bench", "the bench (`--run-bench`)"),
    (r"takes minutes on the bench: give --bench-long", "the bench (`--bench-long`)"),
    (
        r"firmware\.elf does not exist: .* writes it, and this guard reads it"
        r"|(sdkconfig\.meter|project_description\.json|target-kleidos-meter\.elf\.json)"
        r" does not exist: .* writes it, and this guard reads it",
        "the `firmware` job (`METER_BUILD_REQUIRED=1`)",
    ),
    (
        r"compile_commands\.json does not exist: .* writes it, and this guard reads it",
        "the `firmware` and `static-analysis` jobs (`METER_BUILD_REQUIRED=1`)",
    ),
    (r"doxygen is not on PATH", "the `docs` job"),
    (r"gitleaks not on PATH", "the `lint` job"),
    (r"kicad-cli is not installed", "the `schematic` job"),
    (
        r"the \[browser\] extra is not installed: .*|Playwright's Chromium is not installed: .*",
        "the `panel` job (`METER_BROWSER_REQUIRED=1`)",
    ),
    (
        r"the (agents|firmware) repository is not checked out beside this one",
        "a machine that has that repository beside this one",
    ),
)


@dataclass
class Suite:
    source: str
    name: str
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    seconds: float = 0.0
    failed: list[str] = field(default_factory=list)
    #: Each skipped case and the reason it gave.
    skips: list[tuple[str, str]] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return self.tests - self.failures - self.errors - self.skipped

    @property
    def ok(self) -> bool:
        return self.failures == 0 and self.errors == 0


@dataclass
class Gap:
    file: str
    line: int
    decision: str
    covered: list[bool]
    reason: str | None = None
    kind: str | None = None
    note: str | None = None


@dataclass
class BranchGap:
    """One side of one branch the tests never took."""

    file: str
    line: int
    decision: str
    side: str  # "true" or "false"
    reason: str | None = None
    kind: str | None = None
    #: The branch region's own text: one operand of an ``&&`` or ``||``, a
    #: loop's or an ``if``'s whole condition, or a ``case`` label.
    operand: str = ""


@dataclass
class BranchPolicy:
    """``branch_exclusions.toml``: the ratchet's baseline and the written reasons."""

    baseline: int = 0
    exclusions: list[dict[str, Any]] = field(default_factory=list)


def read_junit(path: Path, source: str) -> list[Suite]:
    """The suites of one JUnit XML file (pytest's or the native runner's)."""
    root = ET.parse(path).getroot()  # noqa: S314  the JUnit XML our own test runs wrote
    nodes = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    suites = []
    for node in nodes:
        suite = Suite(source, node.get("name", path.stem))
        for case in node.iter("testcase"):
            suite.tests += 1
            suite.seconds += float(case.get("time", "0") or 0)
            if case.find("failure") is not None or case.find("error") is not None:
                if case.find("error") is not None:
                    suite.errors += 1
                else:
                    suite.failures += 1
                suite.failed.append(f"{case.get('classname', '')}::{case.get('name', '')}")
            elif (skipped := case.find("skipped")) is not None:
                suite.skipped += 1
                suite.skips.append(
                    (
                        f"{case.get('classname', '')}::{case.get('name', '')}",
                        skip_reason(skipped),
                    )
                )
        if suite.tests:
            suites.append(suite)
    return suites


def skip_reason(node: ET.Element) -> str:
    """Why a test was skipped, from its JUnit ``skipped`` element.

    A module skipped while it was collected says ``collection skipped``; its
    reason is in the element's text, pytest's ``(file, line, reason)`` printed.
    """
    message = node.get("message") or ""
    if message == "collection skipped":
        try:
            message = str(ast.literal_eval((node.text or "").strip())[2])
        except (ValueError, SyntaxError, IndexError, TypeError):
            message = node.text or ""
    return normalize(message.removeprefix("Skipped: "))


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def load_exclusions(path: Path = EXCLUSIONS) -> list[dict[str, str]]:
    if not path.exists():
        return []
    exclusions: list[dict[str, str]] = tomllib.loads(path.read_text(encoding="utf-8")).get(
        "exclusion", []
    )
    return exclusions


def native_coverage(
    data: dict[str, Any],
    exclusions: list[dict[str, Any]],
    stale: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[Gap]]:
    """Per-module figures and every MC/DC condition left uncovered.

    An entry justifies a gap only when its decision's text is in the gap's and
    every uncovered condition (1-based, left to right) is one its ``conditions``
    names: a further condition losing its pair in an excluded decision is a new,
    unjustified gap. Entries no gap used are appended to *stale*.
    """
    if stale is None:
        stale = []
    modules: list[dict[str, Any]] = []
    gaps: list[Gap] = []
    used: set[int] = set()
    for entry in data["data"][0]["files"]:
        filename = Path(entry["filename"])
        name = filename.name
        summary = entry["summary"]
        mcdc = summary.get("mcdc", {"count": 0, "covered": 0})
        source = filename.read_text(encoding="utf-8").splitlines() if filename.exists() else []
        justified = 0
        for record in entry.get("mcdc_records", []):
            covered = record[9]
            if all(covered):
                continue
            text = normalize(" ".join(source[record[0] - 1 : record[2]]))
            gap = Gap(name, record[0], text, covered)
            missing = {i + 1 for i, shown in enumerate(covered) if not shown}
            for index, exclusion in enumerate(exclusions):
                if exclusion["file"] != name or normalize(exclusion["decision"]) not in text:
                    continue
                used.add(index)
                if missing <= set(exclusion.get("conditions", [])):
                    gap.reason, gap.kind = exclusion["reason"], exclusion["kind"]
                    justified += len(missing)
                else:
                    gap.note = (
                        f"conditions {sorted(missing)} uncovered, the entry justifies "
                        f"{sorted(exclusion.get('conditions', []))}"
                    )
                break
            gaps.append(gap)
        modules.append(
            {
                "module": name,
                "lines": summary["lines"],
                "branches": summary["branches"],
                "mcdc": mcdc,
                "justified": justified,
            }
        )
    modules.sort(key=lambda m: str(m["module"]))
    stale.extend(e for i, e in enumerate(exclusions) if i not in used)
    return modules, gaps


def load_branch_policy(path: Path | None = None) -> BranchPolicy:
    path = BRANCH_EXCLUSIONS if path is None else path
    if not path.exists():
        return BranchPolicy()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return BranchPolicy(int(data.get("baseline", 0)), list(data.get("exclusion", [])))


def native_branches(
    data: dict[str, Any],
    exclusions: list[dict[str, Any]],
    stale: list[dict[str, Any]] | None = None,
) -> list[BranchGap]:
    """Every side of every branch no test took, with its reason when one is written.

    LLVM exports a branch region once per binary that holds the code, so the
    regions are merged by their position first: a side counts as taken when any
    binary took it. An entry justifies a side when its file matches, its
    decision's text is in the branch's source line, its ``sides`` names the
    side and, when it names a ``condition``, that text is in the region's own
    (the one operand of an ``&&`` or ``||`` the reason is about, so the other
    operands of the same line keep their own gaps). Entries no gap used are
    appended to *stale*.

    A region whose whole text is the literal ``true`` or ``false`` (the
    ``while ( true )`` of a task) is no branch: the condition is a constant the
    compiler folds, so the other side is not in the program. LLVM's summary
    leaves such a region out of its branch count; the JSON export lists it with
    nothing to tell it by but its text.
    """
    if stale is None:
        stale = []
    gaps: list[BranchGap] = []
    used: set[int] = set()
    for entry in data["data"][0]["files"]:
        filename = Path(entry["filename"])
        name = filename.name
        regions: dict[tuple[int, ...], tuple[int, int]] = {}
        for record in entry.get("branches", []):
            key = tuple(record[0:4])
            taken, not_taken = regions.get(key, (0, 0))
            regions[key] = (max(taken, record[4]), max(not_taken, record[5]))
        source = filename.read_text(encoding="utf-8").splitlines() if filename.exists() else []
        for key in sorted(regions):
            true_count, false_count = regions[key]
            text = normalize(" ".join(source[key[0] - 1 : key[2]]))
            operand = region_text(source, key)
            if operand in FOLDED:
                continue
            for side, count in (("true", true_count), ("false", false_count)):
                if count != 0:
                    continue
                gap = BranchGap(name, key[0], text, side, operand=operand)
                for index, exclusion in enumerate(exclusions):
                    if (
                        exclusion["file"] == name
                        and normalize(exclusion["decision"]) in text
                        and side in exclusion.get("sides", [])
                        and normalize(exclusion.get("condition", "")) in operand
                    ):
                        used.add(index)
                        gap.reason, gap.kind = exclusion["reason"], exclusion["kind"]
                        break
                gaps.append(gap)
    stale.extend(e for i, e in enumerate(exclusions) if i not in used)
    return gaps


def region_text(source: list[str], key: tuple[int, ...]) -> str:
    """The source a branch region spans, from its first column to its last, normalized.

    *key* is LLVM's ``(line_start, column_start, line_end, column_end)``, all
    1-based, the end column one past the region's last character.
    """
    start_line, start_column, end_line, end_column = key[0:4]
    if not 0 < start_line <= end_line <= len(source):
        return ""
    if start_line == end_line:
        return normalize(source[start_line - 1][start_column - 1 : end_column - 1])
    lines = [source[start_line - 1][start_column - 1 :]]
    lines += source[start_line : end_line - 1]
    lines.append(source[end_line - 1][: end_column - 1])
    return normalize(" ".join(lines))


def branch_verdict(gaps: list[BranchGap], baseline: int) -> tuple[bool, str]:
    """Whether the ratchet holds, and the sentence that says so."""
    unjustified = sum(1 for g in gaps if g.reason is None)
    if unjustified > baseline:
        return False, (
            f"{unjustified} untaken branch side(s) without a written reason, over the "
            f"baseline of {baseline}: add the tests, or a reason in branch_exclusions.toml"
        )
    if unjustified < baseline:
        return True, (
            f"{unjustified} untaken branch side(s) without a written reason, under the "
            f"baseline of {baseline}: lower it to {unjustified} (branch_exclusions.toml "
            "and docs/testing.md)"
        )
    return True, f"{unjustified} untaken branch side(s) without a written reason, the baseline"


def coverage_diagnostics(directory: Path) -> list[str]:
    """Return what LLVM's tools printed while the coverage was exported, one line each.

    An empty list is a clean export. A missing log is a diagnostic of its own:
    without it nothing says the export was clean.
    """
    log = directory / COVERAGE_LOG
    if not log.exists():
        return [f"{COVERAGE_LOG} is missing beside coverage.json, so the export is unchecked"]
    return [line.strip() for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def skip_home(reason: str) -> str | None:
    """Where a test skipped for *reason* runs instead; None for a reason nobody named."""
    for pattern, home in SKIPS:
        if re.fullmatch(pattern, reason):
            return home
    return None


def unnamed_skips(suites: list[Suite]) -> list[tuple[str, str]]:
    """Every skipped case whose reason :data:`SKIPS` does not name, with the reason."""
    return [
        (case, reason)
        for suite in suites
        for case, reason in suite.skips
        if skip_home(reason) is None
    ]


def load_host_floors(path: Path | None = None) -> dict[str, float]:
    """The host package's floors: module (its path under ``host/``) to percent of branch sides.

    :raises ValueError: a floor that is not a number from 0 to 100.
    """
    path = HOST_FLOORS if path is None else path
    floors = tomllib.loads(path.read_text(encoding="utf-8")).get("floors", {})
    for module, floor in floors.items():
        if not isinstance(floor, int | float) or isinstance(floor, bool) or not 0 <= floor <= 100:
            raise ValueError(f"{path.name}: {module}: {floor!r} is not a percentage")
    return {module: float(floor) for module, floor in floors.items()}


def branch_percent(summary: dict[str, Any]) -> float:
    """The percentage of a module's branch sides its tests took; 100 for one with none."""
    count = summary.get("num_branches", 0)
    return 100.0 if count == 0 else 100.0 * summary.get("covered_branches", 0) / count


def host_floor_verdict(
    host: dict[str, Any], floors: dict[str, float]
) -> tuple[list[str], list[str]]:
    """What fails the floors, and what is worth raising.

    :param host: coverage.py's JSON report.
    :param floors: :func:`load_host_floors`.
    :returns: The problems (a module under its floor, one with no floor, a floor
        whose module is gone) and the notes (a figure well above its floor).
    """
    summaries = {
        Path(name).as_posix(): entry["summary"] for name, entry in host.get("files", {}).items()
    }
    measured = {module: branch_percent(summary) for module, summary in summaries.items()}
    problems = [f"{module}: no floor" for module in sorted(measured.keys() - floors.keys())]
    problems += [
        f"{module}: a floor, and no such module was measured"
        for module in sorted(floors.keys() - measured.keys())
    ]
    notes = []
    for module in sorted(measured.keys() & floors.keys()):
        figure, floor = measured[module], floors[module]
        if figure < floor:
            problems.append(
                f"{module}: {figure:.2f} % of its branch sides, under its floor of {floor:g} %"
            )
        elif (figure - floor) / 100 * summaries[module].get("num_branches", 0) > FLOOR_SLACK:
            notes.append(f"{module}: {figure:.2f} %, over its floor of {floor:g} %: raise it")
    return problems, notes


def percent(covered: int, count: int) -> str:
    return "—" if count == 0 else f"{100.0 * covered / count:.1f} %"


def git_revision() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def render_markdown(
    suites: list[Suite],
    modules: list[dict[str, Any]],
    gaps: list[Gap],
    host: dict[str, Any] | None,
    branch_gaps: list[BranchGap] | None = None,
    baseline: int = 0,
    diagnostics: list[str] | None = None,
    floors: dict[str, float] | None = None,
) -> str:
    total = sum(s.tests for s in suites)
    failed = sum(s.failures + s.errors for s in suites)
    skipped = sum(s.skipped for s in suites)
    unjustified = [g for g in gaps if g.reason is None]
    lines = [
        "# Kleidos meter — test report",
        "",
        f"Revision `{git_revision()}`, {dt.datetime.now(dt.UTC):%Y-%m-%d %H:%M} UTC. "
        f"**{total - failed - skipped} passed, {skipped} skipped, {failed} failed of {total} "
        f"tests**{skipped_homes(suites)}; "
        f"{len(unjustified)} MC/DC gap(s) without a written reason; "
        f"{sum(1 for g in branch_gaps or [] if g.reason is None)} untaken branch side(s) "
        f"without one (baseline {baseline}).",
        "",
        "## Suites",
        "",
        "| Source | Suite | Passed | Failed | Skipped | Time (s) | Verdict |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    lines.extend(
        f"| {s.source} | {s.name} | {s.passed} | {s.failures + s.errors} | {s.skipped} "
        f"| {s.seconds:.2f} | {'pass' if s.ok else '**FAIL**'} |"
        for s in suites
    )
    failures = [name for s in suites for name in s.failed]
    if failures:
        lines += ["", "Failed tests:", "", *(f"- `{name}`" for name in failures)]

    lines += skipped_lines(suites)

    lines += [
        "",
        "## Firmware coverage (LLVM: statements, branches, MC/DC)",
        "",
        "MC/DC is LLVM's short-circuit (masking) MC/DC over every decision of two or "
        "more conditions; a condition counts when the tests hold a pair of runs in "
        "which it alone flips the decision. *Justified* counts the uncovered conditions "
        "given a written reason in `firmware/test/native/mcdc_exclusions.toml`.",
        "",
        "| Module | Lines | Branches | MC/DC | MC/DC conditions | Justified | "
        "MC/DC with justified |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    totals = {"lc": 0, "ln": 0, "bc": 0, "bn": 0, "mc": 0, "mn": 0, "j": 0}
    for m in modules:
        lines_ = m["lines"]
        branches = m["branches"]
        mcdc = m["mcdc"]
        justified = int(m["justified"])
        totals["lc"] += lines_["covered"]
        totals["ln"] += lines_["count"]
        totals["bc"] += branches["covered"]
        totals["bn"] += branches["count"]
        totals["mc"] += mcdc["covered"]
        totals["mn"] += mcdc["count"]
        totals["j"] += justified
        lines.append(
            f"| `{m['module']}` | {percent(lines_['covered'], lines_['count'])} "
            f"| {percent(branches['covered'], branches['count'])} "
            f"| {percent(mcdc['covered'], mcdc['count'])} "
            f"| {mcdc['covered']}/{mcdc['count']} | {justified} "
            f"| {percent(mcdc['covered'] + justified, mcdc['count'])} |"
        )
    lines.append(
        f"| **total** | {percent(totals['lc'], totals['ln'])} "
        f"| {percent(totals['bc'], totals['bn'])} "
        f"| {percent(totals['mc'], totals['mn'])} | {totals['mc']}/{totals['mn']} | {totals['j']} "
        f"| {percent(totals['mc'] + totals['j'], totals['mn'])} |"
    )

    if host is not None:
        floors = floors or {}
        lines += [
            "",
            "## Host coverage (coverage.py: statements and branches)",
            "",
            "Each module's floor is the smallest share of its branch sides the gate allows "
            "(`host/branch_floors.toml`); a floor only rises.",
            "",
            "| Module | Statements | Branches | Floor |",
            "|---|---:|---:|---:|",
        ]
        for name, entry in sorted(host.get("files", {}).items()):
            s = entry["summary"]
            module = Path(name).as_posix()
            floor = f"{floors[module]:g} %" if module in floors else "**none**"
            lines.append(
                f"| `{module.removeprefix('kleidos_meter/')}` | "
                f"{percent(s['covered_lines'], s['num_statements'])} | "
                f"{percent(s.get('covered_branches', 0), s.get('num_branches', 0))} | {floor} |"
            )
        t = host["totals"]
        lines.append(
            f"| **total** | {percent(t['covered_lines'], t['num_statements'])} | "
            f"{percent(t.get('covered_branches', 0), t.get('num_branches', 0))} | |"
        )

    lines += ["", "## MC/DC conditions not shown independent", ""]
    if not gaps:
        lines.append("None: every condition of every decision is shown to decide on its own.")
    else:
        lines += [
            "| Where | Decision | Conditions shown | Kind | Reason |",
            "|---|---|---|---|---|",
        ]
        for g in gaps:
            shown = "".join("✓" if c else "✗" for c in g.covered)
            decision = g.decision.replace("|", "\\|")[:110]
            lines.append(
                f"| `{g.file}:{g.line}` | `{decision}` | {shown} | {g.kind or '**none**'} "
                f"| {g.reason or '**UNJUSTIFIED**' + (f' ({g.note})' if g.note else '')} |"
            )
    lines += ["", "## Branch sides not taken", ""]
    if branch_gaps is None:
        lines.append("No coverage export: not measured.")
    else:
        verdict = branch_verdict(branch_gaps, baseline)[1]
        lines += [
            "Every side of every branch (each `if`, loop test, `?:` and short-circuit "
            "operand, taken and not taken) that no test took. The gate allows as many "
            "without a written reason as the baseline in "
            "`firmware/test/native/branch_exclusions.toml`, which only falls. "
            f"{verdict}.",
            "",
        ]
        if not branch_gaps:
            lines.append("None: every branch is taken both ways.")
        else:
            lines += [
                "| Where | Branch | Side not taken | Kind | Reason |",
                "|---|---|---|---|---|",
            ]
            for bg in branch_gaps:
                decision = bg.decision.replace("|", "\\|")[:110]
                lines.append(
                    f"| `{bg.file}:{bg.line}` | `{decision}` | {bg.side} | {bg.kind or '—'} "
                    f"| {bg.reason or 'none yet'} |"
                )
    lines += ["", "## Coverage tools", ""]
    if diagnostics is None:
        lines.append("No coverage export: not measured.")
    elif not diagnostics:
        lines.append("llvm-profdata and llvm-cov printed nothing: no record was dropped.")
    else:
        lines.append("**FAIL**: llvm-profdata or llvm-cov printed, and the gate refuses it:")
        lines.append("")
        lines += [f"- `{line}`" for line in diagnostics]
    lines.append("")
    return "\n".join(lines)


def skipped_homes(suites: list[Suite]) -> str:
    """The headline's note of where the skipped tests run instead: `` (skipped: 46 in the
    `firmware` job ..., 1 on the bench ...)``, or nothing when none was skipped."""
    homes: dict[str, int] = {}
    for suite in suites:
        for _, reason in suite.skips:
            home = skip_home(reason) or "nowhere"
            homes[home] = homes.get(home, 0) + 1
    if not homes:
        return ""
    where = ", ".join(
        f"{count} {'on' if home.startswith(('the bench', 'a machine')) else 'in'} {home}"
        if home != "nowhere"
        else f"{count} nowhere"
        for home, count in sorted(homes.items())
    )
    return f" (skipped: {where})"


def skipped_lines(suites: list[Suite]) -> list[str]:
    """The report's section on skipped tests: each reason, how many gave it, where they run."""
    counts: dict[tuple[str, str], int] = {}
    for suite in suites:
        for _, reason in suite.skips:
            key = (suite.source, reason)
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ["", "## Skipped tests", "", "None."]
    lines = [
        "",
        "## Skipped tests",
        "",
        "In CI a test skipped for a reason without a place to run instead fails the gate "
        "(`SKIPS` in `scripts/test_report.py`).",
        "",
        "| Source | Reason | Tests | Runs in |",
        "|---|---|---:|---|",
    ]
    for (source, reason), count in sorted(counts.items()):
        text = reason.replace("|", "\\|")
        lines.append(f"| {source} | {text} | {count} | {skip_home(reason) or '**nowhere**'} |")
    return lines


def render_html(markdown: str) -> str:
    """A self-contained page: the Markdown's tables as HTML, no external asset."""
    body: list[str] = []
    in_table = False
    for raw in markdown.splitlines():
        if raw.startswith("|"):
            cells = [c.strip() for c in raw.strip("|").split("|")]
            if all(set(c) <= set("-:") for c in cells):
                continue
            tag = "th" if not in_table else "td"
            if not in_table:
                body.append("<table>")
                in_table = True
            body.append("<tr>" + "".join(f"<{tag}>{inline(c)}</{tag}>" for c in cells) + "</tr>")
            continue
        if in_table:
            body.append("</table>")
            in_table = False
        if raw.startswith("# "):
            body.append(f"<h1>{inline(raw[2:])}</h1>")
        elif raw.startswith("## "):
            body.append(f"<h2>{inline(raw[3:])}</h2>")
        elif raw.startswith("- "):
            body.append(f"<p>• {inline(raw[2:])}</p>")
        elif raw.strip():
            body.append(f"<p>{inline(raw)}</p>")
    if in_table:
        body.append("</table>")
    style = (
        "body{font:14px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:72rem;padding:0 1rem;"
        "color:#1d1d1f;background:#fff}table{border-collapse:collapse;margin:1rem 0;width:100%}"
        "th,td{border:1px solid #d2d2d7;padding:.3rem .5rem;text-align:left;vertical-align:top}"
        "th{background:#f5f5f7}code{font:12px ui-monospace,monospace}"
        "@media (prefers-color-scheme:dark){body{color:#f5f5f7;background:#1d1d1f}"
        "th{background:#2c2c2e}th,td{border-color:#3a3a3c}}"
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Meter test report</title><style>{style}</style></head><body>"
        + "\n".join(body)
        + "</body></html>\n"
    )


def inline(text: str) -> str:
    out = html.escape(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=REPORT, help="where the inputs are")
    parser.add_argument("--gate", action="store_true", help="exit 1 on a failure or a gap")
    args = parser.parse_args(argv)

    suites: list[Suite] = []
    for path in sorted((args.dir / "native").glob("*.xml")):
        suites += read_junit(path, f"firmware ({path.stem})")
    for label in ("host", "guards"):
        path = args.dir / f"{label}.xml"
        if path.exists():
            suites += read_junit(path, label)

    modules: list[dict[str, Any]] = []
    gaps: list[Gap] = []
    stale: list[dict[str, Any]] = []
    branch_gaps: list[BranchGap] | None = None
    diagnostics: list[str] | None = None
    policy = load_branch_policy()
    coverage = args.dir / "coverage.json"
    if coverage.exists():
        exported = json.loads(coverage.read_text())
        modules, gaps = native_coverage(exported, load_exclusions(), stale)
        branch_gaps = native_branches(exported, policy.exclusions, stale)
        diagnostics = coverage_diagnostics(args.dir)
    host_path = args.dir / "host-coverage.json"
    host = json.loads(host_path.read_text()) if host_path.exists() else None
    floors = load_host_floors()
    under, worth_raising = ([], []) if host is None else host_floor_verdict(host, floors)

    markdown = render_markdown(
        suites, modules, gaps, host, branch_gaps, policy.baseline, diagnostics, floors
    )
    args.dir.mkdir(parents=True, exist_ok=True)
    (args.dir / "report.md").write_text(markdown, encoding="utf-8")
    (args.dir / "report.html").write_text(render_html(markdown), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as out:
            out.write(markdown + "\n")
    print(f"report: {args.dir / 'report.md'} and report.html")

    failed = sum(s.failures + s.errors for s in suites)
    unjustified = [g for g in gaps if g.reason is None]
    for g in unjustified:
        extra = f" ({g.note})" if g.note else ""
        print(f"UNJUSTIFIED MC/DC gap: {g.file}:{g.line}: {g.decision}{extra}", file=sys.stderr)
    # Not a failure: an LLVM release may not instrument a decision another one
    # does (LLVM 21 does not see console.cpp's lead-verdict test as one).
    for e in stale:
        print(f"note: exclusion matched no gap: {e['file']}: {e['decision']}", file=sys.stderr)
    branches_hold = True
    if branch_gaps is not None:
        branches_hold, verdict = branch_verdict(branch_gaps, policy.baseline)
        print(("" if branches_hold else "BRANCH GATE: ") + verdict, file=sys.stderr)
    for line in diagnostics or []:
        print(f"COVERAGE TOOLS: {line}", file=sys.stderr)
    for line in under:
        print(f"HOST FLOOR: {line} (host/branch_floors.toml)", file=sys.stderr)
    for line in worth_raising:
        print(f"note: host floor: {line}", file=sys.stderr)
    in_ci = os.environ.get("CI") == "true"
    unnamed = unnamed_skips(suites)
    for case, reason in unnamed:
        label = "SKIPPED" if in_ci else "note: skipped"
        print(f"{label}: {case}: {reason}", file=sys.stderr)
    if not suites:
        print("no test results found", file=sys.stderr)
        return 1 if args.gate else 0
    refused = (
        failed or unjustified or not branches_hold or diagnostics or under or (in_ci and unnamed)
    )
    return 1 if args.gate and refused else 0


if __name__ == "__main__":
    sys.exit(main())
