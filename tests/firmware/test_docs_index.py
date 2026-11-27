# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""docs/README.md, the index of the documents, lists every page of docs/.

A page nobody links is a page nobody finds. Every ``docs/*.md`` the repository
holds (the index itself apart) must be linked from the index, under one of its
four groups and under one only, and the repository's README must link the
index. The links themselves are held by ``test_markdown_links.py``.

Host-only: the files listed and read as text.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
INDEX = DOCS / "README.md"

#: The index's sections, in its order: the reading order, then the four groups.
GROUPS = ["Reading order", "Use the meter", "Build the rig", "Develop the firmware", "History"]

#: The target of an inline link, its fragment left out.
TARGET = re.compile(r"\]\(([^)#\s]+)(?:#[^)\s]*)?\)")


def _groups(text: str) -> dict[str, list[str]]:
    """Each ``##`` section of *text*, with the targets its tables link."""
    sections: dict[str, list[str]] = {}
    for section in re.split(r"^## ", text, flags=re.MULTILINE)[1:]:
        title, _, body = section.partition("\n")
        rows = [line for line in body.splitlines() if line.startswith("| [")]
        sections[title.strip()] = [target for row in rows for target in TARGET.findall(row)]
    return sections


def _unlisted(pages: list[str], text: str) -> list[str]:
    """The *pages* no group of the index *text* lists, or that two do."""
    listed = [target for targets in _groups(text).values() for target in targets]
    return [
        f"{page} (listed {listed.count(page)} times)" for page in pages if listed.count(page) != 1
    ]


def test_the_index_lists_every_page_once() -> None:
    pages = sorted(page.name for page in DOCS.glob("*.md") if page != INDEX)
    assert len(pages) > 5
    wrong = _unlisted(pages, INDEX.read_text(encoding="utf-8"))
    assert not wrong, "docs/README.md must list each page of docs/ once:\n" + "\n".join(wrong)


def test_the_index_has_its_reading_order_and_its_four_groups() -> None:
    assert list(_groups(INDEX.read_text(encoding="utf-8"))) == GROUPS


def test_the_readme_links_the_index_and_the_usage_guide() -> None:
    targets = TARGET.findall((ROOT / "README.md").read_text(encoding="utf-8"))
    assert {"docs/README.md", "docs/usage.md"} <= set(targets)


def test_the_guard_sees_a_page_left_out_and_a_page_listed_twice() -> None:
    text = "\n".join(
        [
            "# The documents",
            "A link in the prose, [scpi.md](scpi.md), does not list a page.",
            "## Use the meter",
            "| [usage.md](usage.md#install) | how |",
            "## History",
            "| [usage.md](usage.md) | again |",
            "| [old.md](old.md) | then |",
        ]
    )
    assert _groups(text) == {"Use the meter": ["usage.md"], "History": ["usage.md", "old.md"]}
    assert _unlisted(["old.md", "scpi.md", "usage.md"], text) == [
        "scpi.md (listed 0 times)",
        "usage.md (listed 2 times)",
    ]
