# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
#
# PlatformIO pre-build script: a changed firmware/version.txt reaches the image.
#
# ESP-IDF reads version.txt when CMake configures the project and stamps it into
# esp_app_desc_t.version (PROJECT_VER), which is what `*IDN?` and
# `SYSTem:INFormation?` report as `fw=`. CMakeLists.txt lists the file as a
# configure dependency, but PlatformIO's espidf builder never runs CMake's own
# build step: it configures only when build.ninja is missing or older than the
# two CMakeLists.txt, sdkconfig, sdkconfig.defaults, dependencies.lock or the
# framework (is_cmake_reconfigure_required() in builder/frameworks/espidf.py),
# and version.txt is none of them. An incremental build after a version bump
# therefore kept the old PROJECT_VER: on 2026-11-20 a meter_relay2 image built
# from 0.6.1 sources in an existing build tree reported fw=0.5.0.
#
# So before the builder decides, this script compares version.txt with the
# version the build tree was configured with (project_description.json, which
# the configure step writes) and, when they differ, deletes build.ninja: the
# builder then configures again, the new PROJECT_VER reaches esp_app_desc.c's
# compile line, and SCons recompiles it because its command line changed.
# A tree that was never configured is left alone (the builder configures it).

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    #: What PlatformIO's SCons hands the script: its construction environment.
    env: Any

    def Import(*names: str) -> None:
        """SCons's import of a name from the calling SConscript."""


def configured_version(build_dir: Path) -> str | None:
    """The version the build tree was configured with, or None when unknown."""
    try:
        description = json.loads((build_dir / "project_description.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None
    version = description.get("project_version")
    return version if isinstance(version, str) else None


def stale_configuration(build_dir: Path, version_file: Path) -> bool:
    """True when the tree is configured and not with version.txt's version."""
    if not (build_dir / "build.ninja").is_file():
        return False
    wanted = version_file.read_text(encoding="utf-8").strip()
    return configured_version(build_dir) != wanted


def force_reconfigure(build_dir: Path, version_file: Path) -> bool:
    """Deletes build.ninja when the configuration is stale; True if it did."""
    if not stale_configuration(build_dir, version_file):
        return False
    (build_dir / "build.ninja").unlink()
    return True


try:
    Import("env")
except NameError:  # imported by the host tests
    env = None

if env is not None:
    _build = Path(env.subst("$BUILD_DIR"))
    _version = Path(env.subst("$PROJECT_DIR")) / "version.txt"
    if force_reconfigure(_build, _version):
        print(f"version.txt is {_version.read_text().strip()}: reconfiguring the ESP-IDF project")
