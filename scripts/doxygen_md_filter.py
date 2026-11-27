#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""doxygen_md_filter.py — a Markdown page of this repository, as Doxygen reads it.

The documents are written once, in Markdown, to read on GitHub. Doxygen reads
the same files as the pages of the documentation site (``Doxyfile``,
``FILTER_PATTERNS``) through this filter, which changes three things and keeps
every line where it was, so a warning still names the line of the file:

* **The comments before the title.** The two SPDX lines a page opens with, and
  a generated block between ``<!-- BEGIN GENERATED: x -->`` and
  ``<!-- END GENERATED: x -->`` (the README's banner, which GitHub draws with a
  ``<picture>`` Doxygen does not know), become blank lines, so the page's first
  heading is its title.
* **The page's name.** The first heading gets ``{#name}``, the name of the page
  this file is (:func:`page_name`): ``docs/rig-wiring.md`` is
  ``rig_wiring_page``, ``calibration/README.md`` is ``calibration_page``.
  ``docs/README.md`` is the main page (``USE_MDFILE_AS_MAINPAGE``) and gets
  none.
* **The links.** One form is written, the form GitHub reads: a path relative to
  the file, with an anchor when it goes to a heading
  (``[the pin map](scpi.md#the-pin-map)``). Each becomes the site's
  (:func:`rewrite_link`): a link to a page of the site, or to a heading of
  this one, an HTML link to that page and anchor (``<a
  href="scpi_page.html#the-pin-map">the pin map</a>``: the anchors are GitHub's
  in both, ``MARKDOWN_ID_STYLE = GITHUB``, but for one that starts with a
  digit, :func:`heading_id`, and one another page shares, which the filter
  names itself as the page's name and GitHub's anchor, :func:`shared_ids`);
  a link to one of the firmware
  reference's ``.dox`` files, one to its page (:data:`DOX_PAGES`); an image or
  a link to one, the image's file name, which Doxygen finds in ``IMAGE_PATH``
  (an image it draws) and the build copies beside the pages (one a link
  opens, ``scripts/build_reference.py``); a link to anything else of the
  repository (a folder, a source file), the file on GitHub, once the filter
  has seen it in the tree: a link to a file the repository does not hold is
  written as a reference Doxygen cannot resolve (``@ref "docs/gone.md"``) and
  said on stderr at the page's line (:func:`missing_file`, :func:`warning`),
  so the gate (``mkdir -p build && doxygen Doxyfile``) fails on it there,
  as on a warning in a comment.
  ``https:`` and ``mailto:`` links stay. The HTML link is written because Doxygen 1.17.0
  turns a Markdown link with an anchor into a reference that writes its text
  twice and drops the rest of the line. A link whose text is its target's
  file (``[usage.md](usage.md)``), which GitHub shows among files, reads on
  the site as the page's name (:func:`link_text`, :data:`LINK_NAMES`).

What the filter does not judge, the tests do: that every link's heading
exists, and its file (``tests/firmware/test_markdown_links.py``), that a Markdown link
goes to a page of the site and that ``Doxyfile``'s pages are this filter's
(``tests/firmware/test_doc_link_form.py``). The links of the finished site are
checked in its HTML by ``scripts/check_doc_links.py`` (the gate's build) and
``scripts/check_reference.py`` (the published one).

Usage: ``python3 scripts/doxygen_md_filter.py <file.md>`` (prints the page as
Doxygen reads it).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Where a link to a file of this repository that is not a page goes.
REPOSITORY = "https://github.com/kleidos-dev/meter"
BRANCH = "main"

#: The Markdown files that are pages of the site, as ``Doxyfile``'s ``INPUT``
#: names them: each file named, and each ``.md`` of each folder named.
#: ``tests/firmware/test_doc_link_form.py`` holds the two to each other.
PAGE_DIRS = ("docs", "calibration/records")
PAGE_FILES = (
    "README.md",
    "AGENTS.md",
    "CHANGELOG.md",
    "NOTICE.md",
    "calibration/README.md",
    "hardware/README.md",
    "hardware/bom.md",
    "host/README.md",
)

#: The page Doxygen opens the site with (``USE_MDFILE_AS_MAINPAGE``).
MAIN_PAGE = "docs/README.md"

#: The Doxygen files a page links to, and the page of the site each one is:
#: the firmware's front page and its topics.
DOX_PAGES = {"docs/firmware.dox": "firmware_page.html", "docs/groups.dox": "topics.html"}

#: What a link reads on the site when its text is its target's file (``[usage.md](usage.md)``):
#: GitHub shows files, the site shows pages, so a page is named here, as a
#: phrase that fits in a sentence (:func:`link_text`). Every page has one
#: (``tests/firmware/test_doc_link_form.py``).
LINK_NAMES = {
    "AGENTS.md": "the repository's rules",
    "CHANGELOG.md": "the changelog",
    "NOTICE.md": "the notices",
    "README.md": "the repository's README",
    "calibration/README.md": "the calibration guide",
    "calibration/records/2026-11-19.md": "the record of 2026-11-19",
    "calibration/records/2026-11-20.md": "the record of 2026-11-20",
    "calibration/records/radio-ab-2026-11-21.md": "the radio A/B's record",
    "calibration/records/radio-tx-sweep-2026-11-21.md": "the radio A/B's addendum",
    "docs/README.md": "the documentation's front page",
    "docs/architecture.md": "the firmware's architecture",
    "docs/carrier-board.md": "the carrier board's guide",
    "docs/cli.md": "the command line's reference",
    "docs/firmware-design.md": "the first design",
    "docs/http-api.md": "the HTTP API's reference",
    "docs/ina228-reference.md": "the INA228 reference",
    "docs/misra.md": "the MISRA record",
    "docs/panel.md": "the panel's guide",
    "docs/python-api.md": "the Python API's reference",
    "docs/relay-timing.md": "the relay timings",
    "docs/relay2-click-wiring.md": "the Relay 2 Click guide",
    "docs/rig-wiring.md": "the wiring guide",
    "docs/scpi.md": "the SCPI command set",
    "docs/testing.md": "the testing guide",
    "docs/troubleshooting.md": "the troubleshooting guide",
    "docs/usage.md": "the usage guide",
    "hardware/README.md": "the schematic's page",
    "hardware/bom.md": "the bill of materials",
    "host/README.md": "the host package's README",
    "docs/firmware.dox": "the firmware's front page",
    "docs/groups.dox": "the firmware's topics",
}

#: What comes before a link that opens a sentence, a table's cell or a list's
#: item (an emphasis may open it too), whose name is then capitalized.
_OPENS_RE = re.compile(r"(?:^[\s>]*(?:[-*+]\s+|\d+\.\s+)?|[.!?|]\s*)(?:\*\*|__|\*|_)?$")
#: What follows a link to a heading that already names its section.
_SECTION_AFTER_RE = re.compile(r"^\s*\u00a7")

#: The kinds of file an image link names: found by their name in ``IMAGE_PATH``.
IMAGE_SUFFIXES = frozenset({".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp"})

_COMMENT_OPEN = "<!--"
_COMMENT_CLOSE = "-->"
_GENERATED_BEGIN_RE = re.compile(r"<!--\s*BEGIN GENERATED:")
_GENERATED_END_RE = re.compile(r"<!--\s*END GENERATED:.*?-->")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_HEADING_RE = re.compile(r"^# (?!.*\{#)(.+?)\s*$")
#: An inline link or image: ``!`` or not, its text, its target, an optional
#: title. The text may hold one level of brackets (``[`x[0]`](y)``).
_LINK_RE = re.compile(
    r"(?P<bang>!?)\[(?P<text>(?:[^\[\]]|\[[^\[\]]*\])*)\]"
    r"\((?P<target>[^()\s]+)(?P<title>(?:\s+\"[^\"]*\")?)\)"
)
#: The parts of a link :func:`rewrite_link` reads.
_LINK_GROUPS = ("bang", "text", "target", "title")
#: A code span, whose content is never a link.
_CODE_SPAN_RE = re.compile(r"(`+)(?:(?!\1).)+?\1")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def page_files(root: Path = REPO_ROOT) -> list[str]:
    """Return every Markdown file that is a page of the site, from the root."""
    found = {name for name in PAGE_FILES if (root / name).is_file()}
    for folder in PAGE_DIRS:
        found |= {f"{folder}/{p.name}" for p in (root / folder).glob("*.md")}
    return sorted(found)


def page_name(path: str) -> str:
    """Return the name of the page the Markdown file *path* (from the root) is.

    ``index`` for the main page. Any other is a stem, each run of other
    characters than letters and digits an underscore and capitals lowered,
    then ``_page``: a folder's README is the folder's name (the root's is
    ``readme``), a calibration record ``calibration_record_`` and its file's
    name, any other file its name: ``docs/rig-wiring.md`` is
    ``rig_wiring_page``, ``calibration/README.md`` ``calibration_page``. The
    suffix keeps a page's name apart from every heading's anchor, which Doxygen
    holds in the same set of names and writes with hyphens
    (``MARKDOWN_ID_STYLE = GITHUB``): a page named ``calibration`` would be a
    heading of ``docs/scpi.md``.
    """
    pure = PurePosixPath(path)
    if path == MAIN_PAGE:
        return "index"
    if pure.name == "README.md":
        stem = "readme" if str(pure.parent) == "." else str(pure.parent)
    elif str(pure.parent) == "calibration/records":
        stem = f"calibration_record_{pure.stem}"
    else:
        stem = pure.stem
    return re.sub(r"[^a-z0-9]+", "_", stem.lower()).strip("_") + "_page"


def heading_id(fragment: str) -> str:
    """Return the id Doxygen gives the heading GitHub anchors as *fragment*.

    The two agree (``MARKDOWN_ID_STYLE = GITHUB``) but for an anchor that
    starts with a digit, which Doxygen 1.17.0 prefixes with ``autotoc_md``:
    GitHub's ``#24-the-own-i2c-master`` is Doxygen's
    ``#autotoc_md24-the-own-i2c-master``.
    """
    return f"autotoc_md{fragment}" if fragment[:1].isdigit() else fragment


_ATX_RE = re.compile(r"^(#{2,6})\s+(.*?)\s*#*\s*$")
_LABEL_RE = re.compile(r"@(?:defgroup|page|section|subsection|anchor)\s+(\w+)")


def github_slug(heading: str) -> str:
    """Return GitHub's anchor for a heading's text (``tests/firmware/test_markdown_links.py``)."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    text = text.replace("`", "").lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def heading_ids(text: str) -> list[tuple[int, str]]:
    """Return each heading below the title of the page *text*, by line, with GitHub's anchor.

    A heading GitHub has seen before on the page gets ``-1``, ``-2``, … as
    GitHub numbers it. Headings in fenced blocks are not headings.
    """
    found: list[tuple[int, str]] = []
    seen: dict[str, int] = {}
    fence = ""
    for number, line in enumerate(text.split("\n")):
        opened = _FENCE_RE.match(line)
        if fence:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = ""
            continue
        if opened:
            fence = opened.group(1)
            continue
        heading = _ATX_RE.match(line)
        if heading is None or "{#" in heading.group(2):
            continue
        slug = github_slug(heading.group(2))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.append((number, slug if count == 0 else f"{slug}-{count}"))
    return found


def shared_ids(root: Path = REPO_ROOT) -> frozenset[str]:
    """Return the anchors Doxygen cannot give a heading as GitHub does.

    Doxygen keeps one set of names for the whole site and numbers a second
    heading of one name on another page, where GitHub's anchors are a page's
    own: a heading whose anchor another page also has, or that is a name of
    the firmware reference (a topic, a page, a section), is the one this
    filter names itself (:func:`filtered`).
    """
    pages: dict[str, int] = {}
    for name in page_files(root):
        for slug in {slug for _, slug in heading_ids((root / name).read_text(encoding="utf-8"))}:
            pages[slug] = pages.get(slug, 0) + 1
    labels: set[str] = set()
    for dox in sorted((root / "docs").glob("*.dox")):
        labels |= set(_LABEL_RE.findall(dox.read_text(encoding="utf-8")))
    return frozenset({slug for slug, count in pages.items() if count > 1} | (set(pages) & labels))


def anchor_for(page: str, fragment: str, shared: frozenset[str]) -> str:
    """Return the id the site gives the heading GitHub anchors as *fragment* on *page*.

    A shared anchor (:func:`shared_ids`) is the page's name, a hyphen and
    GitHub's anchor (``usage_page-what-you-need``); any other is GitHub's,
    as Doxygen writes it (:func:`heading_id`).
    """
    return f"{page}-{fragment}" if fragment in shared else heading_id(fragment)


def _resolve(source: str, target: str) -> str:
    """Return *target*, a link relative to the file *source*, as a path from the root."""
    parts: list[str] = []
    for part in (PurePosixPath(source).parent / target).parts:
        if part == "..":
            if parts:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


def site_target(
    source: str,
    target: str,
    pages: frozenset[str],
    root: Path = REPO_ROOT,
    shared: frozenset[str] = frozenset(),
) -> tuple[str, bool]:
    """Return where the link *target* of the page *source* goes on the site.

    :param source: The page's file, from the root.
    :param target: The link's target as the page writes it.
    :param pages: Every page's file, from the root.
    :param root: The repository's root, where a folder is told from a file.
    :param shared: The anchors the filter names itself (:func:`shared_ids`).
    :returns: The address, and whether it is an HTML link to write (a page of
        the site, a heading, a ``.dox`` page, an image a link opens) rather
        than a Markdown target to keep (an image, a URL).
    """
    if target.startswith("#"):
        return f"#{anchor_for(page_name(source), target[1:], shared)}", True
    if _SCHEME_RE.match(target) or target.startswith("/"):
        return target, False
    path, _, fragment = target.partition("#")
    resolved = _resolve(source, path)
    if resolved in pages:
        name = page_name(resolved)
        anchor = f"#{anchor_for(name, fragment, shared)}" if fragment else ""
        return f"{name}.html{anchor}", True
    anchor = f"#{fragment}" if fragment else ""
    if resolved in DOX_PAGES:
        return f"{DOX_PAGES[resolved]}{anchor}", True
    if PurePosixPath(resolved).suffix.lower() in IMAGE_SUFFIXES:
        return PurePosixPath(resolved).name, True
    kind = "tree" if path.endswith("/") or (root / resolved).is_dir() else "blob"
    return f"{REPOSITORY}/{kind}/{BRANCH}/{resolved.rstrip('/')}{anchor}", False


def missing_file(
    source: str, target: str, pages: frozenset[str], root: Path = REPO_ROOT
) -> str | None:
    """Return the file the link *target* of the page *source* names, when the tree lacks it.

    Only a link that :func:`site_target` would send to GitHub is looked at: a
    page of the site, a ``.dox`` page and an image are the HTML gates' to
    check, and an anchor is never looked at here.

    :returns: The file's path from the root, or ``None`` when the link goes
        elsewhere or the file (or folder) is there.
    """
    if target.startswith(("#", "/")) or _SCHEME_RE.match(target):
        return None
    resolved = _resolve(source, target.partition("#")[0])
    if (
        resolved in pages
        or resolved in DOX_PAGES
        or PurePosixPath(resolved).suffix.lower() in IMAGE_SUFFIXES
    ):
        return None
    return None if (root / resolved).exists() else resolved


def warning(path: str, number: int, target: str, resolved: str) -> str:
    """Return the line the filter says on stderr for a link to a file that is not there.

    It takes Doxygen's ``WARN_FORMAT`` (``$file:$line: $text``), so the gates
    that read Doxygen's warnings read it as one.
    """
    return (
        f"{path}:{number}: warning: the link {target} names {resolved},"
        " which is not in the repository"
    )


def heading_text(path: str, fragment: str, root: Path = REPO_ROOT) -> str | None:
    """Return the words of the heading GitHub anchors as *fragment* in the page *path*.

    Links in the heading keep their words; ``None`` when the page has no such
    heading.
    """
    lines = (root / path).read_text(encoding="utf-8").split("\n")
    for number, slug in heading_ids("\n".join(lines)):
        if slug == fragment:
            heading = _ATX_RE.match(lines[number])
            if heading is not None:
                return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.group(2))
    return None


def link_text(
    text: str, source: str, target: str, before: str, after: str = "", root: Path = REPO_ROOT
) -> str:
    """Return what the link *text* to *target* on the page *source* reads on the site.

    A text that is the target's file, as the page writes its path, as the
    path from the root or as the file's name alone, code span or not, is the
    page's name (:data:`LINK_NAMES`); with an anchor, the heading's words in
    quotes and then the page's name, or the page's name alone where *after*,
    the line after the link, names the section (``§3.2``). The name is
    capitalized where *before*, the line before the link, leaves it opening a
    sentence, a cell or an item. Any other text stays.
    """
    if target.startswith(("#", "/")) or _SCHEME_RE.match(target):
        return text
    path, _, fragment = target.partition("#")
    resolved = _resolve(source, path)
    name = LINK_NAMES.get(resolved)
    words = text.strip("`")
    if name is None or words not in {path, resolved, PurePosixPath(resolved).name}:
        return text
    named = fragment and not _SECTION_AFTER_RE.match(after)
    heading = heading_text(resolved, fragment, root) if named else None
    if heading is not None:
        return f"“{heading}” in {name}"
    return name[:1].upper() + name[1:] if _OPENS_RE.search(before) else name


def rewrite_link(
    match: Mapping[str, str],
    source: str,
    pages: frozenset[str],
    root: Path = REPO_ROOT,
    shared: frozenset[str] = frozenset(),
) -> str:
    """Return the link or image *match* of the page *source* as the site writes it.

    :param match: The link's parts as the page writes them (:data:`_LINK_GROUPS`).
    """
    gone = missing_file(source, match["target"], pages, root)
    if gone is not None:
        return f'{match["text"]} @ref "{gone}"'
    target, html = site_target(source, match["target"], pages, root, shared)
    if match["bang"]:
        return f"![{match['text']}]({target}{match['title']})"
    if html:
        return f'<a href="{target}">{match["text"]}</a>'
    return f"[{match['text']}]({target}{match['title']})"


def _links(
    line: str,
    source: str,
    pages: frozenset[str],
    root: Path,
    shared: frozenset[str],
    gone: list[tuple[str, str]],
) -> str:
    """Return *line* with every link rewritten, none inside a code span.

    Each link to a file that is not there is added to *gone*, its target as
    written and the file's path from the root (:func:`missing_file`).

    The code spans are masked while the links are found, so a link inside one
    is no link, and a link whose text holds one (``[`scpi.md`, "The pin
    map"](scpi.md#the-pin-map)``) is one link; each is then read back from the
    line as written.
    """
    masked = _CODE_SPAN_RE.sub(lambda m: "\0" * len(m.group(0)), line)
    out: list[str] = []
    last = 0
    for match in _LINK_RE.finditer(masked):
        out.append(line[last : match.start()])
        parts = {group: line[match.start(group) : match.end(group)] for group in _LINK_GROUPS}
        if not parts["bang"]:
            before = line[: match.start()]
            after = line[match.end() :]
            parts["text"] = link_text(parts["text"], source, parts["target"], before, after, root)
        missing = missing_file(source, parts["target"], pages, root)
        if missing is not None:
            gone.append((parts["target"], missing))
        out.append(rewrite_link(parts, source, pages, root, shared))
        last = match.end()
    out.append(line[last:])
    return "".join(out)


def filtered(
    text: str,
    source: str,
    pages: frozenset[str],
    root: Path = REPO_ROOT,
    shared: frozenset[str] = frozenset(),
    missing: list[tuple[int, str, str]] | None = None,
) -> str:
    """Return the Markdown page *text* of the file *source* as Doxygen reads it.

    :param shared: The anchors the filter names itself (:func:`shared_ids`): a
        heading of one gets ``{#<page>-<anchor>}``.
    :param missing: Where each link to a file that is not there is added: its
        line (from 1), its target as written, the file's path from the root.
    """
    named = {number: slug for number, slug in heading_ids(text) if slug in shared}
    lines = text.split("\n")
    in_fence = ""
    in_comment = False
    in_generated = False
    titled = source == MAIN_PAGE
    before_title = True
    for index, line in enumerate(lines):
        fence = _FENCE_RE.match(line)
        if in_fence:
            if fence and fence.group(1)[0] == in_fence[0] and len(fence.group(1)) >= len(in_fence):
                in_fence = ""
            continue
        if in_generated:
            in_generated = not _GENERATED_END_RE.search(line)
            lines[index] = ""
            continue
        if _GENERATED_BEGIN_RE.search(line):
            in_generated = not _GENERATED_END_RE.search(line)
            lines[index] = ""
            continue
        if before_title and (in_comment or line.lstrip().startswith(_COMMENT_OPEN)):
            in_comment = _COMMENT_CLOSE not in line
            lines[index] = ""
            continue
        if fence:
            in_fence = fence.group(1)
            continue
        if line.strip():
            before_title = False
        heading = _HEADING_RE.match(line)
        written = line
        if heading and not titled:
            written = f"# {heading.group(1)} {{#{page_name(source)}}}"
            titled = True
        elif index in named:
            written = f"{line.rstrip()} {{#{page_name(source)}-{named[index]}}}"
        gone: list[tuple[str, str]] = []
        lines[index] = _links(written, source, pages, root, shared, gone)
        if missing is not None:
            missing.extend((index + 1, target, resolved) for target, resolved in gone)
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """Print the page *argv[1]* as Doxygen reads it."""
    if len(argv) != 2:
        print("usage: doxygen_md_filter.py <file.md>", file=sys.stderr)
        return 2
    path = Path(argv[1]).resolve()
    try:
        source = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        source = path.name
    text = path.read_text(encoding="utf-8")
    missing: list[tuple[int, str, str]] = []
    pages = frozenset(page_files())
    sys.stdout.write(filtered(text, source, pages, REPO_ROOT, shared_ids(), missing))
    for number, target, resolved in missing:
        print(warning(argv[1], number, target, resolved), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
