# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every pytest run of the repository is parallel unless it talks to the bench.

``host/pyproject.toml`` and ``tests/pytest.ini`` start every run with
``-n auto``, so a forgetful caller still gets the parallel run; ``-n 0`` runs
serially; and ``--run-bench``, which holds the meter's port and the bench lock,
runs serially whatever ``-n`` says (the conftest files make it so). These tests
run a three-case file of each tree in a child pytest and read xdist's banner.

Host-only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
#: Each tree, its working directory and a small file of it.
TREES = {
    "host": (ROOT / "host", "tests/test_identity.py"),
    "guards": (ROOT, "tests/ci/test_skips.py"),
}


def run(tree: str, *options: str) -> str:
    """The output of a child pytest over the tree's small file, with *options*."""
    cwd, target = TREES[tree]
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--no-cov", *options, target],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return done.stdout


@pytest.mark.parametrize("tree", TREES)
def test_a_plain_run_is_parallel(tree: str) -> None:
    assert " workers [" in run(tree)


@pytest.mark.parametrize("tree", TREES)
def test_a_serial_run_is_asked_for_with_n_0(tree: str) -> None:
    assert " workers [" not in run(tree, "-n", "0")


@pytest.mark.parametrize("tree", TREES)
def test_a_run_that_talks_to_the_bench_is_serial_whatever_n_says(tree: str) -> None:
    assert " workers [" not in run(tree, "--run-bench")
    assert " workers [" not in run(tree, "--run-bench", "-n", "4")


def test_every_ci_step_that_runs_pytest_has_xdist() -> None:
    """``-n auto`` is in both trees' addopts: a step with pytest and no xdist cannot start.

    A step installs the host package's ``dev`` extra (which pins pytest-xdist)
    or names ``pytest-xdist`` beside pytest, at the host package's pin.
    """
    workflows = ROOT / ".github" / "workflows"
    pin = re.search(r'"(pytest-xdist==[^"]+)"', (ROOT / "host" / "pyproject.toml").read_text())
    assert pin
    for workflow in sorted(workflows.glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        for job in re.split(r"^  [a-z0-9_-]+:\n", text[text.index("\njobs:\n") :], flags=re.M)[1:]:
            if not re.search(r"\bpython3? -m pytest\b", job):
                continue
            installs = " ".join(re.findall(r"pip install[^\n]*", job))
            assert "host[dev" in installs or pin.group(1) in installs, (
                f"{workflow.name}: a job runs pytest without pytest-xdist ({installs!r})"
            )
