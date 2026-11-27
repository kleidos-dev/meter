# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""One version, written in one place and repeated where it must be.

``firmware/version.txt`` is the instrument's version: ESP-IDF stamps it into
the image, and ``*IDN?`` and ``SYSTem:INFormation?`` report it. ``CHANGELOG.md``
records what each version changed on the wire and states the version rule.
This guard holds the top entry of the changelog to ``version.txt``, the entries
to a strictly falling order, and the copies that cannot read the file (the host
package, its simulator, the firmware's host-test harness) to the same number,
so that a bump which forgets one fails here rather than on the bench.

Host-only: text files read as text. No device, no build.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSION_TXT = REPO_ROOT / "firmware" / "version.txt"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

#: An entry heading of the changelog: ``## 0.5.0 (2026-11-20)``.
_ENTRY = re.compile(r"^## (\d+)\.(\d+)\.(\d+)\b", re.MULTILINE)

#: The same heading whole, with the date the version was set on (0.1.0's is its days).
_DATED = re.compile(r"^## \d+\.\d+\.\d+ \(.*\d{4}-\d{2}-\d{2}\)$", re.MULTILINE)

#: A page calling a version unreleased: ``(0.17.0, not yet released)``.
_PENDING = re.compile(r"\b(\d+)\.(\d+)\.(\d+),? not yet released")

#: The documents that may say so while a version is on its way.
PAGES = sorted([*(REPO_ROOT / "docs").glob("*.md"), REPO_ROOT / "README.md"])


def _version() -> str:
    return VERSION_TXT.read_text(encoding="utf-8").strip()


def _entries() -> list[tuple[int, int, int]]:
    text = CHANGELOG.read_text(encoding="utf-8")
    return [(int(a), int(b), int(c)) for a, b, c in _ENTRY.findall(text)]


def test_the_version_is_strict_major_minor_patch() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", _version()), (
        f"firmware/version.txt holds {_version()!r}: write major.minor.patch"
    )


def test_the_changelog_starts_at_the_version() -> None:
    entries = _entries()
    assert entries, "CHANGELOG.md has no '## <major>.<minor>.<patch>' entry"
    top = ".".join(map(str, entries[0]))
    assert top == _version(), (
        f"firmware/version.txt is {_version()} but CHANGELOG.md starts at {top}: "
        "a version comes with its entry, in the same commit"
    )


def test_the_changelog_entries_fall_strictly() -> None:
    entries = _entries()
    assert entries == sorted(set(entries), reverse=True), (
        "CHANGELOG.md lists its versions newest first, each once"
    )


def test_every_changelog_entry_carries_its_date() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    assert len(_DATED.findall(text)) == len(_entries()), (
        "an entry's heading is '## <major>.<minor>.<patch> (<year>-<month>-<day>)'"
    )


def pending_versions(text: str) -> list[tuple[int, int, int]]:
    """The versions *text* calls not yet released."""
    return [(int(a), int(b), int(c)) for a, b, c in _PENDING.findall(text)]


def test_no_page_calls_a_released_version_unreleased() -> None:
    assert pending_versions("a key (0.17.0, not yet released) and 1.2.3") == [(0, 17, 0)]
    released = _entries()
    stale = {
        page.name: late
        for page in PAGES
        if (late := [v for v in pending_versions(page.read_text("utf-8")) if v in released])
    }
    assert not stale, f"these say a released version is not yet released: {stale}"


def test_the_changelog_states_the_rule() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    assert "## The version rule" in text
    assert "A changed meaning is not allowed" in text


def test_every_copy_of_the_version_is_the_same() -> None:
    version = _version()
    pyproject = tomllib.loads((REPO_ROOT / "host" / "pyproject.toml").read_text("utf-8"))
    copies = {
        "host/pyproject.toml": pyproject["project"]["version"],
    }
    patterns = {
        "host/kleidos_meter/__init__.py": r'^__version__ = "([^"]+)"',
        "host/kleidos_meter/sim.py": r'self\.firmware = "([^"]+)"',
        "firmware/test/native/harness/sim.h": r'appVersion\s*=\s*"([^"]+)"',
    }
    for path, pattern in patterns.items():
        found = re.search(pattern, (REPO_ROOT / path).read_text("utf-8"), re.MULTILINE)
        assert found, f"{path}: no version found by {pattern!r}"
        copies[path] = found.group(1)
    wrong = {path: value for path, value in copies.items() if value != version}
    assert not wrong, f"firmware/version.txt is {version}; these say otherwise: {wrong}"
