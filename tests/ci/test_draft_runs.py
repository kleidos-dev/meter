# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""A draft pull request runs only the cheap jobs; the ready head runs the rest.

CI's first stage (the kit's quick and check jobs, ``📋 What changed`` and the
repository's rules) takes minutes; everything after it takes the better part
of an hour. A pull request pushed again and again while it is iterated on must
not pay for that each time, so ``📋 What changed`` reports every heavy family
false on a draft and the tests job skips. A draft's run is no verdict, and
nothing in GitHub stops a draft head from being fast-forwarded to ``main``
(no protection, no ruleset), so ``🙋 Draft`` fails on a draft and ``✅ Check``
fails with it: no draft head carries a green gate. Marking the pull request
ready for review runs the whole of it (``pull_request`` includes
``ready_for_review``), and so does every later push to a ready pull request.
A skip never satisfies the gate on a ready pull request: the only jobs outside
the path filter that may skip are the tests job, only on ``draft == 'true'``,
and ``🙋 Draft``, only on ``draft != 'true'``.

Host-only: the workflow read as text, and the filter's draft block run in bash.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")

#: The heavy families ``📋 What changed`` decides, by output name.
FAMILIES = ("firmware", "docs", "panel", "schematic")


def job(name: str) -> str:
    """The text of the job *name*."""
    body = TEXT[TEXT.index("\njobs:\n") :]
    match = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][a-z0-9_-]*:\n|\Z)", body, re.M | re.S)
    assert match, name
    return match.group(1)


def test_the_run_starts_on_ready_for_review_and_the_default_events() -> None:
    types = re.search(r"^  pull_request:\n    types: \[(.+)\]$", TEXT, re.M)
    assert types
    assert set(types.group(1).split(", ")) == {
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
    }


def test_the_draft_state_reaches_the_filter_and_is_an_output() -> None:
    assert "DRAFT: ${{ github.event.pull_request.draft == true }}" in job("changes")
    assert "draft: ${{ steps.paths.outputs.draft }}" in job("changes")


def test_the_tests_job_skips_on_a_draft_and_runs_otherwise() -> None:
    block = job("tests")
    assert "needs: [changes, lint]" in block
    assert "if: needs.changes.outputs.draft != 'true'" in block


def test_every_filtered_job_reads_the_filter_so_a_draft_turns_it_off() -> None:
    for name in ("panel", "docs", "static-analysis", "native32", "firmware", "schematic"):
        block = job(name)
        needs = re.search(r"^    needs: (.+)$", block, re.M)
        assert needs and "changes" in needs.group(1), name
        assert re.search(r"^    if: needs\.changes\.outputs\.\w+ == 'true'", block, re.M), name


def test_a_draft_turns_every_family_off_and_a_ready_head_leaves_them_alone() -> None:
    block = re.search(
        r"^( *)# A draft pull request runs no heavy family.*?\n\1fi\n", job("changes"), re.M | re.S
    )
    assert block, "the filter has no draft block"
    script = "\n".join(line.lstrip() for line in block.group(0).splitlines())
    for draft, expected in (("true", "false"), ("false", "true")):
        done = subprocess.run(
            [
                "bash",
                "-c",
                f"DRAFT={draft}; firmware=true docs=true panel=true schematic=true\n"
                f"{script}\n" + "; ".join(f'echo "{f}=${f}"' for f in FAMILIES),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert done.stdout.split() == [f"{family}={expected}" for family in FAMILIES], draft


def test_the_gate_lets_only_the_tests_job_skip_beside_the_filtered_ones() -> None:
    may = re.search(r"^          MAY_SKIP: (.+)$", job("done"), re.M)
    assert may
    assert set(may.group(1).split()) == {
        "tests",
        "panel",
        "docs",
        "static-analysis",
        "native32",
        "firmware",
        "schematic",
        "draft",
    }
    # The skip of the tests job is a draft's alone: no other condition on it.
    assert len(re.findall(r"^    if: ", job("tests"), re.M)) == 1


def test_a_draft_fails_the_gate_and_a_ready_head_skips_the_draft_job() -> None:
    """A draft head never carries a green ``✅ Check``: ``🙋 Draft`` fails on it."""
    block = job("draft")
    assert "    needs: [changes, lint]\n" in block
    assert re.findall(r"^    if: (.+)$", block, re.M) == ["needs.changes.outputs.draft == 'true'"]
    run = re.search(r"^        run: \|\n((?:          .*\n)+)", block, re.M)
    assert run, "the draft job has no script"
    script = "\n".join(line[10:] for line in run.group(1).splitlines())
    done = subprocess.run(["bash", "-e", "-c", script], check=False, capture_output=True, text=True)
    assert done.returncode != 0 and "::error title=Draft pull request::" in done.stdout
    needs = re.search(r"^    needs: \[(.+)\]$", job("done"), re.M)
    assert needs and "draft" in needs.group(1).split(", ")


def test_the_gate_judges_a_draft_as_failed_and_a_ready_run_as_passed() -> None:
    """The kit's gate script, run on the results of a draft's run and of a ready run."""
    gate = job("done")
    script = re.search(r"^        run: \|\n((?:          .*\n)+)", gate, re.M)
    may = re.search(r"^          MAY_SKIP: (.+)$", gate, re.M)
    needed = re.search(r"^    needs: \[(.+)\]$", gate, re.M)
    assert script and may and needed
    body = "\n".join(line[10:] for line in script.group(1).splitlines())
    body = body.replace("export PATH=/usr/bin:/bin HOME=/nonexistent", "")
    jobs = needed.group(1).split(", ")
    may_skip = may.group(1)
    heavy = {"tests", "panel", "docs", "static-analysis", "native32", "firmware", "schematic"}

    def verdict(results: dict[str, str]) -> int:
        needs = json.dumps({name: {"result": results[name]} for name in jobs})
        env = {
            "NEEDS": needs,
            "MAY_SKIP": may_skip,
            "GITHUB_STEP_SUMMARY": "/dev/null",
            "PATH": os.environ["PATH"],
        }
        return subprocess.run(
            ["bash", "-c", body], env=env, check=False, capture_output=True
        ).returncode

    draft = {name: "skipped" if name in heavy else "success" for name in jobs}
    draft["draft"] = "failure"
    assert verdict(draft) != 0, "a draft's run made the gate green"
    ready = dict.fromkeys(jobs, "success")
    ready["draft"] = "skipped"
    assert verdict(ready) == 0
    ready["tests"] = "skipped"
    assert verdict(ready) == 0, "MAY_SKIP is the kit's word; the tests job's condition is the guard"


def test_codeql_skips_a_draft_and_runs_on_ready_for_review() -> None:
    text = (ROOT / ".github" / "workflows" / "codeql.yml").read_text(encoding="utf-8")
    types = re.search(r"^  pull_request:\n    types: \[(.+)\]$", text, re.M)
    assert types and "ready_for_review" in types.group(1).split(", ")
    assert "    if: github.event.pull_request.draft != true\n" in text
