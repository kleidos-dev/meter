# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The ESP-IDF installer CI builds the images with is the firmware repository's.

``scripts/install_idf.sh`` installs the pinned ESP-IDF release as a local
PlatformIO framework package and patches the platform's builder for it; the
``firmware`` job of ``.github/workflows/check.yml`` runs it before building
the image. It is a byte copy of the Kleidos firmware repository's script,
recorded in ``scripts/install_idf.json`` (source, path, commit, SHA-256) the
way ``hardware/kicad/KIT.json`` records the KiCad kit: a hand edit here fails
the digest, and a copy whose release is not the one ``firmware/platformio.ini``
pins fails the version check. With that repository checked out beside this
one (or named by ``KLEIDOS_FIRMWARE_REPO``), the recorded commit is also read
there and must hold the same bytes.

Host-only: two files read as text, and ``git show`` when the source is present.
"""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "install_idf.sh"
MANIFEST = REPO_ROOT / "scripts" / "install_idf.json"
METER_INI = REPO_ROOT / "firmware" / "platformio.ini"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "check.yml"


def _manifest() -> dict[str, str]:
    manifest: dict[str, str] = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return manifest


def test_the_copy_is_the_bytes_the_manifest_records() -> None:
    digest = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
    assert digest == _manifest()["sha256"], (
        "scripts/install_idf.sh is not the copy scripts/install_idf.json records: "
        "never edit it here; copy it from the firmware repository and update the manifest"
    )


def test_the_copy_installs_the_framework_the_project_pins() -> None:
    tag = re.search(r'IDF_TAG="\$\{IDF_TAG:-v(\d+)\.(\d+)\.(\d+)\}"', SCRIPT.read_text())
    assert tag, "IDF_TAG default not found in scripts/install_idf.sh"
    major, minor, patch = (int(part) for part in tag.groups())
    package = f"4.{major * 10000 + minor * 100 + patch}.0"
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(METER_INI, encoding="utf-8")
    pin = ini["env:meter"]["platform_packages"].strip()
    assert pin == f"platformio/framework-espidf @ ~{package}", (
        f"platformio.ini pins {pin!r}; the installer copy installs {package}"
    )


def test_ci_builds_both_images_with_the_copy() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/install_idf.sh" in text
    for env in ("meter",):
        assert re.search(rf"pio run --project-dir firmware\b.*-e {env}\b", text), (
            f"check.yml does not build -e {env}"
        )


def _source_checkout() -> Path | None:
    named = os.environ.get("KLEIDOS_FIRMWARE_REPO")
    candidate = Path(named) if named else REPO_ROOT.parent / "kleidos"
    return candidate if (candidate / ".git").exists() else None


def test_the_recorded_commit_holds_the_same_bytes() -> None:
    source = _source_checkout()
    if source is None:
        pytest.skip("the firmware repository is not checked out beside this one")
    manifest = _manifest()
    shown = subprocess.run(
        ["git", "-C", str(source), "show", f"{manifest['commit']}:{manifest['path']}"],
        capture_output=True,
        check=False,
    )
    if shown.returncode != 0:
        pytest.skip(f"commit {manifest['commit'][:12]} is not in {source} (fetch it)")
    assert hashlib.sha256(shown.stdout).hexdigest() == manifest["sha256"]
