# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The local build's ``elf=``, which the Health view holds the instrument's to.

The digest is the first 16 hex digits of the ELF file's SHA-256, as the
instrument reports it (``docs/scpi.md``, ``SYSTem:INFormation?``); it is taken
again only when the file's size or modification time changed. ``GET
/api/health`` carries the comparison, and ``serve --elf`` names the build,
the repository's ``meter`` build by default.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from kleidos_meter.panel import local_elf as local_elf_module
from kleidos_meter.panel import serve
from kleidos_meter.panel.local_elf import DIGITS, LocalElf, elf_digest
from kleidos_meter.panel.paths import shown_path
from kleidos_meter.registry import ROOT as REGISTRY_ROOT
from test_panel_http import serving_panel

#: The simulated instrument's ``elf=``.
SIM_ELF = "5e1f0ba47c2291d3"


def test_the_digest_is_the_first_sixteen_hex_digits_of_the_files_sha256(tmp_path: Path) -> None:
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"\x7fELF" + bytes(range(256)) * 9000)  # more than one read
    assert elf_digest(elf) == hashlib.sha256(elf.read_bytes()).hexdigest()[:16]
    assert DIGITS == 16


def test_a_build_matches_or_not_and_says_which(tmp_path: Path) -> None:
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"one build")
    local = LocalElf(elf)
    mine = elf_digest(elf)
    same = local.compare(mine)
    assert same["matches"] is True and same["elf"] == mine and same["instrument"] == mine
    shown = shown_path(elf)
    assert shown == f"{tmp_path.name}/firmware.elf" and same["path"] == shown
    assert same["detail"] == f"the instrument runs the build at {shown}"
    other = local.compare(SIM_ELF)
    assert other["matches"] is False
    assert other["detail"] == f"the instrument runs another image than the build at {shown}"
    unknown = local.compare(None)  # "-": an image without a description
    assert unknown["matches"] is None and unknown["instrument"] is None
    assert local.compare("")["instrument"] is None


def test_no_build_no_file_or_an_unreadable_one_gives_no_digest(tmp_path: Path) -> None:
    none = LocalElf(None).compare(SIM_ELF)
    assert none == {
        "path": None,
        "elf": None,
        "instrument": SIM_ELF,
        "matches": None,
        "detail": "serve was given no build to compare with (--elf)",
    }
    missing = LocalElf(tmp_path / "nope.elf").compare(SIM_ELF)
    assert missing["elf"] is None
    assert missing["detail"] == f"no local build at {tmp_path.name}/nope.elf"
    folder = LocalElf(tmp_path).compare(SIM_ELF)  # a directory stats but does not open
    assert folder["elf"] is None and "could not be read" in folder["detail"]
    assert str(tmp_path) not in json.dumps(folder)


def test_the_file_is_hashed_again_only_when_it_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    real = local_elf_module.elf_digest

    def counting(path: Path) -> str:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(local_elf_module, "elf_digest", counting)
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"first")
    local = LocalElf(elf)
    first = local.digest()[0]
    assert local.digest()[0] == first and len(calls) == 1
    elf.write_bytes(b"second, longer")
    stat = elf.stat()
    os.utime(elf, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = local.digest()[0]
    assert second != first and second == real(elf) and len(calls) == 2


def test_an_unreadable_file_is_tried_again_once_it_can_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"a build")
    failing = [True]
    real = local_elf_module.elf_digest

    def flaky(path: Path) -> str:
        if failing[0]:
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(local_elf_module, "elf_digest", flaky)
    local = LocalElf(elf)
    digest, detail = local.digest()
    assert digest is None and detail.endswith("could not be read: Permission denied")
    failing[0] = False
    assert local.digest()[0] == real(elf)  # same size and time: nothing cached the failure


def test_health_compares_the_instruments_elf_with_the_local_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"the simulator's build")
    with serving_panel() as panel:
        panel.api.local_elf = LocalElf(elf)
        differs = panel.get("/api/health").body["local_elf"]
        assert differs["matches"] is False and differs["instrument"] == SIM_ELF
        assert differs["path"] == shown_path(elf) and differs["elf"] == elf_digest(elf)
        monkeypatch.setattr(local_elf_module, "elf_digest", lambda _: SIM_ELF)
        elf.write_bytes(b"a rebuild that is the simulator's image")
        same = panel.get("/api/health").body["local_elf"]
        assert same["matches"] is True and same["elf"] == SIM_ELF
    health = "SYST:HEAL?;:DIAG:HEAP?;:DIAG:WATC?;:SYST:INF?;:CAL:DATA?"
    asked = [info.message for info in panel.sent]
    assert asked.count(health) == 2 and len(asked) == 2  # the comparison asked nothing more


def test_serve_compares_with_the_local_meter_build_unless_told_otherwise(
    tmp_path: Path,
) -> None:
    assert (
        REGISTRY_ROOT.parent / "firmware" / ".pio" / "build" / "meter" / "firmware.elf"
    ) == serve.DEFAULT_ELF
    assert serve.build_parser().parse_args([]).elf == serve.DEFAULT_ELF
    other = tmp_path / "other.elf"
    assert serve.build_parser().parse_args(["--elf", str(other)]).elf == other


@pytest.mark.parametrize("inside", [False, True], ids=["elsewhere", "in-the-working-directory"])
def test_health_names_the_build_by_no_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside: bool
) -> None:
    """``serve --elf`` given an absolute path: ``GET /api/health`` holds none of it."""
    work = tmp_path / "work"
    elf = (work if inside else tmp_path / "elsewhere") / "build" / "firmware.elf"
    elf.parent.mkdir(parents=True)
    elf.write_bytes(b"a build")
    work.mkdir(exist_ok=True)
    monkeypatch.chdir(work)
    with serving_panel() as panel:
        panel.api.local_elf = LocalElf(serve.build_parser().parse_args(["--elf", str(elf)]).elf)
        text = json.dumps(panel.get("/api/health").body, ensure_ascii=False)
    local = json.loads(text)["local_elf"]
    assert local["path"] == "build/firmware.elf" and local["elf"] == elf_digest(elf)
    assert (
        local["detail"] == "the instrument runs another image than the build at build/firmware.elf"
    )
    for absolute in (str(tmp_path), str(tmp_path.resolve()), str(Path.home())):
        assert absolute not in text
    assert all(not str(value).startswith("/") for value in local.values())
