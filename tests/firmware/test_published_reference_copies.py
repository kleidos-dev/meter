# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The published site's page modules are the firmware repository's, but for one recorded patch.

``scripts/build_reference.py`` shapes each page, writes its links at the API
host's clean addresses and converts it to its Markdown twin with three modules
of the Kleidos firmware repository's own build of its published reference,
copied into ``scripts/published_reference/`` and recorded in its
``SOURCE.json`` (source, commit, and for each file its path there, the
SHA-256 of its bytes there and of the copy here): the two references on one
host stay one design only while the code that shapes them is one. The copies
differ from their source by ``typing.patch`` alone, two lines this
repository's strict mypy asks for. So each copy's bytes are what the manifest
records, the patch undone gives the source's bytes, and nothing else is in the
folder. With that repository checked out beside this one (or named by
``KLEIDOS_FIRMWARE_REPO``), the recorded commit is read there and must hold
those bytes.

Host-only: the files read as bytes, ``git apply`` in a temporary folder, and
``git show`` when the source is present.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FOLDER = REPO_ROOT / "scripts" / "published_reference"
MANIFEST = FOLDER / "SOURCE.json"


def _manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return manifest


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_each_copy_is_the_bytes_the_manifest_records() -> None:
    wrong = [
        entry["path"]
        for entry in _manifest()["files"].values()
        if _sha((REPO_ROOT / entry["path"]).read_bytes()) != entry["sha256"]
    ]
    assert not wrong, (
        "not the copies scripts/published_reference/SOURCE.json records (never edit them here;"
        " copy them from the firmware repository, apply typing.patch and update the manifest): "
        + ", ".join(wrong)
    )


def test_the_patch_undone_gives_the_source_s_bytes(tmp_path: Path) -> None:
    manifest = _manifest()
    for entry in manifest["files"].values():
        shutil.copy2(REPO_ROOT / entry["path"], tmp_path / Path(entry["path"]).name)
    undone = subprocess.run(
        ["git", "apply", "-R", "--unsafe-paths", "-p1", str(REPO_ROOT / manifest["patch"])],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert undone.returncode == 0, undone.stderr
    wrong = [
        entry["path"]
        for entry in manifest["files"].values()
        if _sha((tmp_path / Path(entry["path"]).name).read_bytes()) != entry["upstream_sha256"]
    ]
    assert not wrong, "with typing.patch undone, not the source's bytes: " + ", ".join(wrong)


def test_the_folder_holds_the_copies_and_nothing_else() -> None:
    manifest = _manifest()
    named = {Path(entry["path"]).name for entry in manifest["files"].values()}
    named |= {"SOURCE.json", Path(manifest["patch"]).name}
    held = {path.name for path in FOLDER.iterdir() if path.is_file()}
    assert held == named, (
        f"in the folder, not in SOURCE.json: {sorted(held - named)};"
        f" missing: {sorted(named - held)}"
    )


def test_the_manifest_names_a_commit_and_a_source() -> None:
    manifest = _manifest()
    assert len(manifest["commit"]) == 40 and int(manifest["commit"], 16) >= 0
    assert manifest["source"].startswith("https://github.com/")
    names = {f"scripts/{Path(e['path']).name}" for e in manifest["files"].values()}
    assert set(manifest["files"]) == names


def _source_checkout() -> Path | None:
    named = os.environ.get("KLEIDOS_FIRMWARE_REPO")
    candidates = [Path(named)] if named else [REPO_ROOT.parent / "kleidos"]
    return next((path for path in candidates if (path / ".git").exists()), None)


def test_the_recorded_commit_holds_the_source_s_bytes() -> None:
    source = _source_checkout()
    if source is None:
        pytest.skip("the firmware repository is not checked out beside this one")
    manifest = _manifest()
    for there, entry in manifest["files"].items():
        shown = subprocess.run(
            ["git", "-C", str(source), "show", f"{manifest['commit']}:{there}"],
            capture_output=True,
            check=False,
        )
        if shown.returncode != 0:
            pytest.skip(f"commit {manifest['commit'][:12]} is not in {source} (fetch it)")
        assert _sha(shown.stdout) == entry["upstream_sha256"], there
