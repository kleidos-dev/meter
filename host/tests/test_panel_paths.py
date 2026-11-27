# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""How serve names a local file to the page and the API (:mod:`~kleidos_meter.panel.paths`).

Relative to the working directory when the file is inside it, then to the
repository, else by its last two components; never an absolute path, never
the home directory spelled out.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from kleidos_meter.panel import paths
from kleidos_meter.panel.paths import PROJECT, TAIL, shown_error, shown_path
from kleidos_meter.registry import ROOT as REGISTRY_ROOT


def test_a_path_inside_a_base_is_shown_relative_to_the_first_that_holds_it(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    inner = work / "inner"
    elf = inner / "build" / "firmware.elf"
    assert shown_path(elf, [work, inner], home=tmp_path / "h") == "inner/build/firmware.elf"
    assert shown_path(elf, [inner, work], home=tmp_path / "h") == "build/firmware.elf"
    assert shown_path(work, [work], home=tmp_path / "h") == "."


def test_a_path_outside_every_base_keeps_its_last_two_components(tmp_path: Path) -> None:
    elf = tmp_path / "far" / "away" / "build" / "firmware.elf"
    shown = shown_path(elf, [tmp_path / "elsewhere"], home=tmp_path / "h")
    assert shown == "build/firmware.elf" and TAIL == 2
    assert shown_path(Path("/"), [tmp_path], home=tmp_path / "h") == "."
    assert shown_path(Path("/firmware.elf"), [tmp_path], home=tmp_path / "h") == "firmware.elf"


def test_the_home_directory_is_never_spelled_out(tmp_path: Path) -> None:
    home = tmp_path / "home" / "someone"
    elf = home / "firmware.elf"
    # The root and the directory above the home hold it: they are not used as bases.
    assert shown_path(elf, [Path("/"), home.parent], home=home) == "firmware.elf"
    deep = home / "a" / "b" / "c" / "firmware.elf"
    assert shown_path(deep, [], home=home) == "c/firmware.elf"
    # The home directory itself is a base like any other.
    assert shown_path(deep, [home], home=home) == "a/b/c/firmware.elf"


def test_a_relative_path_is_read_from_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert shown_path("build/firmware.elf", home=tmp_path / "h") == "build/firmware.elf"
    assert shown_path(Path("build") / "x.elf") == "build/x.elf"


def test_by_default_the_working_directory_comes_before_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert REGISTRY_ROOT.parent == PROJECT
    built = PROJECT / "firmware" / ".pio" / "build" / "meter" / "firmware.elf"
    monkeypatch.chdir(tmp_path)
    assert shown_path(built) == "firmware/.pio/build/meter/firmware.elf"
    monkeypatch.chdir(PROJECT / "firmware")
    assert shown_path(built) == ".pio/build/meter/firmware.elf"


def test_an_os_error_names_its_file_as_shown(tmp_path: Path) -> None:
    missing = tmp_path / "runs" / "meta.json"
    with pytest.raises(OSError) as caught:
        missing.read_text(encoding="utf-8")
    assert str(tmp_path) in str(caught.value)
    assert shown_error(caught.value, [tmp_path]) == "No such file or directory: runs/meta.json"
    named = OSError(errno.EACCES, os.strerror(errno.EACCES), os.fsencode(missing))
    assert shown_error(named, [tmp_path]) == "Permission denied: runs/meta.json"
    reasonless = OSError(None, None, str(missing))
    assert shown_error(reasonless, [tmp_path / "runs"]) == "OSError: meta.json"
    assert shown_error(OSError("read-only")) == "read-only"
    assert paths.__all__ == ["PROJECT", "shown_error", "shown_path"]
