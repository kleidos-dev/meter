# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Nothing outside the historical records describes the meter's old home or protocol.

The meter left the Kleidos firmware repository on 2026-11-19, where it was the
nested project ``qa/meter/`` with its notes in ``plan/power-meter/``, and its
console spoke ``key=value`` lines with ``METER_OK:`` / ``METER_ERR:`` prefixes
until SCPI replaced it in 0.2.0. A comment or a document that still names
those paths or replies says something false about this repository, so this
guard fails on any of them in a tracked file, except in the documents that are
records of that time and say so: the changelog, and the one README sentence
that says where the meter came from. The 0.1.0 design
(``docs/firmware-design.md``), the map from the old console in
``docs/scpi.md`` and the carrier board's protocol note may name the old
replies, which they describe, but not the old home: the old paths are the Kleidos
firmware repository's private notes or its layout, which no reader here can
open (owner, 2026-11-27).

The 0.1.0 design was frozen on 2026-11-22: the current design is
``docs/architecture.md``, which every change keeps current, and the record's
section numbers no longer describe the firmware. So a citation of a section
of the record (``firmware-design.md §3.5``, or ``SS 3.5`` where a source keeps
to ASCII) is stale anywhere but in the record itself and the changelog, whose
entries are history; naming the record as a whole stays allowed.

Host-only: ``git ls-files`` and the files read as text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: The tokens that describe the old home or the old protocol.
TOKENS = ("qa/meter/", "plan/power-meter", "METER_OK", "METER_ERR")

#: Files that are records of that time, and the tokens each may carry.
HISTORICAL: dict[str, set[str]] = {
    "CHANGELOG.md": set(TOKENS),
    "docs/firmware-design.md": {"METER_OK", "METER_ERR"},
    "docs/scpi.md": {"METER_OK", "METER_ERR"},
    "docs/carrier-board.md": {"METER_OK", "METER_ERR"},
    "README.md": {"qa/meter/"},
    "tests/firmware/test_no_stale_references.py": set(TOKENS),
}

#: A citation of a section of the frozen design record: the file name, at most
#: a few characters of quoting, punctuation or a comment's continuation, then a
#: section sign or its ASCII spelling ``SS`` and a number.
CITATION = re.compile(r"firmware-design\.md[^\w§]{0,16}?(?:§|SS ?\d)")

#: The files that may cite the record's sections: the record and the changelog.
CITING_ALLOWED = {
    "CHANGELOG.md",
    "docs/firmware-design.md",
    "tests/firmware/test_no_stale_references.py",
}


def _tracked_text_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.split(b"\0")
    files = []
    for name in listed:
        if not name:
            continue
        path = ROOT / name.decode()
        if path.is_file() and not path.is_symlink():
            files.append(path)
    return files


def _stale(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return []
    allowed = HISTORICAL.get(path.relative_to(ROOT).as_posix(), set())
    found: list[Any] = []
    for number, line in enumerate(text.splitlines(), start=1):
        found.extend(
            f"{path.relative_to(ROOT)}:{number}: {token}"
            for token in TOKENS
            if token in line and token not in allowed
        )
    return found


def _citations(name: str, text: str) -> list[str]:
    """Each citation of a section of the frozen record in ``text``, by line."""
    if name in CITING_ALLOWED:
        return []
    return [
        f"{name}:{text.count(chr(10), 0, match.start()) + 1}: {match.group(0)!r}"
        for match in CITATION.finditer(text)
    ]


def test_no_tracked_file_names_the_old_home_or_protocol() -> None:
    found = [hit for path in _tracked_text_files() for hit in _stale(path)]
    assert not found, "stale references (this repository is not qa/meter/):\n" + "\n".join(found)


def test_the_guard_sees_a_planted_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = ROOT / "firmware" / "src" / "main.cpp"
    text = planted.read_text(encoding="utf-8") + "\n// see qa/meter/docs/x.md\n"
    monkeypatch.setattr(Path, "read_text", lambda self, encoding="utf-8": text)
    assert _stale(planted) == [f"firmware/src/main.cpp:{text.count(chr(10))}: qa/meter/"]


def test_nothing_cites_a_section_of_the_frozen_design_record() -> None:
    found = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found += _citations(path.relative_to(ROOT).as_posix(), text)
    assert not found, (
        "a citation of the frozen 0.1.0 design record's sections (cite docs/architecture.md):\n"
        + "\n".join(found)
    )


def test_the_citation_guard_sees_every_spelling() -> None:
    cited = [
        "see `docs/firmware-design.md` §3.5.",
        "(docs/firmware-design.md SS 3.10.1)",
        "``docs/firmware-design.md`` SS 6.5",
        "[docs/firmware-design.md](docs/firmware-design.md) §3.1.1",
        "(`docs/firmware-design.md`\n    // SS 2.2)",
        "`firmware-design.md` — §3.7",
        "(`docs/firmware-design.md` SS 3.1, SS 5)",
    ]
    for text in cited:
        assert len(_citations("firmware/src/main.cpp", text)) == 1, text
    assert _citations("firmware/src/main.cpp", cited[4]) == [
        "firmware/src/main.cpp:1: 'firmware-design.md`\\n    // SS 2'"
    ]
    named = [
        "[`docs/firmware-design.md`](docs/firmware-design.md) is the record of 0.1.0",
        "the checks are in `docs/firmware-design.md`, the frozen record",
        "`docs/architecture.md` §3.3",
    ]
    for text in named:
        assert _citations("firmware/src/main.cpp", text) == [], text
    assert _citations("CHANGELOG.md", cited[0]) == []
