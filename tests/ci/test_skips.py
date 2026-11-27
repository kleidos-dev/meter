# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""A test skipped in CI runs somewhere else, or the report's gate fails (FW-31).

``scripts/test_report.py`` names each reason a test may be skipped for and the
job that runs it instead (``SKIPS``); in CI any other skip fails its gate. A
conformance case without the firmware's bridge and a guard without the device
build do not skip where what they need was just built: ``scripts/run_tests.sh``
sets ``METER_BRIDGE_REQUIRED``, CI's jobs ``METER_BUILD_REQUIRED`` and
``METER_BROWSER_REQUIRED``, each of which turns the skip into a failure. Held
here: the reasons and their homes, the variables where they belong, and that
every test that reads a device build is run by a job that has one.

Host-only: the files read as text (no YAML parser is a dependency here).
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import test_report  # noqa: E402

WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
RUN_TESTS = ROOT / "scripts" / "run_tests.sh"
PYPROJECT = ROOT / "host" / "pyproject.toml"

#: The Kleidos QA's Playwright on 2026-11-22 (its qa/requirements.txt): the two move together.
KLEIDOS_PLAYWRIGHT = "1.63.0"


def jobs() -> dict[str, str]:
    """CI's jobs, each as text."""
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text[text.index("\njobs:\n") + len("\njobs:\n") :]
    parts = re.split(r"^  ([a-z0-9_-]+):\n", body, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


# -- The gate -------------------------------------------------------------------


def suite_of(tmp_path: Path, reason: str) -> list[test_report.Suite]:
    """The suites of a JUnit file with one case skipped for *reason* and one that passed."""
    path = tmp_path / "one.xml"
    message = reason.replace("&", "&amp;").replace('"', "&quot;")
    path.write_text(
        '<testsuites><testsuite name="pytest"><testcase classname="m" name="a">'
        f'<skipped type="pytest.skip" message="{message}">x</skipped></testcase>'
        '<testcase classname="m" name="b"/></testsuite></testsuites>',
        encoding="utf-8",
    )
    return test_report.read_junit(path, "guards")


@pytest.mark.parametrize(
    "reason",
    [
        "talks to the meter on the bench: give --run-bench",
        "firmware.elf does not exist: `pio run --project-dir firmware -e meter` writes it,"
        " and this guard reads it",
        "compile_commands.json does not exist: pio run ... -t compiledb writes it,"
        " and this guard reads it",
        "doxygen is not on PATH",
        "gitleaks not on PATH",
        "kicad-cli is not installed",
        "the [browser] extra is not installed: pip install -e '.[browser]' in host/",
        "the agents repository is not checked out beside this one",
    ],
)
def test_a_named_reason_has_a_home(tmp_path: Path, reason: str) -> None:
    suites = suite_of(tmp_path, reason)
    assert suites[0].skips == [("m::a", reason)]
    assert test_report.unnamed_skips(suites) == []
    assert any(reason in line for line in test_report.skipped_lines(suites))


@pytest.mark.parametrize(
    "reason",
    [
        "KLEIDOS_METER_NATIVE_BRIDGE is not set: it names the firmware's native bridge binary"
        " (make -C firmware/test/native bridge), so the suite runs against the simulator alone",
        "no bridge: make -C firmware/test/native bridge builds it",
        "pyelftools reads the images: pip install pyelftools==0.33",
        "no reason at all",
    ],
)
def test_a_skipped_conformance_case_and_an_unnamed_reason_have_none(
    tmp_path: Path, reason: str
) -> None:
    assert test_report.unnamed_skips(suite_of(tmp_path, reason)) == [("m::a", reason)]
    assert any(
        "**nowhere**" in line for line in test_report.skipped_lines(suite_of(tmp_path, reason))
    )


def test_a_module_skipped_while_collected_gives_its_reason(tmp_path: Path) -> None:
    """As the panel's browser suite is, in the job that installs no browser."""
    reason = "the [browser] extra is not installed: pip install -e '.[browser]' in host/"
    (tmp_path / "test_module.py").write_text(
        f"import pytest\n\npytest.skip({reason!r}, allow_module_level=True)\n", encoding="utf-8"
    )
    junit = tmp_path / "out.xml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            ".",
        ],
        cwd=tmp_path,
        capture_output=True,
        check=False,
    )
    (suite,) = test_report.read_junit(junit, "host")
    assert [why for _, why in suite.skips] == [reason]
    assert test_report.unnamed_skips([suite]) == []


def test_a_collection_skip_no_reader_knows_keeps_its_text() -> None:
    node = ET.fromstring('<skipped message="collection skipped">odd  text</skipped>')  # noqa: S314
    assert test_report.skip_reason(node) == "odd text"


def test_the_gate_refuses_an_unnamed_skip_in_ci_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    suite_of(tmp_path, "because")
    (tmp_path / "one.xml").rename(tmp_path / "host.xml")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("CI", raising=False)
    assert test_report.main(["--dir", str(tmp_path), "--gate"]) == 0
    assert "note: skipped: m::a: because" in capsys.readouterr().err
    monkeypatch.setenv("CI", "true")
    assert test_report.main(["--dir", str(tmp_path), "--gate"]) == 1
    assert "SKIPPED: m::a: because" in capsys.readouterr().err
    assert test_report.main(["--dir", str(tmp_path)]) == 0  # the report without the gate


def test_every_home_is_a_job_or_the_bench() -> None:
    known = set(jobs())
    for pattern, home in test_report.SKIPS:
        named = set(re.findall(r"`([a-z-]+)` (?:job|and)", home))
        assert named <= known, f"{pattern}: {home}"
        assert named or home.startswith(("the bench", "a machine")), f"{pattern}: {home}"


def test_a_report_without_skips_says_none() -> None:
    assert test_report.skipped_lines([]) == ["", "## Skipped tests", "", "None."]


# -- Where each variable is set ---------------------------------------------------


def test_run_tests_names_the_bridge_it_built_to_the_host_suite_and_the_guards() -> None:
    script = RUN_TESTS.read_text(encoding="utf-8")
    export = script.index("export KLEIDOS_METER_NATIVE_BRIDGE METER_BRIDGE_REQUIRED")
    assert 'KLEIDOS_METER_NATIVE_BRIDGE="$native/build/bridge"' in script[:export]
    assert "METER_BRIDGE_REQUIRED=1" in script[:export]
    assert script.index('-C "$native" -j"$jobs" CXX="$cxx" test') < export
    assert export < script.index('--junitxml="$out/host.xml"')
    assert export < script.index('--junitxml="$out/guards.xml"')


def test_the_panel_job_requires_the_browser_it_installed() -> None:
    job = jobs()["panel"]
    assert "pip install -e 'host[dev,browser]'" in job
    assert re.search(r'^          METER_BROWSER_REQUIRED: "1"$', job, re.MULTILINE)
    assert "tests/test_panel_browser.py" in job
    install = job.index("playwright install --with-deps chromium")
    assert job.index("path: ~/.cache/ms-playwright") < install < job.index("METER_BROWSER_REQUIRED")
    assert "steps.install.outputs.playwright }}-chromium" in job, "the cache is the release's"


def test_playwright_is_pinned_where_the_panel_job_reads_it() -> None:
    extras = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    assert extras["browser"] == [f"playwright=={KLEIDOS_PLAYWRIGHT}"]


def test_every_guard_over_a_device_build_runs_in_a_job_that_has_one() -> None:
    guards = {
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "tests" / "firmware").glob("test_*.py"))
        if re.search(r"(?<![\w.])built\(", path.read_text(encoding="utf-8"))
    }
    assert "tests/firmware/test_heap_reachability_gate.py" in guards and len(guards) >= 5
    required: set[str] = set()
    for job in jobs().values():
        for run in re.findall(r"METER_BUILD_REQUIRED=1 python3 -m pytest((?:.*\\\n)*.*\n)", job):
            required |= set(re.findall(r"tests/firmware/test_\w+\.py", run))
    assert guards <= required, f"no job runs these with a device build: {guards - required}"


def test_the_gitleaks_rules_run_where_gitleaks_is() -> None:
    job = jobs()["lint"]
    assert "gitleaks version" in job
    assert job.index("gitleaks version") < job.index("tests/ci/test_gitleaks_rules.py")


def test_the_report_names_where_each_skip_runs(tmp_path: Path) -> None:
    lines = test_report.skipped_lines(suite_of(tmp_path, "kicad-cli is not installed"))
    assert lines[-1] == "| guards | kicad-cli is not installed | 1 | the `schematic` job |"


def test_the_headline_counts_a_skipped_test_as_skipped_and_says_where_it_runs(
    tmp_path: Path,
) -> None:
    suites = suite_of(tmp_path, "talks to the meter on the bench: give --run-bench")
    report = test_report.render_markdown(suites, [], [], None)
    headline = next(line for line in report.splitlines() if line.startswith("Revision"))
    assert "**1 passed, 1 skipped, 0 failed of 2 tests**" in headline
    assert "(skipped: 1 on the bench (`--run-bench`))" in headline
    assert "of 2 tests passed" not in headline
