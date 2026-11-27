# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every path a document writes in code is in this tree, or says whose it is.

``scripts/check_doc_paths.py`` is the gate (pre-commit runs it on the Markdown
that changed, CI's lint job on the whole tree); this holds it to the tree as it
stands, to its own self-test, and to a dead path planted in a real document,
so a gate that stopped seeing one would fail here too.

Host-only: ``git ls-files`` and the files read as text.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_doc_paths.py"


def _gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_doc_paths", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_every_document_passes() -> None:
    run = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert ", 0 finding(s)" in run.stdout


def test_the_self_test_judges_every_case() -> None:
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--selftest"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "0 judged wrongly" in run.stdout


def test_a_dead_path_planted_in_a_real_document_fails() -> None:
    gate = _gate()
    tree = gate.Tree.of(ROOT)
    document = "docs/usage.md"
    text = (ROOT / document).read_text(encoding="utf-8")
    assert gate.check_text(document, text, tree) == []
    planted = text + "\nThe rig's notes are in `docs/rig-notes.md` and `scripts/pio-job`.\n"
    found = [finding.path for finding in gate.check_text(document, planted, tree)]
    assert found == ["docs/rig-notes.md", "scripts/pio-job"]
    qualified = text + "\nThe Kleidos firmware repository's `scripts/pio-job` runs pio.\n"
    assert gate.check_text(document, qualified, tree) == []


def test_every_record_is_tracked() -> None:
    gate = _gate()
    assert gate.stale_records(gate.Tree.of(ROOT)) == []


def test_pre_commit_runs_the_gate() -> None:
    config = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "entry: python3 scripts/check_doc_paths.py" in config
