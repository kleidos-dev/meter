# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The gate workflow keeps the project's CI convention.

``.github/workflows/check.yml`` is the workflow ``CI``: every job carries a
quoted ``"<emoji> <description>"`` name, every job of this repository's own a
timeout, every checkout keeps no credentials, a new push cancels the
superseded run, and every job past the first stage reaches the quality kit's
quick job, and this repository's lint, through ``needs:``, so that a failure
there skips the rest of the run. The heavy families run only when ``📋 What
changed`` says a path they read changed. The kit's jobs are called by name at
its branch ``v3`` (kleidos-dev/.github, docs/quality.md), and ``✅ Check``,
last, needs every other job: the kit's own check holds that job to its model.

The advisory workflows beside it (``codeql.yml``, ``security.yml``, the
Kleidos firmware repository's CodeQL and composition scans) keep the same
naming, bounds and checkouts, read-only permissions and a cancelled
superseded run; and every action any workflow uses is pinned to a commit,
with the release it is beside a comment.

Host-only: the workflow read as text (no YAML parser is a dependency here).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
WORKFLOW = WORKFLOWS / "check.yml"
#: The advisory workflows: a schedule of their own, nothing waits for them.
ADVISORY = ("codeql.yml", "security.yml")

#: Jobs of the first stage: no toolchain, no needs.
FIRST_STAGE = {"changes", "quick"}
#: The quality kit's entry workflows, called by name at its branch.
KIT_CALLERS = {
    "quick": "kleidos-dev/.github/.github/workflows/quick.yml@v3",
    "check": "kleidos-dev/.github/.github/workflows/check.yml@v3",
}
#: The kit's gate, the one required check: it needs every other job.
GATE = "done"
#: Jobs that run only when their family changed, and the output they test.
FILTERED = {
    "static-analysis": "firmware",
    "docs": "firmware",
    "native32": "firmware",
    "firmware": "firmware",
    "panel": "panel",
    "schematic": "schematic",
}
EMOJI = "⚡📋🧹✍️🔎🧪🔨🧫🧭📚📊🛡️🚀📡📦✅🙋"


def _jobs(workflow: Path = WORKFLOW) -> dict[str, str]:
    text = workflow.read_text(encoding="utf-8")
    body = text[text.index("\njobs:\n") + len("\njobs:\n") :]
    parts = re.split(r"^  ([a-z0-9_-]+):\n", body, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_the_workflow_is_ci_and_cancels_a_superseded_run() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"^name: CI$", text, re.MULTILINE)
    assert re.search(r"^  group: ci-\$\{\{ github\.ref \}\}", text, re.MULTILINE)
    assert re.search(r"^  cancel-in-progress: true$", text, re.MULTILINE)
    assert "\non:\n  push:\n    branches: [main]\n" in text
    assert re.search(r"^  pull_request:\n    types: \[[^\]]*\bready_for_review\b", text, re.M)
    assert "paths:" not in text and "paths-ignore:" not in text, (
        "filter per job, in 📋 What changed"
    )


def test_every_job_is_named_and_bounded() -> None:
    jobs = _jobs()
    assert FIRST_STAGE | set(FILTERED) | {"lint", "tests", *KIT_CALLERS, GATE} <= set(jobs)
    _named_and_bounded(jobs)


def test_the_kit_is_called_by_name_and_the_gate_needs_every_job() -> None:
    jobs = _jobs()
    for job, called in KIT_CALLERS.items():
        assert re.search(rf"^    uses: {re.escape(called)}$", jobs[job], re.MULTILINE), job
    assert list(jobs)[-1] == GATE, "✅ Check is the last job"
    needs = re.search(r"^    needs: \[(.+)\]$", jobs[GATE], re.MULTILINE)
    assert needs and set(needs.group(1).split(", ")) == set(jobs) - {GATE}
    may_skip = re.search(r"^          MAY_SKIP: (.+)$", jobs[GATE], re.MULTILINE)
    # The filtered families, the tests job, which a draft pull request skips,
    # and the draft job, which runs on a draft alone (tests/ci/test_draft_runs.py).
    assert may_skip and set(may_skip.group(1).split()) == set(FILTERED) | {"tests", "draft"}


def _named_and_bounded(jobs: dict[str, str]) -> None:
    for job, block in jobs.items():
        name = re.search(r'^    name: "(.+)"$', block, re.MULTILINE)
        assert name, f"{job}: no quoted name"
        kind = name.group(1).split(" ", 1)[0]
        assert kind in EMOJI, f"{job}: name does not start with a kind emoji"
        if re.search(r"^    uses: ", block, re.MULTILINE):
            continue  # a called workflow bounds its own jobs
        assert re.search(r"^    timeout-minutes: \d+$", block, re.MULTILINE), f"{job}: no timeout"
        for checkout in re.finditer(r"uses: actions/checkout@\S+.*\n((?:\s{8,}.*\n)*)", block):
            assert "persist-credentials: false" in checkout.group(1), (
                f"{job}: checkout keeps credentials"
            )


def test_every_later_job_needs_the_cheap_gate() -> None:
    for job, block in _jobs().items():
        needs = re.search(r"^    needs: (.+)$", block, re.MULTILINE)
        if job in FIRST_STAGE:
            assert needs is None, f"{job} is a first-stage gate and needs nothing"
            continue
        if job in ("lint", "check"):
            assert needs and needs.group(1) == "quick", f"{job} does not need the kit's quick job"
            continue
        assert needs and "lint" in needs.group(1), f"{job} does not need lint"
        if job in FILTERED:
            assert "changes" in needs.group(1)
            assert f"if: needs.changes.outputs.{FILTERED[job]} == 'true'" in block, job


def test_the_advisory_workflows_keep_the_convention() -> None:
    for name in ADVISORY:
        workflow = WORKFLOWS / name
        text = workflow.read_text(encoding="utf-8")
        jobs = _jobs(workflow)
        assert jobs, name
        _named_and_bounded(jobs)
        assert re.search(r"^  schedule:\n    - cron: ", text, re.MULTILINE), f"{name}: no schedule"
        assert re.search(r"^  cancel-in-progress: true$", text, re.MULTILINE), name
        permissions = re.search(r"^permissions:\n((?:  .*\n)+)", text, re.MULTILINE)
        assert permissions and "  contents: read\n" in permissions.group(1), name
        assert "write" not in permissions.group(1), f"{name}: writes nothing, uploads nothing"
        assert "upload-sarif" not in text, f"{name}: code scanning is off here; keep an artifact"


def test_codeql_analyses_the_device_build() -> None:
    text = (WORKFLOWS / "codeql.yml").read_text(encoding="utf-8")
    init, build, analyze = (
        text.index("github/codeql-action/init@"),
        text.index("run: pio run --project-dir firmware -e meter\n"),
        text.index("github/codeql-action/analyze@"),
    )
    assert init < build < analyze, "CodeQL traces the build between init and analyze"
    assert "languages: c-cpp" in text and "build-mode: manual" in text
    assert "upload: never" in text


def test_every_action_is_pinned_to_a_commit() -> None:
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for line in workflow.read_text(encoding="utf-8").splitlines():
            used = re.search(r"uses: (\S+)", line)
            if not used or used.group(1).startswith("kleidos-dev/.github/"):
                continue  # the quality kit, called by name at its branch (docs/quality.md)
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", used.group(1)), (
                f"{workflow.name}: {used.group(1)} is not pinned to a commit"
            )
            assert re.search(r"@[0-9a-f]{40} # v\d", line), f"{workflow.name}: {line.strip()}"
