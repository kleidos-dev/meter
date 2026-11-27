# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""A changed ``firmware/version.txt`` reaches an incremental device build.

ESP-IDF stamps ``version.txt`` into ``esp_app_desc_t.version`` when CMake
configures the project, and PlatformIO's espidf builder re-runs that step only
when a ``CMakeLists.txt``, ``sdkconfig``, ``sdkconfig.defaults``, the
dependency lock or the framework changed: on 2026-11-20 a ``meter_relay2``
image built from 0.6.1 sources in an existing build tree reported
``fw=0.5.0``. ``firmware/scripts/meter_version_reconfigure.py`` (a ``pre:``
extra script) deletes ``build.ninja`` when the tree was configured with
another version, so the builder configures again. These tests hold its
decision on fake build trees, and that ``platformio.ini`` runs it before the
build; CI's ``firmware`` job proves the whole path on a real incremental
build.

Host-only: the script imported as a module, temporary directories.
"""

from __future__ import annotations

import configparser
import importlib.util
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "firmware" / "scripts" / "meter_version_reconfigure.py"
METER_INI = ROOT / "firmware" / "platformio.ini"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("meter_version_reconfigure", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path: Path, configured: str | None, version: str = "0.6.1") -> tuple[Path, Path]:
    build = tmp_path / "build"
    build.mkdir()
    (build / "build.ninja").write_text("", encoding="utf-8")
    if configured is not None:
        (build / "project_description.json").write_text(
            json.dumps({"project_version": configured}), encoding="utf-8"
        )
    version_file = tmp_path / "version.txt"
    version_file.write_text(version + "\n", encoding="utf-8")
    return build, version_file


def test_a_tree_configured_with_another_version_is_reconfigured(tmp_path: Path) -> None:
    build, version = _tree(tmp_path, configured="0.5.0")
    assert _module().force_reconfigure(build, version)
    assert not (build / "build.ninja").exists()


def test_a_tree_configured_with_this_version_is_left_alone(tmp_path: Path) -> None:
    build, version = _tree(tmp_path, configured="0.6.1")
    assert not _module().force_reconfigure(build, version)
    assert (build / "build.ninja").exists()


def test_a_tree_whose_version_is_unknown_is_reconfigured(tmp_path: Path) -> None:
    build, version = _tree(tmp_path, configured=None)
    assert _module().force_reconfigure(build, version)
    (build / "build.ninja").write_text("", encoding="utf-8")
    (build / "project_description.json").write_text("not json", encoding="utf-8")
    assert _module().force_reconfigure(build, version)


def test_a_tree_never_configured_is_left_to_the_builder(tmp_path: Path) -> None:
    build, version = _tree(tmp_path, configured="0.5.0")
    (build / "build.ninja").unlink()
    assert not _module().force_reconfigure(build, version)


def test_platformio_runs_it_before_the_build() -> None:
    ini = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))
    ini.read_string(METER_INI.read_text(encoding="utf-8"))
    scripts = ini["env:meter"]["extra_scripts"].split()
    assert "pre:scripts/meter_version_reconfigure.py" in scripts


def test_ci_rebuilds_incrementally_after_a_version_change() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "firmware/version.txt" in text and "app_version" in text
