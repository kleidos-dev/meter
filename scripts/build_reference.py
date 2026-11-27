#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""build_reference.py — the meter's documentation as it is published, guarded.

``https://api.kleidos.dev/meter/`` is to be this script's output, once this
repository is public: one site, the Markdown pages of the repository and the
firmware's reference (``Doxyfile``), in the design of the Kleidos API host,
which serves the firmware's reference beside it. A port of the Kleidos firmware
repository's ``scripts/build_api_reference.py``, read against this tree. Three
steps, stopping at the first that fails:

1. **Doxygen**, with ``Doxyfile.public`` (an overlay of the gate's
   ``Doxyfile``), into a temporary directory. The overlay keeps
   ``WARN_AS_ERROR``, so a warning fails this build as it fails the gate.
2. **The tree a static host serves.** Doxygen writes more than pages: its own
   sheets, scripts and pictures, a crawler's helper, a page per folder.
   :data:`BYPRODUCTS` and :data:`DROPPED_PAGES` are left behind, and a kind of
   file this script does not know stops the build. The images of the documents
   stay: the ones a page draws, which Doxygen copied from ``IMAGE_PATH``, and
   the ones a page links to, which this script copies from there. Each page is
   rewritten where the host's policy (no script, no frame, no inline style)
   would break it, and given the landmarks a reader's tools look for: its
   content in ``<main>``, its title as its one ``<h1>``, the entry of the bar it
   is marked, an image's caption as its text, and a heading the Markdown
   filter named after its page GitHub's anchor beside that name. Then the
   shape a reader gets (``api_reference_pages.py``), the links at the
   addresses the host serves, without ``.html``, with each page's clean
   address as its canonical one (``api_reference_links.py``), each page's
   Markdown twin beside it and the site's ``llms.txt``
   (``api_reference_twins.py``). Those three modules are the firmware
   repository's, copied byte for byte but for the two lines of
   ``scripts/published_reference/typing.patch`` and held to its ``SOURCE.json``
   (``tests/firmware/test_published_reference_copies.py``): never edit them
   here.
3. **The guard**, ``scripts/check_reference.py``, over the tree as written,
   twins included.

The pages are styled by the host, not by this tree: the header
(``docs/published-reference/header.html``) links ``../assets/tokens.css`` and
``../assets/api.css``, which the host serves beside ``meter/``.
``scripts/preview_reference.py`` serves a built tree with them on this
computer.

Usage: ``python3 scripts/build_reference.py --out <new directory>``
(``--doxygen`` names the binary; CI pins 1.17.0). Exit code 0 when the tree is
written and guarded, 1 when a step failed, 2 on a usage error.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS / "published_reference"))
sys.path.insert(0, str(SCRIPTS))

import api_reference_links  # noqa: E402
import api_reference_pages  # noqa: E402
import api_reference_twins  # noqa: E402
import check_reference  # noqa: E402

#: The repository this script lives in.
REPO_ROOT = SCRIPTS.parent

#: The overlay, the directory Doxygen writes the pages into, and the site's
#: directory on the host.
OVERLAY = "Doxyfile.public"
HTML_OUTPUT = "meter"
REFERENCE = "meter"

#: The version the footer states.
VERSION_FILE = REPO_ROOT / "firmware" / "version.txt"

#: Doxygen's own sheets, scripts and pictures, and the files a graph leaves.
BYPRODUCTS = frozenset({".css", ".js", ".md5", ".map", ".cmapx", ".dot"})

#: The kinds of file an image of the documents is.
IMAGE_SUFFIXES = frozenset({".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp"})

#: The pages left behind, each with the words the build counts it under, as
#: the firmware's build leaves them: the list of every link, written for a
#: crawler; a folder's page, which only the index of files (``SHOW_FILES``)
#: would link; the list of a class's members, which says nothing the class's
#: page does not; and the indexes that repeat another.
DROPPED_PAGES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"doxygen_crawl\.html|dir_[0-9a-f]{32}\.html"),
        "a crawler's or a folder's page",
    ),
    (re.compile(r".+-members\.html"), "a class's list of members"),
    (re.compile(r"(?:classes|hierarchy|pages)\.html"), "an index that repeats another"),
)

#: The indexes of members Doxygen splits by letter, and the same indexes cut
#: by kind, which only the tab menu links.
MEMBER_INDEXES = ("functions", "namespacemembers", "globals")
_INDEX_BY_KIND_RE = re.compile(rf"(?:{'|'.join(MEMBER_INDEXES)})_[a-z]{{2,}}(?:_.+)?\.html")
_LETTER_H3_RE = re.compile(r'<h3( class="doxsection")?>(<a id="index_[^"]+"[^>]*></a>.*?)</h3>')
_LETTER_RE = re.compile(r'<a id="(index_[^"]+)" name="[^"]*"></a>(.+?)</h2>')
_INDEX_INTRO_RE = re.compile(r'(<div class="textblock">.*?</div>)', re.S)
CRAWL_PAGE = "doxygen_crawl.html"
_CRAWL_LINK_RE = re.compile(
    rf'<a href="{re.escape(CRAWL_PAGE)}"\s*>\s*</a>|<a href="{re.escape(CRAWL_PAGE)}"\s*/>'
)

#: A tree row's indentation, and the width of one level in Doxygen's pixels.
_SPACER_RE = re.compile(r'<span style="width:(\d+)px;display:inline-block;">&#160;</span>')
SPACER_STEP = 16

#: The tooltip Doxygen writes under an example for each name it links.
_TOOLTIP_RE = re.compile(r'^<div class="ttc" id="[^"]*">.*</div>\n', re.M)

#: A link whose address is a web scheme and no host, and a link Doxygen makes
#: of a word that looks like a scheme (``file:line`` in a code span): both
#: lead nowhere, so each keeps its words and loses its link.
_HOSTLESS_LINK_RE = re.compile(r'<a href="(?:https?|ftp)://"[^>]*>(.*?)</a>', re.S)
_FILE_LINK_RE = re.compile(r'<a href="file:[^"]*"[^>]*>(.*?)</a>', re.S)

#: A link into the tree with a fragment, and an anchor a page holds.
_FRAGMENT_LINK_RE = re.compile(r'href="([^"#:]*)#([^"]+)"')
_ANCHOR_RE = re.compile(r'\bid="([^"]+)"')

#: The page's content, which Doxygen opens as a ``<div>`` and the footer
#: closes as the page's ``<main>``.
_CONTENT_RE = re.compile(r'<div id="doc-content">')
CONTENT_OPEN = '<main id="doc-content">'

#: A page's title, with the groups it is in after it.
_TITLE_RE = re.compile(
    r'<div class="headertitle"><div class="title">(.*?)'
    r'(<div class="ingroups">(?:(?!</?div\b).)*</div>)?</div></div>',
    re.S,
)
_HEAD_NAME_RE = re.compile(r"<title>(.+?) · [^<]*</title>")
TITLE_CLASS = '<h1 class="title">'

#: Where a page is, which becomes a navigation landmark of its own.
_NAVPATH_RE = re.compile(r'<div id="nav-path" class="navpath">(\s*<ul>.*?</ul>\s*)</div>', re.S)
NAVPATH_LABEL = "Where this is"

#: The boxes the sheet lets scroll sideways, which take the keyboard's focus.
_SCROLL_BOX_RE = re.compile(r'<(div|pre|table|figure) class="(fragment|doxtable|diagram)">')

#: A heading of a page's own sections, whose first level is the title's.
_SECTION_H_RE = re.compile(r'<(/?)h([1-5])((?: class="doxsection")?)>')
_SECTION_RE = re.compile(r'<h[1-5] class="doxsection">.*?</h[1-5]>', re.S)
_SECTION_H1 = '<h1 class="doxsection">'

#: The bar, and an entry of it.
_NAV_RE = re.compile(r'<nav class="api-nav".*?</nav>', re.S)
_NAV_LINK_RE = re.compile(r'<a href="([^"#]+)"')

#: An image of a page and the caption Doxygen writes under it, apart from it.
_IMAGE_RE = re.compile(
    r'(<div class="image">\s*<img src="[^"]*") alt=""(\s*/>\s*)'
    r'<div class="caption">(.*?)</div>',
    re.S,
)
_TAG_RE = re.compile(r"<[^>]+>")

#: The anchor of a heading the Markdown filter named after its page, because
#: another page has a heading of the same words (``doxygen_md_filter.py``,
#: ``shared_ids``).
_RENAMED_ANCHOR_RE = re.compile(r'<a class="anchor" id="([a-z0-9_]+_page)-([^"]+)"></a>')

#: A link to an image of the documents, beside the page.
_IMAGE_LINK_RE = re.compile(r'\bhref="([^"/:#?]+\.(?:png|svg|jpe?g|gif|webp))"')


def project_number() -> str:
    """Return the version the footer states (``firmware/version.txt``)."""
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def image_dirs(root: Path = REPO_ROOT) -> list[Path]:
    """Return the folders of ``Doxyfile``'s ``IMAGE_PATH``, from *root*."""
    text = (root / "Doxyfile").read_text(encoding="utf-8")
    match = re.search(r"^IMAGE_PATH\s*=\s*((?:.*\\\n)*.*)$", text, re.M)
    if match is None:
        return []
    return [root / part for part in match.group(1).replace("\\\n", " ").split()]


def images_by_name(root: Path = REPO_ROOT) -> dict[str, Path]:
    """Return every image of ``IMAGE_PATH`` by its file's name."""
    found: dict[str, Path] = {}
    for folder in image_dirs(root):
        for path in sorted(folder.iterdir()) if folder.is_dir() else []:
            if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES:
                found.setdefault(path.name, path)
    return found


def _captioned(match: re.Match[str]) -> str:
    """Return an image whose text is its caption, and the caption as a paragraph.

    Doxygen 1.17.0 writes a Markdown image's words as a caption beside it and
    leaves its ``alt`` empty, which says the image is decorative; the
    caption's plain words become its text. The caption becomes a paragraph,
    whose margin the host's sheet sets, so the text after it keeps its
    distance.
    """
    words = " ".join(html.unescape(_TAG_RE.sub("", match.group(3))).split())
    return (
        f'{match.group(1)} alt="{html.escape(words, quote=True)}"{match.group(2)}'
        f'<p class="caption">{match.group(3)}</p>'
    )


def github_anchors(name: str, text: str) -> str:
    """Return a page whose renamed headings also hold GitHub's anchor.

    A heading another page shares is named ``<page>-<anchor>`` by the
    Markdown filter, as Doxygen keeps one set of names for the whole site;
    every link of the site is written to that name. An address typed or
    linked from outside in GitHub's form (``scpi_page#the-pin-map``) lands on
    the heading too, by a second anchor of GitHub's name, unless the page
    holds that name already.
    """
    page = name.removesuffix(".html")
    held = set(_ANCHOR_RE.findall(text))

    def alias(match: re.Match[str]) -> str:
        if match.group(1) != page or match.group(2) in held:
            return match.group(0)
        held.add(match.group(2))
        return f'{match.group(0)}<a class="anchor" id="{match.group(2)}"></a>'

    return _RENAMED_ANCHOR_RE.sub(alias, text)


def _spacer(match: re.Match[str]) -> str:
    """Return a tree row's indentation as one classed span per level."""
    depth = max(1, round(int(match.group(1)) / SPACER_STEP))
    return '<span class="tree-pad"></span>' * depth


def _heading(match: re.Match[str]) -> str:
    """Return a page's title as its ``<h1>``, with its groups after it."""
    return (
        f'<div class="headertitle">{TITLE_CLASS}{match.group(1)}</h1>{match.group(2) or ""}</div>'
    )


def _titled(text: str) -> str:
    """Return a page with a title: its own, or the one its head gives it."""
    name = _HEAD_NAME_RE.search(text)
    if TITLE_CLASS in text or name is None:
        return text
    header = (
        f'{CONTENT_OPEN}\n<div class="header">\n  <div class="headertitle">'
        f"{TITLE_CLASS}{name.group(1)}</h1></div>\n</div><!--header-->"
    )
    return text.replace(CONTENT_OPEN, header, 1)


def _one_first_heading(text: str) -> str:
    """Return a page whose sections are a level under its title, where one was not."""
    if _SECTION_H1 not in text:
        return text

    def lower(match: re.Match[str]) -> str:
        return _SECTION_H_RE.sub(
            lambda each: f"<{each[1]}h{int(each[2]) + 1}{each[3]}>", match.group(0)
        )

    return _SECTION_RE.sub(lower, text)


def mark_current(name: str, text: str) -> str:
    """Return a page with its entry of the bar marked as the current one."""

    def mark(match: re.Match[str]) -> str:
        target = match.group(1)
        if target == name:
            return f'{match.group(0)} aria-current="page"'
        stem = target.removesuffix(".html")
        if stem in MEMBER_INDEXES and name.startswith(stem + "_"):
            return f'{match.group(0)} aria-current="true"'
        return match.group(0)

    return _NAV_RE.sub(lambda nav: _NAV_LINK_RE.sub(mark, nav.group(0)), text, count=1)


def rewrite_page(text: str) -> str:
    """Return a page with what the host's policy would block rewritten, and its landmarks."""
    text = _one_first_heading(text)
    text = _CRAWL_LINK_RE.sub("", text)
    text = _TOOLTIP_RE.sub("", text)
    text = _HOSTLESS_LINK_RE.sub(r"\1", text)
    text = _FILE_LINK_RE.sub(r"\1", text)
    text = _CONTENT_RE.sub(CONTENT_OPEN, text)
    text = _NAVPATH_RE.sub(
        rf'<nav id="nav-path" class="navpath" aria-label="{NAVPATH_LABEL}">\1</nav>', text
    )
    text = _titled(_TITLE_RE.sub(_heading, text))
    text = _LETTER_H3_RE.sub(r"<h2\1>\2</h2>", text)
    text = _SCROLL_BOX_RE.sub(r'<\1 class="\2" tabindex="0">', text)
    text = _IMAGE_RE.sub(_captioned, text)
    return _SPACER_RE.sub(_spacer, text)


def doxygen_anchor(fragment: str) -> str:
    """Return *fragment* as Doxygen spells an anchor: each other character as ``_XX``."""
    return "".join(c if c.isalnum() or c == "_" else f"_{ord(c):02X}" for c in fragment)


def repair_fragments(pages: dict[str, str]) -> None:
    """Point each link at the anchor its page holds, where Doxygen spelled the two apart."""
    anchors = {name: set(_ANCHOR_RE.findall(text)) for name, text in pages.items()}
    for name, text in pages.items():

        def repair(match: re.Match[str], here: str = name) -> str:
            target, fragment = match.group(1), match.group(2)
            held = anchors.get(target or here)
            if held is None or fragment in held:
                return match.group(0)
            spelled = doxygen_anchor(fragment)
            return f'href="{target}#{spelled}"' if spelled in held else match.group(0)

        pages[name] = _FRAGMENT_LINK_RE.sub(repair, text)


def index_of(name: str) -> str:
    """Return the first page of the index by letter that *name* is of, or nothing."""
    stem = name.removesuffix(".html").split("_", 1)[0]
    return f"{stem}.html" if stem in MEMBER_INDEXES else ""


def add_letter_rows(pages: dict[str, str]) -> None:
    """Give each member index split by letter the row of letters that links its parts."""
    for index in MEMBER_INDEXES:
        family = sorted(name for name in pages if re.fullmatch(rf"{index}(?:_[^_]+)?\.html", name))
        if len(family) < 2 or all('class="qindex"' in pages[name] for name in family):
            continue
        letters = [
            f'<a class="qindex" href="{name}#{anchor}">{label.upper()}</a>'
            for name in family
            for anchor, label in _LETTER_RE.findall(pages[name])
        ]
        row = '<div class="qindex">' + "".join(letters) + "</div>"
        for name in family:
            if 'class="qindex"' not in pages[name]:
                pages[name] = _INDEX_INTRO_RE.sub(rf"\1\n{row}", pages[name], count=1)


#: A letter of an index's row that links an anchor of its own page, and a
#: row left with no letter.
_OWN_LETTER_RE = re.compile(r'<a class="qindex"[^>]*href="#([^"]+)"[^>]*>[^<]*</a>')
_EMPTY_ROW_RE = re.compile(r'<nav class="letters"[^>]*>\s*</nav>|<div class="qindex">\s*</div>')


def drop_dead_letters(text: str) -> str:
    """Return an index without the letters of its row that link no heading of its page.

    Doxygen 1.17.0 writes the row of letters of a member index that fits on one
    page, and none of the headings it links (``globals.html`` of this tree);
    a letter that leads nowhere goes, and a row with none left goes with it.
    """
    held = set(_ANCHOR_RE.findall(text))
    text = _OWN_LETTER_RE.sub(lambda m: m.group(0) if m.group(1) in held else "", text)
    return _EMPTY_ROW_RE.sub("", text)


def same_but_for_capitals(names: Sequence[str]) -> list[str]:
    """Return the names that another of *names* is, but for its capitals."""
    folded = Counter(name.casefold() for name in names)
    return sorted(name for name in names if folded[name.casefold()] > 1)


def stage_tree(raw: Path, out: Path, images: dict[str, Path]) -> tuple[Counter[str], list[str]]:
    """Write the tree a host serves from what Doxygen wrote into *raw*.

    :param raw: Doxygen's HTML output directory.
    :param out: The directory to write, which must be empty.
    :param images: The images of the documents by name (:func:`images_by_name`).
    :returns: How many files of each kind were left behind, and the files of a
        kind this script does not know, a link to an image that is not one of
        the documents', or a name only capitals tell from another (which fail
        the build).
    """
    left: Counter[str] = Counter()
    unknown: list[str] = []
    names: list[str] = []
    for path in sorted(raw.iterdir()):
        if path.is_dir():
            unknown.append(path.name + "/")
        elif path.suffix == ".html":
            dropped = next((why for kind, why in DROPPED_PAGES if kind.fullmatch(path.name)), None)
            if dropped is not None:
                left[dropped] += 1
            elif _INDEX_BY_KIND_RE.fullmatch(path.name):
                left["a member index by kind"] += 1
            else:
                names.append(path.name)
        elif path.suffix.lower() in IMAGE_SUFFIXES:
            if path.name in images:
                shutil.copyfile(path, out / path.name)
            else:
                left["Doxygen's own picture"] += 1
        elif path.suffix in BYPRODUCTS:
            left[path.suffix] += 1
        else:
            unknown.append(path.name)
    unknown += same_but_for_capitals(names)
    pages: dict[str, str] = {}
    for name in names:
        text = rewrite_page((raw / name).read_text(encoding="utf-8"))
        pages[name] = mark_current(name, api_reference_pages.shaped(name, text))
    add_letter_rows(pages)
    repair_fragments(pages)
    for name, text in pages.items():
        pages[name] = github_anchors(name, text)
    for name, text in pages.items():
        for image in _IMAGE_LINK_RE.findall(text):
            if image in images:
                if not (out / image).exists():
                    shutil.copyfile(images[image], out / image)
            else:
                unknown.append(f"{name}: a link to {image}, which is no image of the documents")
        done = drop_dead_letters(api_reference_pages.finished(name, text, index_of(name)))
        (out / name).write_text(api_reference_links.finish(name, done, REFERENCE), encoding="utf-8")
    write_twins(out, sorted(pages))
    return left, unknown


def write_twins(out: Path, names: Sequence[str]) -> None:
    """Write each page's Markdown twin beside it, and the site's ``llms.txt``."""
    entries = []
    for name in names:
        page = (out / name).read_text(encoding="utf-8")
        twin = api_reference_twins.convert(name, page, REFERENCE)
        (out / api_reference_twins.twin_name(name)).write_text(twin, encoding="utf-8")
        entries.append(api_reference_twins.entry(name, page, twin))
    index = (out / api_reference_links.INDEX).read_text(encoding="utf-8")
    topics = (
        (out / "topics.html").read_text(encoding="utf-8") if (out / "topics.html").is_file() else ""
    )
    (out / api_reference_twins.LLMS).write_text(
        api_reference_twins.llms_file(entries, index, topics, REFERENCE), encoding="utf-8"
    )


def run_doxygen(doxygen: str, output: Path) -> int:
    """Run Doxygen with the overlay into *output*; say what it said; return its exit code."""
    config = (
        f"@INCLUDE = {OVERLAY}\n"
        f'OUTPUT_DIRECTORY = "{output}"\n'
        f"HTML_OUTPUT = {HTML_OUTPUT}\n"
        f'PROJECT_NUMBER = "{project_number()}"\n'
    )
    done = subprocess.run(
        [doxygen, "-"], input=config, text=True, cwd=REPO_ROOT, stderr=subprocess.PIPE, check=False
    )
    sys.stderr.write(done.stderr)
    if done.returncode == 0 and done.stderr.strip():
        return 1
    return done.returncode


def build(out: Path, doxygen: str) -> int:
    """Build, stage and guard the site; return the exit code."""
    if shutil.which(doxygen) is None:
        print(f"build_reference: no {doxygen} on the path", file=sys.stderr)
        return 2
    if out.exists() and any(out.iterdir()):
        print(f"build_reference: {out} is not empty", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kleidos-meter-reference-") as tmp:
        output = Path(tmp).resolve()
        code = run_doxygen(doxygen, output)
        if code != 0:
            print(f"build_reference: doxygen failed (exit {code})", file=sys.stderr)
            return 1
        left, unknown = stage_tree(output / HTML_OUTPUT, out, images_by_name())
    for name in unknown:
        print(f"build_reference: {name}: a kind of file nobody decided to publish or drop")
    kept = Counter(path.suffix for path in out.iterdir())
    print(
        f"build_reference: {kept['.html']} pages, {kept['.md']} twins and"
        f" {sum(kept[s] for s in IMAGE_SUFFIXES)} images in {out}; left behind: "
        + (", ".join(f"{count} {kind}" for kind, count in sorted(left.items())) or "nothing")
        + "."
    )
    if unknown:
        return 1
    return check_reference.run(out)


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments and build."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--out", type=Path, required=True, help="the directory to write (empty)")
    parser.add_argument("--doxygen", default="doxygen", help="the Doxygen binary")
    args = parser.parse_args(argv)
    return build(args.out.resolve(), args.doxygen)


if __name__ == "__main__":
    sys.exit(main())
