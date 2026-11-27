# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The host's mutants and the page's coverage gate run in every CI run that reads them.

``scripts/run_tests.sh`` runs the host package's mutants after its tests
(``scripts/run_mutants.sh``): all of them where Chromium is installed, those
whose kill command asks for no browser where it is not, which is CI's tests
job. CI's panel job, which installs it, runs the others, and the browser suite
whole, so that the suite's last test judges the pure modules' coverage
(``host/js_floors.toml``) instead of skipping; the figures it records go to
the run's summary, and a run without them fails. The firmware's mutants take
too long for either and are run by hand and before a release
(``docs/testing.md``, "Mutants").

Host-only: the files read as text (no YAML parser is a dependency here).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
RUN_TESTS = ROOT / "scripts" / "run_tests.sh"
PATCHES = sorted((ROOT / "host" / "mutants").glob("*.patch"))

sys.path.insert(0, str(ROOT / "host" / "tests"))

import js_coverage  # noqa: E402


def job(name: str) -> str:
    """CI's job *name*, as text, without its comments."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z0-9_-]+:\n|\Z)", text, re.M | re.S)
    assert match, name
    return "\n".join(line for line in match.group(1).splitlines() if line.strip()[:1] != "#")


def panel_runs(*changed: str) -> bool:
    """Whether a change of exactly *changed* sets ``panel=true``."""
    text = WORKFLOW.read_text(encoding="utf-8")

    def pattern(name: str) -> re.Pattern[str]:
        match = re.search(rf"^\s*{name}='(.*)'$", text, re.MULTILINE)
        assert match, f"check.yml assigns no {name}"
        return re.compile(match.group(1))

    excluded, runners = pattern("not_panel"), pattern("panel_runners")
    return any(not excluded.search(p) or runners.search(p) for p in changed)


def test_run_tests_runs_the_hosts_mutants_after_its_tests() -> None:
    script = RUN_TESTS.read_text(encoding="utf-8")
    tests = script.index('--junitxml="$out/host.xml"')
    step = script.index('"$root/scripts/run_mutants.sh" "$mutants"')
    assert tests < step < script.index('"$root/scripts/test_report.py" --gate')
    assert "mutants=--host" in script[tests:step] and "mutants=--package" in script[tests:step]
    assert 'PYTHON="$python"' in script[tests:step]
    assert "|| true" not in script[tests : script.index("\n", step)]


def test_the_tests_job_runs_them_through_that_script() -> None:
    assert "run: scripts/run_tests.sh\n" in job("tests")


def test_the_panel_job_runs_the_pages_mutants_where_its_browser_is() -> None:
    panel = job("panel")
    install = panel.index("playwright install --with-deps chromium")
    suite = panel.index("tests/test_panel_browser.py")
    assert install < suite < panel.index("run: scripts/run_mutants.sh --page\n")


def test_the_panel_job_runs_the_whole_suite_and_shows_the_pages_coverage() -> None:
    panel = job("panel")
    run = re.search(r"python3 -m pytest ((?:.*\\\n)*.*tests/test_panel_browser\.py.*)", panel)
    assert run, "the panel job runs no browser suite"
    assert not re.search(r"(?:^|\s)(?:-k|-m|--lf|--ff|-x|--deselect)\b", run.group(1)), run.group(1)
    assert "test_panel_browser.py::" not in panel, "a selection skips the coverage gate"
    suite_start = panel.index("The page in the browser")
    # grep finds no figure when the gate skipped, and pipefail makes that a failure.
    assert "set -euo pipefail" in panel[suite_start : panel.index("### The page")]
    figure = re.search(r"grep -oE '(.*)' \.\./build/test-report/panel\.xml", panel)
    assert figure, "the panel job shows no coverage figure"
    for module in js_coverage.PURE:
        line = f'<property name="{module}" value="functions 100.00 % (9 of 9), blocks 99 %" />'
        assert re.search(figure.group(1), line), module


@pytest.mark.parametrize(
    "path",
    [
        "scripts/run_mutants.sh",
        "host/mutants/HO10-gapwide.patch",
        "host/js_floors.toml",
        "host/tests/js_coverage.py",
        "host/kleidos_meter/panel/static/js/chart-math.js",
    ],
)
def test_a_change_to_what_the_panel_job_reads_runs_it(path: str) -> None:
    assert (ROOT / path).is_file(), path
    assert panel_runs(path), f"a change to {path} alone skips the panel job"


def test_a_change_to_another_script_still_skips_the_panel_job() -> None:
    assert not panel_runs("scripts/run_tests.sh", "scripts/mutants/F1.patch", "docs/usage.md")


def test_both_lists_have_mutants() -> None:
    page = [patch for patch in PATCHES if "METER_BROWSER_REQUIRED=1" in patch.read_text("utf-8")]
    assert len(page) >= 10 and len(PATCHES) - len(page) >= 10
