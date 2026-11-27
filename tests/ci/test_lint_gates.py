# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The commit hooks run in CI, each where it can, at the versions the tools pin.

``.pre-commit-config.yaml`` opens with the Kleidos quality kit's hooks, at a
release tag, and the third-party hooks the kit holds to its canonical list
(kleidos-dev/.github, docs/quality.md); this repository's own hooks follow.
CI runs every hook on every file in the kit's quick job, but the ones its
``skip`` input names: those need a toolchain or the host package, and a job of
``check.yml`` of this repository's own runs each instead. A hook skipped
there with no job, or a job named for a hook that is gone, fails here.
Beside them: the gitleaks binary the lint job downloads and the ruff the host
package's ``[dev]`` extra installs are the hooks' versions, every pytest run
of the repository turns a warning into an error, and the large-file hook reads
every file, which a CI checkout never stages.

Host-only: the files read as text (no YAML parser is a dependency here), and
one pytest run in a subprocess.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / ".pre-commit-config.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
RUN_TESTS = ROOT / "scripts" / "run_tests.sh"
PYTEST_INI = ROOT / "tests" / "pytest.ini"
HOST_PROJECT = ROOT / "host" / "pyproject.toml"

KIT = "https://github.com/kleidos-dev/.github"
#: The quality kit's hooks, every one of them in force here.
KIT_HOOKS = ("kleidos-kit", "kleidos-commit-msg", "kleidos-mypy")
#: The hooks of this repository the kit's quick job skips, each with the job
#: of check.yml that runs it instead.
SKIPPED_IN_QUICK = {
    "native-tests": "tests",
    "host-tests": "tests",
    "static-analysis": "static-analysis",
    "doxygen": "docs",
    "calibration-registry": "lint",
}


def hook_revs() -> dict[str, str]:
    """Each remote hook repository of the pre-commit configuration and its version.

    A rev pinned to a commit names its release in the ``# frozen:`` comment.
    """
    text = HOOKS.read_text(encoding="utf-8")
    found = re.findall(
        r"^  - repo: (\S+)\n    rev: (\S+)(?:\s+# frozen: (\S+))?$", text, re.MULTILINE
    )
    return {repo: frozen or rev for repo, rev, frozen in found}


def hook_block(hook_id: str) -> str:
    """The lines of the hook *hook_id*, up to the next hook or repository."""
    text = HOOKS.read_text(encoding="utf-8")
    match = re.search(
        rf"^      - id: {re.escape(hook_id)}\n((?:        .*\n)*)", text, re.MULTILINE
    )
    assert match, f"no {hook_id} hook"
    return match.group(1)


def hook_ids() -> list[str]:
    """Every hook id of the pre-commit configuration, in order."""
    return re.findall(r"^      - id: (\S+)$", HOOKS.read_text(encoding="utf-8"), re.MULTILINE)


def ci_job(name: str) -> str:
    """The job *name* of check.yml, as text."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(rf"^  {re.escape(name)}:\n((?:    .*\n|\n)+)", text, re.MULTILINE)
    assert match, f"check.yml has no {name} job"
    return match.group(1)


def test_the_kit_s_hooks_are_in_force_at_a_release() -> None:
    text = HOOKS.read_text(encoding="utf-8")
    entry = re.search(
        rf"^  - repo: {re.escape(KIT)}\n    rev: (v\d+\.\d+\.\d+)\n"
        r"    hooks:\n((?:      - id: \S+\n)+)",
        text,
        re.MULTILINE,
    )
    assert entry, "the kit's hook entry is missing, or not at a release tag"
    assert re.findall(r"- id: (\S+)", entry.group(2)) == list(KIT_HOOKS)
    assert "check_commit_msg.py" not in text, "the commit-message checker is the kit's hook"


def test_the_quick_job_skips_only_hooks_another_job_runs() -> None:
    """A commit made where the hooks are not installed still meets each of them (G1).

    The kit's quick job runs every hook on every file; a hook it skips names
    the job of check.yml that runs it instead.
    """
    skip = re.search(r'^      skip: "(\S+)"$', ci_job("quick"), re.MULTILINE)
    assert skip, "the quick job names no skip"
    assert set(skip.group(1).split(",")) == set(SKIPPED_IN_QUICK)
    for hook, runner in SKIPPED_IN_QUICK.items():
        assert hook in hook_ids(), f"skip names {hook}, which is no hook"
        assert not set(KIT_HOOKS) & {hook}, f"{hook}: a kit hook is never skipped"
        ci_job(runner)
    assert "scripts/check_calibration_registry.py" in ci_job("lint")
    assert "run: scripts/run_tests.sh" in ci_job("tests")


def test_ci_runs_the_gitleaks_rules_at_the_hook_s_version() -> None:
    job = ci_job("lint")
    found = re.search(r"^          GITLEAKS_VERSION: (\S+)$", job, re.MULTILINE)
    assert found, "the lint job sets no GITLEAKS_VERSION"
    assert found.group(1) == hook_revs()["https://github.com/gitleaks/gitleaks"].removeprefix("v")
    assert re.search(r"^          GITLEAKS_SHA256: [0-9a-f]{64}$", job, re.MULTILINE)
    assert "tests/ci/test_gitleaks_rules.py" in job


def test_the_host_package_s_ruff_is_the_hook_s() -> None:
    ruff = hook_revs()["https://github.com/astral-sh/ruff-pre-commit"].removeprefix("v")
    assert f'"ruff=={ruff}"' in HOST_PROJECT.read_text(encoding="utf-8")


def test_the_test_job_checks_out_the_whole_history() -> None:
    """The ratchets that only fall read every version of their file; a shallow clone has one."""
    job = ci_job("tests")
    assert "run: scripts/run_tests.sh" in job, "check.yml has no test job"
    assert re.search(r"^          fetch-depth: 0$", job, re.MULTILINE), job


def test_every_pytest_run_turns_a_warning_into_an_error() -> None:
    script = RUN_TESTS.read_text(encoding="utf-8")
    runs = re.findall(r'"\$python" -m pytest ([^\n]*)', script)
    assert len(runs) == 2 and all("-W error" in run for run in runs), runs
    hook = hook_block("host-tests")
    assert hook.count("-m pytest -q -W error") == 2, hook


def test_the_guards_configuration_fails_a_test_that_warns(tmp_path: Path) -> None:
    test = tmp_path / "test_warns.py"
    test.write_text(
        "import warnings\n\n\ndef test_warns():\n    warnings.warn('planted', UserWarning)\n",
        encoding="utf-8",
    )
    planted = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            str(PYTEST_INI),
            str(test),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert planted.returncode == 1, planted.stdout
    assert "UserWarning: planted" in planted.stdout


def test_the_large_file_limit_holds_on_every_file_in_ci() -> None:
    """``check-added-large-files`` reads only staged files unless told otherwise,
    which on CI's checkout is none."""
    block = hook_block("check-added-large-files")
    assert "--maxkb=512" in block and "--enforce-all" in block, block
