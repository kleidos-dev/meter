# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The firmware's mutants stay runnable, and the runner judges both lists.

``scripts/run_mutants.sh`` applies each ``scripts/mutants/*.patch`` to a copy of
the firmware and requires the host tests to fail on it (``docs/testing.md``,
"Mutants"). That run takes half a minute a mutant, so it is not part of every
test run; what every run holds is that the list has not rotted: each patch applies
to the sources as they are (a change that moves a mutant's lines needs its
patch written again, or the mutant was removed with them), changes only
``firmware/src``, says what it breaks on its ``# Mutant`` line, and the audit's
firmware mutants of 2026-11-22 are all there.

The same script runs the host package's mutants, ``host/mutants/*.patch``, each
by the command its ``# Kill:`` line gives (``host/tests/test_mutants.py`` holds
those patches and their commands). What is held here is the runner: in a
checkout made for the purpose it kills a planted host mutant, and a planted
survivor, a mutant its tests cannot run on, a kill command that fails on the
unmutated tree and a patch that names none each fail the run by name.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MUTANTS = ROOT / "scripts" / "mutants"
RUNNER = ROOT / "scripts" / "run_mutants.sh"

#: The tests audit's firmware mutants. Eight survived it (F2b, F3, F4, F5, F7,
#: F8, F9 and F10) and F2 was killed by a text guard alone; each must stay in
#: the list, killed by a host test.
AUDIT = ("F1", "F2", "F2b", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11")

#: The line of a patch's preamble that names it and says what it breaks.
HEADER = re.compile(r"^# Mutant (?P<name>[A-Za-z0-9-]+): (?P<what>.+)\.$", re.MULTILINE)

PATCHES = sorted(MUTANTS.glob("*.patch"))


def test_the_audit_mutants_are_listed() -> None:
    """None of the audit's firmware mutants has left the list."""
    names = {patch.stem for patch in PATCHES}
    missing = [name for name in AUDIT if name not in names]
    assert not missing, f"scripts/mutants/ lacks the audit's mutants {missing}"


@pytest.mark.parametrize("patch", PATCHES, ids=[patch.stem for patch in PATCHES])
def test_a_mutant_says_what_it_breaks_and_touches_only_the_firmware(patch: Path) -> None:
    """Its ``# Mutant`` line names it as its file does; it changes firmware/src alone."""
    text = patch.read_text(encoding="utf-8")
    lines = text.splitlines()
    header = HEADER.search(text)
    assert header, f"{patch.name}: no '# Mutant <name>: <what it breaks>.' line"
    assert header["name"] == patch.stem, f"{patch.name}: the header names {header['name']}"
    touched = [line.split(" b/", 1)[1] for line in lines if line.startswith("diff --git ")]
    assert touched, f"{patch.name}: no diff in it"
    outside = [path for path in touched if not path.startswith("firmware/src/")]
    assert not outside, f"{patch.name} changes {outside}: a mutant changes firmware/src only"


@pytest.mark.parametrize("patch", PATCHES, ids=[patch.stem for patch in PATCHES])
def test_a_mutant_applies_to_the_sources_as_they_are(patch: Path) -> None:
    """``git apply --check`` takes it: the lines it changes are still there."""
    done = subprocess.run(
        ["git", "apply", "--check", str(patch)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, (
        f"{patch.name} no longer applies ({done.stderr.strip()}): write it again against "
        f"the moved lines, or remove it with the code it broke"
    )


def test_the_runner_is_executable_and_documented() -> None:
    """The script runs as a command, and the testing page says what it does."""
    assert os.access(RUNNER, os.X_OK), "scripts/run_mutants.sh must be executable"
    testing = (ROOT / "docs" / "testing.md").read_text(encoding="utf-8")
    assert "scripts/run_mutants.sh" in testing


# --- The runner's self-test, on a checkout planted for it --------------------

#: The planted package's module, and the test its kill command runs.
MODULE = "def clamp(value, top):\n    return top if value > top else value\n"
SUITE = (
    "from planted import clamp\n\n\ndef test_clamp():\n"
    "    assert clamp(9, 5) == 5\n    assert clamp(3, 5) == 3\n"
)
KILL = "cd host && ${PYTHON:-python3} -m pytest -q -p no:cacheprovider test_planted.py"


def host_patch(name: str, new: str, kill: str | None = KILL) -> str:
    """A host mutant's patch: the planted module's second line becomes *new*."""
    header = f"# Mutant {name}: planted for the runner's self-test.\n"
    if kill is not None:
        header += f"# Kill: {kill}\n"
    old = MODULE.splitlines()[1]
    return (
        f"{header}\ndiff --git a/host/planted.py b/host/planted.py\n"
        "--- a/host/planted.py\n+++ b/host/planted.py\n"
        f"@@ -1,2 +1,2 @@\n def clamp(value, top):\n-{old}\n+{new}\n"
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A git checkout with the runner, a one-function host package and no firmware."""
    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    (root / "host" / "mutants").mkdir(parents=True)
    shutil.copy2(RUNNER, root / "scripts" / RUNNER.name)
    (root / "host" / "planted.py").write_text(MODULE, encoding="utf-8")
    (root / "host" / "test_planted.py").write_text(SUITE, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def run(root: Path, *names: str) -> subprocess.CompletedProcess[str]:
    """The planted checkout's runner, on the Python that runs this test."""
    return subprocess.run(
        ["sh", str(root / "scripts" / RUNNER.name), *names],
        env={**os.environ, "PYTHON": sys.executable, "TMPDIR": str(root.parent)},
        capture_output=True,
        text=True,
        check=False,
    )


def plant(root: Path, name: str, new: str, kill: str | None = KILL) -> None:
    (root / "host" / "mutants" / f"{name}.patch").write_text(
        host_patch(name, new, kill), encoding="utf-8"
    )


def test_the_runner_kills_a_planted_host_mutant_and_leaves_the_checkout(checkout: Path) -> None:
    plant(checkout, "planted-kill", "    return value")
    done = run(checkout)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "KILLED   planted-kill: FAILED test_planted.py::test_clamp" in done.stdout
    assert "== 1 mutant(s): 1 killed, 0 survived, 0 error(s)" in done.stdout
    assert (checkout / "host" / "planted.py").read_text(encoding="utf-8") == MODULE
    assert not list(checkout.parent.glob("meter-mutants.*")), "the copy was left behind"


def test_the_runner_fails_on_a_planted_host_survivor(checkout: Path) -> None:
    plant(checkout, "planted-kill", "    return value")
    plant(checkout, "planted-same", "    return top if value >= top else value")
    done = run(checkout, "--host")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "SURVIVED planted-same: planted for the runner's self-test." in done.stdout
    assert "== 2 mutant(s): 1 killed, 1 survived, 0 error(s)" in done.stdout


@pytest.mark.parametrize(
    ("new", "kill", "said"),
    [
        ("    return (", KILL, "the mutant does not build, or its tests do not run"),
        ("    return value", None, "one '# Kill:' line is asked"),
        ("    return value", f"{KILL}\n# Kill: {KILL}", "one '# Kill:' line is asked"),
        ("    return value", "exit 1", "its kill command fails on the unmutated tree: exit 1"),
    ],
    ids=["unrunnable", "no-kill-line", "two-kill-lines", "kills-anything"],
)
def test_the_runner_reports_what_it_cannot_judge(
    checkout: Path, new: str, kill: str | None, said: str
) -> None:
    """A mutant that does not import, or a command that would kill anything, is not a kill."""
    plant(checkout, "planted-bad", new, kill)
    done = run(checkout, "planted-bad")
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"ERROR    planted-bad: {said}" in done.stdout
    assert "== 1 mutant(s): 0 killed, 0 survived, 1 error(s)" in done.stdout


def test_the_runner_takes_the_pages_mutants_and_the_packages_apart(checkout: Path) -> None:
    """``--page`` is the mutants a browser judges, ``--package`` the others, ``--host`` both."""
    plant(checkout, "planted-kill", "    return value")
    plant(
        checkout, "planted-page", "    return 0", KILL.replace("&&", "&& METER_BROWSER_REQUIRED=1")
    )
    for selection, ran in (
        ("--page", {"planted-page"}),
        ("--package", {"planted-kill"}),
        ("--host", {"planted-page", "planted-kill"}),
    ):
        done = run(checkout, selection)
        assert done.returncode == 0, done.stdout + done.stderr
        assert set(re.findall(r"^KILLED   ([\w-]+):", done.stdout, re.MULTILINE)) == ran
        assert f"== {len(ran)} mutant(s): {len(ran)} killed" in done.stdout


@pytest.mark.parametrize("selection", ["--firmware", "--page", "--package", "--host"])
def test_the_runner_refuses_a_list_with_no_mutant(checkout: Path, selection: str) -> None:
    """A list that moved or emptied is not a list whose every mutant was killed."""
    done = run(checkout, selection)
    assert done.returncode == 2, done.stdout + done.stderr
    assert f"no mutant in {selection}" in done.stderr


def test_the_runner_reports_a_host_patch_that_does_not_apply(checkout: Path) -> None:
    plant(checkout, "planted-stale", "    return value")
    (checkout / "host" / "planted.py").write_text(MODULE.replace("top", "most"), encoding="utf-8")
    (checkout / "host" / "test_planted.py").write_text(SUITE, encoding="utf-8")
    done = run(checkout, "planted-stale")
    assert done.returncode == 1
    assert "ERROR    planted-stale: the patch does not apply" in done.stdout


def test_the_runner_refuses_a_mutant_it_does_not_have(checkout: Path) -> None:
    done = run(checkout, "nobody")
    assert done.returncode == 2
    assert "no mutant nobody" in done.stderr
