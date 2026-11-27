# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The README's layout is the tree, and its settled questions cite their record.

``README.md`` draws the repository in its "Layout" block: every top-level
entry, every page of ``docs/`` and every file of ``firmware/src``. A file
added to the tree and not to the block, or one the block still names after
it left, makes the drawing false, so this guard fails on either. A name is
matched as the block writes it: a path's last part (``docs/``, ``LICENSES/``),
and the shorthand ``relay.h/.cpp`` for a header and its source.

The README's "Still open, for the bench" list keeps a question once it is
settled, struck through (``~~...~~``), so that the answer stays next to the
question. A struck item must say ``Settled on <date>`` and link the record that
settles it, a file of this repository (and a heading of it when the link
names one).

Host-only: ``git ls-files`` and the README read as text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

#: A file-like name in the block: a word with an extension, or a directory.
NAME = re.compile(r"[A-Za-z0-9_.-]+(?:/\.[a-z]+)?/?")

#: The relative links of a line: ``[text](target)``.
LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?\s*\)")

#: The date a struck item was settled on.
SETTLED = re.compile(r"Settled on (\d{4}-\d{2}-\d{2})")

#: Names the block writes that are not paths of the tree.
NOT_PATHS = {".", "the"}


def _tracked() -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.split(b"\0")
    return [name.decode() for name in listed if name]


def _section(text: str, heading: str) -> str:
    """The body of the ``## heading`` section of ``text``."""
    start = text.index(f"\n## {heading}\n") + len(heading) + 5
    end = text.find("\n## ", start)
    return text[start : end if end >= 0 else len(text)]


def layout_block(text: str) -> str:
    """The fenced block of the README's "Layout" section."""
    body = _section(text, "Layout")
    start = body.index("```text\n") + len("```text\n")
    return body[start : body.index("\n```", start)]


def layout_names(block: str) -> set[str]:
    """Every name the block draws, a ``name.h/.cpp`` pair as both files."""
    names: set[str] = set()
    for line in block.splitlines():
        drawn = line.split("#", 1)[0]
        drawn = drawn.lstrip("│├└─  ")
        for match in NAME.finditer(drawn):
            name = match.group(0)
            if name.endswith(("/.cpp", "/.h")):
                stem, _, ext = name.rpartition("/")
                base = stem.rsplit(".", 1)[0]
                names.add(stem)
                names.add(base + ext)
            else:
                names.add(name.rstrip("/") + ("/" if name.endswith("/") else ""))
    return names - NOT_PATHS


def expected_names(tracked: list[str]) -> set[str]:
    """What the block must name: top-level entries, docs pages, firmware/src files."""
    expected: set[str] = set()
    for path in tracked:
        parts = path.split("/")
        if len(parts) == 1:
            if not parts[0].startswith("."):
                expected.add(parts[0])
        elif not parts[0].startswith("."):
            expected.add(parts[0] + "/")
        if parts[0] == "docs" and len(parts) == 2:
            expected.add(parts[1])
        if parts[:2] == ["firmware", "src"] and len(parts) == 3:
            expected.add(parts[2])
    return expected


def missing_from_layout(block: str, tracked: list[str]) -> list[str]:
    """The names the tree holds that the block does not draw."""
    return sorted(expected_names(tracked) - layout_names(block))


def stale_in_layout(block: str, tracked: list[str]) -> list[str]:
    """The ``firmware/src`` and ``docs/`` files the block names that the tree lacks."""
    present = {Path(path).name for path in tracked}
    drawn = {name for name in layout_names(block) if name.endswith((".h", ".cpp", ".md", ".dox"))}
    return sorted(drawn - present)


def _slug(heading: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading).replace("`", "").lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    anchors = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not match:
            continue
        slug = _slug(match.group(1))
        count = seen.get(slug, 0)
        anchors.add(slug if count == 0 else f"{slug}-{count}")
        seen[slug] = count + 1
    return anchors


def struck_faults(section: str, base: Path) -> list[str]:
    """Each struck item of ``section`` with no date or no record that resolves."""
    faults = []
    for item in re.split(r"\n(?=\d+\. )", section.strip()):
        if "~~" not in item:
            continue
        label = item.split("~~")[1][:60]
        if not SETTLED.search(item):
            faults.append(f"{label}: no 'Settled on <date>'")
        links = [target for target in LINK.findall(item) if not re.match(r"^[a-z]+:", target)]
        if not links:
            faults.append(f"{label}: links no record")
        for target in links:
            file_part, _, fragment = target.partition("#")
            path = (base / file_part).resolve()
            if not path.is_file():
                faults.append(f"{label}: {target} is no file")
            elif fragment and path.suffix == ".md" and fragment not in _anchors(path):
                faults.append(f"{label}: {target} names no heading")
    return faults


def test_the_layout_draws_every_entry_page_and_source() -> None:
    missing = missing_from_layout(layout_block(README.read_text(encoding="utf-8")), _tracked())
    assert not missing, "README.md 'Layout' does not draw: " + ", ".join(missing)


def test_the_layout_names_nothing_the_tree_lacks() -> None:
    stale = stale_in_layout(layout_block(README.read_text(encoding="utf-8")), _tracked())
    assert not stale, "README.md 'Layout' names files the tree does not hold: " + ", ".join(stale)


def test_every_struck_question_is_dated_and_cites_a_record() -> None:
    section = _section(README.read_text(encoding="utf-8"), "Still open, for the bench")
    assert "~~" in section, "no settled question left to check: the guard would test nothing"
    faults = struck_faults(section, ROOT)
    assert not faults, "README.md 'Still open, for the bench':\n" + "\n".join(faults)


def test_the_layout_guard_sees_a_missing_and_a_stale_source() -> None:
    block = layout_block(README.read_text(encoding="utf-8"))
    tracked = [*_tracked(), "firmware/src/planted.h", "docs/planted.md"]
    assert missing_from_layout(block, tracked) == ["planted.h", "planted.md"]
    assert stale_in_layout(block + "\n│   ├── gone.h/.cpp  # left the tree", _tracked()) == [
        "gone.cpp",
        "gone.h",
    ]


def test_the_struck_guard_sees_an_undated_or_dangling_answer() -> None:
    section = (
        "1. ~~A question.~~ Answered ([record](calibration/records/2026-11-20.md)).\n"
        "2. ~~Another.~~ Settled on 2026-11-20 ([record](calibration/records/none.md)).\n"
        "3. ~~A third.~~ Settled on 2026-11-20, by word of mouth.\n"
        "4. ~~A fourth.~~ Settled on 2026-11-20 ([record](docs/architecture.md#no-such-heading)).\n"
        "5. Still open.\n"
    )
    faults = struck_faults(section, ROOT)
    assert any("A question." in fault and "Settled on" in fault for fault in faults)
    assert any("none.md is no file" in fault for fault in faults)
    assert any("A third." in fault and "links no record" in fault for fault in faults)
    assert any("names no heading" in fault for fault in faults)
    assert len(faults) == 4
