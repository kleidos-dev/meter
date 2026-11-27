# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every relative link of a tracked Markdown file resolves.

A document that moves or splits (the current design left the 0.1.0 record for
``docs/architecture.md`` on 2026-11-22) leaves links behind it, and a broken
relative link renders as a working one until somebody follows it. This guard
reads every tracked ``.md`` file, outside fenced blocks and inline code, and
fails on a link or an image whose target is not a tracked-tree path relative
to the file, and on a ``#fragment`` that names no heading of its target
Markdown file (GitHub's anchors: lower case, punctuation dropped, spaces to
hyphens, a repeated heading numbered ``-1``, ``-2``, ...). Links with a
scheme (``https:``, ``mailto:``) are not followed.

Host-only: ``git ls-files`` and the files read as text.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: An inline link or image: ``[text](target)`` or ``![alt](target "title")``.
LINK = re.compile(r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")

#: A target with a scheme, which this guard does not follow.
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

#: An ATX heading.
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")


def _markdown_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "*.md"], capture_output=True, check=True
    ).stdout.split(b"\0")
    return [ROOT / name.decode() for name in listed if name]


def _prose_lines(text: str) -> list[tuple[int, str]]:
    """The lines outside fenced blocks, numbered, with inline code removed."""
    lines = []
    fence = ""
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = ""
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            continue
        lines.append((number, re.sub(r"`+[^`]*`+", "", line)))
    return lines


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading's text."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link keeps its text
    text = text.replace("`", "").lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _broken(path: Path, text: str) -> list[str]:
    name = path.relative_to(ROOT).as_posix()
    found = []
    for number, line in _prose_lines(text):
        for match in LINK.finditer(line):
            target = match.group(1)
            if SCHEME.match(target):
                continue
            file_part, _, fragment = target.partition("#")
            resolved = (path.parent / file_part).resolve() if file_part else path
            if not resolved.exists():
                found.append(f"{name}:{number}: {target} (no such file)")
                continue
            if fragment and resolved.suffix == ".md":
                heads = _heading_anchors(resolved)
                if fragment not in heads:
                    found.append(f"{name}:{number}: {target} (no such heading)")
    return found


def _heading_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    seen: dict[str, int] = {}
    fence = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = ""
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            continue
        match = HEADING.match(line)
        if not match:
            continue
        slug = _slug(match.group(1))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


#: A Markdown image's alt text, and an HTML image tag.
IMAGE_ALT = re.compile(r"!\[([^\]]*)\]\(")
IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
TAG_ALT = re.compile(r"\balt=\"([^\"]*)\"")


def _without_alt(path: Path, text: str) -> list[str]:
    """The images of *text* with no alt text: a reader of the page without them reads nothing."""
    name = path.relative_to(ROOT).as_posix()
    found = []
    for number, line in _prose_lines(text):
        found += [
            f"{name}:{number}: an image with no alt text"
            for alt in IMAGE_ALT.findall(line)
            if not alt.strip()
        ]
        for tag in IMG_TAG.findall(line):
            alt = TAG_ALT.search(tag)
            if alt is None or not alt.group(1).strip():
                found.append(f"{name}:{number}: an <img> with no alt text")
    return found


def test_every_image_has_its_alt_text() -> None:
    found = []
    for path in _markdown_files():
        found += _without_alt(path, path.read_text(encoding="utf-8"))
    assert not found, "images with no alt text:\n" + "\n".join(found)


def test_the_alt_guard_sees_an_image_and_a_tag_without_one() -> None:
    page = ROOT / "docs" / "panel.md"
    text = "\n".join(
        [
            "![The Live view](images/panel/live.png) and ![ ](images/panel/runs.png)",
            '<img src="a.svg" alt="A banner"> <img src="b.svg"> <img src="c.svg" alt="">',
            "`![](in-code.png)`",
            "```text",
            "![](in-a-fence.png)",
            "```",
        ]
    )
    assert _without_alt(page, text) == [
        "docs/panel.md:1: an image with no alt text",
        "docs/panel.md:2: an <img> with no alt text",
        "docs/panel.md:2: an <img> with no alt text",
    ]


def test_every_relative_link_resolves() -> None:
    found = []
    for path in _markdown_files():
        found += _broken(path, path.read_text(encoding="utf-8"))
    assert not found, "broken relative links:\n" + "\n".join(found)


def test_the_guard_sees_a_broken_link_and_a_missing_heading() -> None:
    page = ROOT / "docs" / "architecture.md"
    text = "\n".join(
        [
            "See [the record](firmware-design.md) and [the protocol](scpi.md#the-line).",
            "A [moved page](firmware-design-old.md), a [lost heading](scpi.md#no-such-heading).",
            "`[in code](nowhere.md)` and [a site](https://kleidos.dev/x.md) are not followed.",
            "```text",
            "[in a fence](nowhere.md)",
            "```",
            "![an image](../hardware/missing.svg)",
        ]
    )
    assert _broken(page, text) == [
        "docs/architecture.md:2: firmware-design-old.md (no such file)",
        "docs/architecture.md:2: scpi.md#no-such-heading (no such heading)",
        "docs/architecture.md:7: ../hardware/missing.svg (no such file)",
    ]


def test_the_anchors_follow_github() -> None:
    assert _slug("1. The design in one page") == "1-the-design-in-one-page"
    assert _slug("The relays — the null without hands") == "the-relays--the-null-without-hands"
    assert _slug("`CHARGE` stays, as a probe") == "charge-stays-as-a-probe"
    assert _slug("Deviations from SCPI-1999") == "deviations-from-scpi-1999"
