#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""
check_doc_links.py — what the firmware reference leaves broken without a warning.

A port of the Kleidos firmware repository's gate of the same name, read against
this tree: the sources are ``firmware/src`` (``SRC_DIR``), the reference is
built from the root ``Doxyfile`` into ``build/doxygen``. The Doxyfile reads the
Markdown pages through ``scripts/doxygen_md_filter.py`` (``FILTER_PATTERNS``),
which writes each page link as an HTML link that Doxygen does not check, so a
link to a heading a page does not have is this gate's to find
(``broken-link``). A link to a file the repository does not hold never reaches
the HTML as one: the filter says it on stderr and writes a reference Doxygen
cannot resolve, and this gate reads both lines among the run's warnings
(``xml-warning``). The self-test's tree holds no Markdown page, so the filter
never runs there and the tree carries no copy of it.

Doxygen's own gate fails on a malformed comment. It prints nothing when the
reference it wrote links to a page it never generated, shows a class twice, or
gives two topics one title. This gate reads a finished build, its HTML and its
XML, and the sources it was built from, and fails on:

* **broken-link** — an internal link (page, graph, sidebar, search index) whose
  page or anchor does not exist. The usual cause is a function a header
  declares with no group, in a namespace with no documentation of its own: its
  home is then the ``.cpp`` that defines it, which has no page. The fix is an
  ``@addtogroup`` wrap opened INSIDE the namespace.
* **pageless-class** — a class with documented public members and no comment of
  its own, so ``HIDE_UNDOC_CLASSES`` drops it and its members render nowhere.
* **glued-colon** — ``@ref name:``. Doxygen takes one trailing colon into the
  name, so the target is ``name:`` and never resolves. Read from the sources,
  so it is seen in code the Doxyfile's preprocessor drops too.
* **group-…** — a group id defined twice, defined outside ``docs/groups.dox``,
  joined but never defined, defined but never joined, an ``@addtogroup`` that
  carries a title, and two ids sharing one title.
* **member-group-title-twice** — an ``@name`` title a page shows more than
  once (a topic page gathers the sections of every header in the group).
* **using-copy** — a class Doxygen documented a second time because a
  using-declaration at namespace scope imported it (``using a::Thing;`` gives
  ``Thing`` a second compound under the importing namespace). Write the
  qualified name, or ``using namespace a;`` where a short name is needed.
* **shadowed-name** — a type or a constant declared at namespace scope under a
  name an enclosing namespace also declares, so the bare name means one or the
  other by where it is written (``relay::kChannelCount`` beside the meter's
  ``kChannelCount`` of measured channels was one). An alias of the outer one is
  the same thing under a second name and passes. Functions are not compared:
  a ``begin`` in two modules is not a defect, and C++ overloads by design.
* **file-basename** — two files of one name under ``firmware/src``, which
  Doxygen tells apart only by path: an ``#include`` or a ``@ref file.h`` of
  that name can link to the other one.
* **xml-warning** — a warning of this gate's own Doxygen run: one only the
  XML writer prints (it parses the comments of private members, which the HTML
  writer skips), and the filter's for a link to a file that is not there.

Three kinds of broken link are written by Doxygen 1.17.0 itself and no comment
can fix them; ``GENERATOR_CAUSES`` names them and they are counted, not failed.
Two are the Kleidos gate's; the third, an empty navigation-index key for the
related-pages index, is seen only in a tree with no related page, as this one
was until its Markdown documents became pages of it, and as the self-test's
probe tree still is.

Doxygen never deletes what an earlier run wrote, so an output directory that
has been built more than once also holds the pages of symbols that no longer
exist. ``doxygen_crawl.html`` lists exactly the pages of the last run; a page it
does not list is not read and is not a valid link target.

Types and constants of one name in sibling namespaces (an ``a::Phase`` beside
a ``b::Phase``) are the house idiom: the namespace says which one. They are
counted, listed by ``--report``, and never failed.

Usage:
    python3 scripts/check_doc_links.py                       # build HTML + XML, check
    python3 scripts/check_doc_links.py --html build/doxygen/html
                                                             # check that HTML; build XML only
    python3 scripts/check_doc_links.py --report              # also list what is only counted
    python3 scripts/check_doc_links.py --json FILE           # write every finding
    python3 scripts/check_doc_links.py --selftest            # plant each defect, see it caught

Exit code:
    0 — nothing to fix.
    1 — at least one finding, or Doxygen is missing or failed (the gate then
        measured nothing, which is not a pass).
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

#: Appended to the tree's Doxyfile. Warnings must not stop the run: this gate
#: reads them (``xml-warning``) instead of dying on the first.
COMMON_OVERRIDES = [
    "GENERATE_XML = YES",
    "XML_OUTPUT = xml",
    "XML_PROGRAMLISTING = NO",
    "GENERATE_LATEX = NO",
    "WARN_AS_ERROR = NO",
    "QUIET = YES",
]


class DoxygenFailed(RuntimeError):
    """Doxygen is missing or did not finish, so nothing was measured."""


def run_doxygen(root: Path, out: Path, with_html: bool) -> list[str]:
    """Build ``root``'s reference into ``out`` and return the warning lines.

    :param root: Tree holding ``Doxyfile``, ``firmware/src/`` and ``docs/``.
    :param out: Output directory; receives ``xml/`` and, with ``with_html``,
        ``html/``.
    :param with_html: Write the HTML too. Without it the run is XML only, with
        no graphs, for a caller that already holds the HTML.
    :raises DoxygenFailed: no ``doxygen`` on PATH, or it wrote no XML index.
    """
    overrides = list(COMMON_OVERRIDES) + [f'OUTPUT_DIRECTORY = "{out}"']
    if with_html:
        overrides += ["GENERATE_HTML = YES", "HTML_OUTPUT = html"]
    else:
        overrides += ["GENERATE_HTML = NO", "HAVE_DOT = NO"]
    config = (root / "Doxyfile").read_text(encoding="utf-8") + "\n" + "\n".join(overrides) + "\n"
    cfg_path = out / "Doxyfile.check"
    cfg_path.write_text(config, encoding="utf-8")
    try:
        result = subprocess.run(
            ["doxygen", str(cfg_path)], cwd=root, capture_output=True, text=True, check=False
        )
    except FileNotFoundError as exc:
        raise DoxygenFailed("doxygen is not on PATH") from exc
    if not (out / "xml" / "index.xml").is_file():
        raise DoxygenFailed("doxygen wrote no XML:\n" + result.stderr[-2000:])
    return result.stderr.splitlines()


# ---------------------------------------------------------------------------
# HTML side: which pages and anchors exist, and which links miss them
# ---------------------------------------------------------------------------

HREF_RE = re.compile(r"""(?:href|xlink:href|src)\s*=\s*(["'])(.*?)\1""", re.I)
ANCHOR_RE = re.compile(r"""\b(?:id|name)\s*=\s*(["'])(.*?)\1""", re.I)
JS_LINK_RE = re.compile(r"""["']((?:\.\./)?[\w\-.~]+\.html(?:#[\w\-.:]+)?)["']""")
#: What follows a key of ``navtreeindex*.js`` that has no tree path: ``"x.html":[]``.
EMPTY_NAV_KEY_RE = re.compile(r"\s*:\s*\[\s*\]")
#: The surface of such a key: a lookup entry, not a link anything navigates by.
EMPTY_NAV_KEY = "nav-index-empty"
EXTERNAL_RE = re.compile(r"^(?:[a-z][a-z0-9+.\-]*:|//)", re.I)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


CRAWL_PAGE = "doxygen_crawl.html"
GRAPH_SRC_RE = re.compile(r"""\bsrc\s*=\s*(["'])([\w\-.~]+\.svg)\1""", re.I)


class HtmlTree:
    """The generated HTML tree: which pages exist and which anchors each has.

    Doxygen never deletes what an earlier run wrote, so an output directory
    that has been built twice holds the pages of symbols that no longer exist,
    with links that were right when they were written. ``doxygen_crawl.html``
    is rewritten on every run and lists exactly the pages of that run: a page
    it does not list is ``stale``, is not read, and is not a valid target.
    """

    def __init__(self, root: Path, images: frozenset[str] = frozenset()) -> None:
        self.root = root
        on_disk = {p.name for p in root.iterdir() if p.is_file()}
        html = {name for name in on_disk if name.endswith(".html")}
        if CRAWL_PAGE in on_disk:
            listed = {
                m.group(2).partition("#")[0] for m in HREF_RE.finditer(read_text(root / CRAWL_PAGE))
            }
            self.pages = (html & listed) | {CRAWL_PAGE}
        else:
            self.pages = html
        self.stale = sorted(html - self.pages)
        stale_stems = {name[:-5] for name in self.stale}
        #: What a link may point at, and what is read for links: this run's
        #: pages, and every other file that does not belong to a stale page.
        self.files = self.pages | {
            name
            for name in on_disk
            if not name.endswith(".html") and name.rpartition(".")[0] not in stale_stems
        }
        #: An image of the documents a page links to (not one it draws, which
        #: Doxygen copies): the published build copies it beside the pages
        #: (``scripts/build_reference.py``), so it is a target here too.
        self.files |= images
        self._anchors: dict[str, set[str]] = {}
        self._homes: dict[str, list[str]] | None = None

    def graphs(self) -> set[str]:
        """The graph files this run's pages embed (and their full-size twins).

        Doxygen keeps a graph whose content did not change and never deletes
        one, so the directory also holds the graphs of earlier runs. A page
        embeds its graphs in ``<iframe src="….svg">``; a ``.map`` file is an
        intermediate that no page loads.
        """
        found: set[str] = set()
        for page in self.pages:
            for m in GRAPH_SRC_RE.finditer(read_text(self.root / page)):
                found.add(m.group(2))
                found.add(m.group(2)[:-4] + "_org.svg")
        return found & self.files

    def anchors(self, page: str) -> set[str]:
        got = self._anchors.get(page)
        if got is None:
            got = {m.group(2) for m in ANCHOR_RE.finditer(read_text(self.root / page))}
            self._anchors[page] = got
        return got

    def homes(self, frag: str) -> list[str]:
        """The pages that do carry anchor ``frag``."""
        if self._homes is None:
            self._homes = collections.defaultdict(list)
            for name in sorted(self.pages):
                if name != CRAWL_PAGE:
                    for anchor in self.anchors(name):
                        self._homes[anchor].append(name)
        return self._homes.get(frag, [])

    def missing(self, target: str) -> str | None:
        """None when ``target`` (``page[#anchor]``) exists, else what is missing."""
        page, _, frag = target.partition("#")
        if page not in self.files:
            return "page"
        if frag and page.endswith(".html") and frag not in self.anchors(page):
            return "anchor"
        return None


#: The ``IMAGE_PATH`` setting of a Doxyfile, its folders on one line or many.
_IMAGE_PATH_RE = re.compile(r"^IMAGE_PATH\s*=\s*((?:.*\\\n)*.*)$", re.M)


def document_images(root: Path) -> frozenset[str]:
    """The file names of the images in ``root``'s ``Doxyfile`` ``IMAGE_PATH`` folders."""
    doxyfile = root / "Doxyfile"
    if not doxyfile.is_file():
        return frozenset()
    match = _IMAGE_PATH_RE.search(read_text(doxyfile))
    if match is None:
        return frozenset()
    folders = [root / part for part in match.group(1).replace("\\\n", " ").split()]
    return frozenset(
        path.name
        for folder in folders
        if folder.is_dir()
        for path in folder.iterdir()
        if path.is_file()
    )


def surface_of(name: str) -> str:
    """Where a link sits: a page, a graph, the sidebar tree, a page's nav data."""
    if name == CRAWL_PAGE:
        return "crawl"
    if name.endswith(".html"):
        return "page"
    if name.endswith(".svg"):
        return "graph"
    if name.startswith("navtree"):
        return "sidebar"
    return "page-nav"


def scan_html_links(tree: HtmlTree) -> list[dict[str, Any]]:
    """Every internal link in the tree whose page or anchor is missing."""
    broken: list[dict[str, Any]] = []

    def consider(source: str, surface: str, raw: str) -> None:
        link = raw.strip()
        if link.startswith("$"):  # graph maps prefix the relative path with $
            link = link[1:]
        if not link or EXTERNAL_RE.match(link) or link.startswith(("data:", "javascript:")):
            return
        if link.startswith("../"):
            link = link[3:]
        page, _, frag = link.partition("#")
        page = page.split("?", 1)[0]
        if not page:
            page = source if source.endswith(".html") else ""
            if not page or not frag:
                return
        if "/" in page:  # search/, a stylesheet folder: not a doc page
            return
        target = page + ("#" + frag if frag else "")
        what = tree.missing(target)
        if what:
            broken.append({"from": source, "surface": surface, "to": target, "missing": what})

    for name in sorted(tree.pages | tree.graphs()):
        for m in HREF_RE.finditer(read_text(tree.root / name)):
            consider(name, surface_of(name), m.group(2))
    for name in sorted(tree.files):
        if name.endswith(".js"):
            text = read_text(tree.root / name)
            for m in JS_LINK_RE.finditer(text):
                surface = surface_of(name)
                if name.startswith("navtreeindex") and EMPTY_NAV_KEY_RE.match(text, m.end()):
                    surface = EMPTY_NAV_KEY
                consider(name, surface, m.group(1))
    search = tree.root / "search"
    if search.is_dir():
        for path in sorted(search.glob("*.js")):
            for m in JS_LINK_RE.finditer(read_text(path)):
                consider("search/" + path.name, "search", m.group(1))
    return broken


# ---------------------------------------------------------------------------
# XML side: what exists, where it is declared, what it is called
# ---------------------------------------------------------------------------


def text_of(node: ET.Element | None) -> str:
    return "" if node is None else "".join(node.itertext()).strip()


def documented(node: ET.Element) -> bool:
    return bool(text_of(node.find("briefdescription")) or text_of(node.find("detaileddescription")))


def scope_of(qualified: str) -> str:
    return qualified.rpartition("::")[0]


SCOPE_KINDS = ("class", "struct", "union", "namespace")
CLASS_KINDS = ("class", "struct", "union")


class Model:
    """What the XML says exists: compounds, members, their scopes and sections."""

    def __init__(self, xml_root: Path) -> None:
        self.compounds: dict[str, dict[str, Any]] = {}
        self.members: dict[str, dict[str, Any]] = {}
        self.sections: list[dict[str, Any]] = []
        self.inner_class_of_file: dict[str, set[str]] = collections.defaultdict(set)
        for path in sorted(xml_root.glob("*.xml")):
            if path.name in ("index.xml", "Doxyfile.xml"):
                continue
            try:
                doc = ET.parse(path).getroot()  # noqa: S314  Doxygen's XML of our own sources
            except ET.ParseError as exc:
                raise DoxygenFailed(f"{path.name}: unreadable XML: {exc}") from exc
            for cdef in doc.findall("compounddef"):
                self._compound(cdef)

    def _compound(self, cdef: ET.Element) -> None:
        cid = cdef.get("id", "")
        kind = cdef.get("kind", "")
        loc = cdef.find("location")
        entry = {
            "id": cid,
            "kind": kind,
            "name": text_of(cdef.find("compoundname")),
            "title": text_of(cdef.find("title")),
            "file": loc.get("file", "") if loc is not None else "",
            "line": int(loc.get("line", "0")) if loc is not None else 0,
            "documented": documented(cdef),
            "members": [],
        }
        self.compounds[cid] = entry
        if kind == "file":
            for node in cdef.findall("innerclass"):
                self.inner_class_of_file[node.get("refid", "")].add(str(entry["file"]))
        for sect in cdef.findall("sectiondef"):
            if sect.get("kind") == "user-defined":
                first = sect.find("memberdef")
                floc = first.find("location") if first is not None else None
                self.sections.append(
                    {
                        "compound": cid,
                        "header": text_of(sect.find("header")),
                        "file": floc.get("file", "") if floc is not None else "",
                        "line": int(floc.get("line", "0")) if floc is not None else 0,
                    }
                )
            for mdef in sect.findall("memberdef"):
                self._member(cdef, mdef)

    def _member(self, cdef: ET.Element, mdef: ET.Element) -> None:
        mid = mdef.get("id", "")
        cid = cdef.get("id", "")
        self.compounds[cid]["members"].append(mid)
        if mid in self.members:
            return
        name = text_of(mdef.find("name"))
        qualified = text_of(mdef.find("qualifiedname"))
        if not qualified:
            ckind = cdef.get("kind", "")
            scope = text_of(cdef.find("compoundname")) if ckind in SCOPE_KINDS else ""
            qualified = f"{scope}::{name}" if scope else name
        loc = mdef.find("location")
        self.members[mid] = {
            "id": mid,
            "kind": mdef.get("kind", ""),
            "name": name,
            "qualified": qualified,
            "prot": mdef.get("prot", ""),
            "file": loc.get("file", "") if loc is not None else "",
            "line": int(loc.get("line", "0")) if loc is not None else 0,
            "documented": documented(mdef),
            # what a typedef stands for, and what a constant is set to: an
            # alias of the outer name is not a second thing of that name
            "stands_for": text_of(mdef.find("type")) + " " + text_of(mdef.find("initializer")),
        }

    def entity(self, refid: str) -> dict[str, Any] | None:
        return self.compounds.get(refid) or self.members.get(refid)


# ---------------------------------------------------------------------------
# Sources: group commands, refs, file names
# ---------------------------------------------------------------------------

GROUP_CMD_RE = re.compile(r"(?<![@\\`])[@\\](defgroup|addtogroup|weakgroup|ingroup)[ \t]+([^\n]*)")
#: The name Doxygen's lexer takes after \ref: an identifier path whose last
#: identifier may swallow ONE trailing colon (REFWORD2_PRE in doctokenizer.l).
REF_NAME = r"(?:\#|::)?[A-Za-z_$~][\w$]*(?:(?:::|\.|\#|/|-)[A-Za-z_$~][\w$]*)*"
GLUED_REF_RE = re.compile(r"(?<![@\\`])[@\\]ref[ \t]+(" + REF_NAME + r"):(?!:)")
#: A using-declaration at namespace or file scope, as the Doxyfile's input
#: filter matches it (column 0); an indented one is class or block scope.
USING_DECL_RE = re.compile(r"^\s*using\s+(?:::)?[A-Za-z_]\w*(?:::[A-Za-z_]\w*)+\s*;")
SOURCE_SUFFIXES = (".h", ".hpp", ".c", ".cc", ".cpp", ".dox")
EXCLUDED_PARTS = ("/generated/", "/managed_components/", "/.pio/", "/build/")
GROUPS_DOX = "docs/groups.dox"
#: The firmware sources the Doxyfile reads (path from the tree's root).
SRC_DIR = "firmware/src"


def source_files(root: Path) -> list[Path]:
    """The files the Doxyfile reads that hold comments: ``firmware/src/`` and ``docs/*.dox``.

    ``firmware/src`` is walked to the bottom although the Doxyfile reads it
    flat (``RECURSIVE = NO``), so a file in a subfolder still counts toward
    ``file-basename``.
    """
    found: list[Path] = []
    for start in (root / SRC_DIR, *sorted((root / "docs").glob("*.dox"))):
        if start.is_file():
            found.append(start)
        elif start.is_dir():
            for path in start.rglob("*"):
                rel = "/" + path.relative_to(root).as_posix()
                if (
                    path.suffix in SOURCE_SUFFIXES
                    and path.is_file()
                    and not any(part in rel for part in EXCLUDED_PARTS)
                    and not path.name.endswith(("_gen.h", "_gen.cpp"))
                ):
                    found.append(path)
    return sorted(found)


def doc_start(line: str, in_block: bool) -> tuple[int, bool]:
    """Where on ``line`` Doxygen comment text starts (-1: none), and the block state after."""
    start = -1
    if in_block:
        start = 0
    else:
        m = re.search(r"/\*[*!](?!/)|//[/!](?!/)", line)
        if m:
            start = m.start()
            in_block = line[m.start() : m.start() + 2] == "/*"
    if in_block and "*/" in line[max(start, 0) :]:
        in_block = False
    return start, in_block


def scan_sources(root: Path) -> dict[str, Any]:
    """Group commands, glued refs and file names, read from the sources."""
    defs: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    adds: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    ingroups: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    glued: list[dict[str, Any]] = []
    basenames: dict[str, list[str]] = collections.defaultdict(list)
    lines_of: dict[str, list[str]] = {}
    for path in source_files(root):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(SRC_DIR + "/"):
            basenames[path.name].append(rel)
        lines = read_text(path).splitlines()
        lines_of[rel] = lines
        in_block = False
        for number, line in enumerate(lines, 1):
            start, in_block = doc_start(line, in_block)
            if start < 0:
                continue
            text = line[start:]
            for m in GROUP_CMD_RE.finditer(text):
                cmd, rest = m.group(1), m.group(2).strip().rstrip("*/").strip()
                if cmd == "ingroup":
                    for gid in rest.split():
                        if re.fullmatch(r"[A-Za-z_]\w*", gid):
                            ingroups[gid].append({"file": rel, "line": number})
                    continue
                gid, _, title = rest.partition(" ")
                row = {"file": rel, "line": number, "title": title.strip().strip('"')}
                (defs if cmd == "defgroup" else adds)[gid].append(row)
            glued.extend(
                {"file": rel, "line": number, "name": m.group(1)}
                for m in GLUED_REF_RE.finditer(text)
            )
    return {
        "defgroup": defs,
        "addtogroup": adds,
        "ingroup": ingroups,
        "glued": glued,
        "basenames": {k: v for k, v in basenames.items() if len(v) > 1},
        "lines": lines_of,
    }


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------

#: Broken links Doxygen 1.17.0 writes by itself. No comment fixes them, so
#: they are counted and not failed. Each rule is as narrow as the defect:
#:
#: * ``generator-nav-entry-at-file-page`` — a member that belongs to a group
#:   (an enum's values, chiefly) is anchored on the group page, but the file
#:   page's sidebar data and ``doxygen_crawl.html`` point it at the file page.
#:   Only those two surfaces, only a file page, only an anchor that does live
#:   on a group page.
#: * ``generator-index-letter`` — a member index prints its A-Z bar with
#:   ``#index_x`` links and gives no element that id (every letter of an index
#:   short enough for one page, and ``:`` and ``~`` of a paged one). Only an
#:   ``#index_…`` link from one index page to another, or to itself.
#: * ``generator-empty-nav-index-key`` — a tree with no related page gets no
#:   ``pages.html``, yet the sidebar's index still carries the key
#:   ``"pages.html":[]``, with no tree path. Seen with and without
#:   ``@mainpage`` and with the pages tab hidden by a layout file, so no comment
#:   or setting removes it. Only a page that does not exist, and only when every
#:   mention of it is such an empty key: a key with a path is a link and fails.
GENERATOR_CAUSES = (
    "generator-nav-entry-at-file-page",
    "generator-index-letter",
    "generator-empty-nav-index-key",
)
INDEX_PAGE_RE = re.compile(r"^(functions|globals|namespacemembers|classes)[\w~]*\.html$")
FILE_PAGE_RE = re.compile(r"_8(h|hpp|c|cc|cpp)\.html$")


def cause_of(
    target: str, missing: str, sources: set[str], surfaces: set[str], model: Model, tree: HtmlTree
) -> tuple[str, str]:
    """Why a link target is missing, and the symbol it stands for."""
    page, _, frag = target.partition("#")
    stem = page[:-5] if page.endswith(".html") else page
    comp = model.compounds.get(stem)
    ent = model.entity(f"{stem}_1{frag}" if frag else stem)
    symbol = (ent.get("qualified") or ent.get("name") or "") if ent else ""
    if missing == "page":
        if surfaces == {EMPTY_NAV_KEY}:
            return "generator-empty-nav-index-key", symbol
        if comp is not None and comp["kind"] == "file":
            return "member-on-pageless-source-file", symbol or comp["file"]
        if comp is not None and comp["kind"] in CLASS_KINDS:
            return "member-of-pageless-class", symbol or comp["name"]
        return "page-never-generated", symbol
    homes = tree.homes(frag)
    if (
        frag.startswith("index_")
        and INDEX_PAGE_RE.match(page)
        and all(INDEX_PAGE_RE.match(source) for source in sources)
    ):
        return "generator-index-letter", symbol
    if (
        homes
        and all(h.startswith("group__") for h in homes)
        and FILE_PAGE_RE.search(page)
        and surfaces <= {"page-nav", "crawl"}
    ):
        return "generator-nav-entry-at-file-page", symbol
    if comp is not None and comp["kind"] in CLASS_KINDS and ent is not None:
        return "member-anchor-absent-from-class-page", symbol
    return "anchor-never-generated", symbol


def broken_link_findings(
    tree: HtmlTree, model: Model
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """The broken links to fail on, one row per target, and the generator's count."""
    by_target: dict[str, dict[str, Any]] = {}
    for link in scan_html_links(tree):
        slot = by_target.setdefault(
            link["to"], {"missing": link["missing"], "from": set(), "surfaces": set(), "links": 0}
        )
        slot["from"].add(link["from"])
        slot["surfaces"].add(link["surface"])
        slot["links"] += 1
    rows: list[dict[str, Any]] = []
    generator: dict[str, int] = collections.Counter()
    for target, slot in sorted(by_target.items()):
        cause, symbol = cause_of(
            target, slot["missing"], slot["from"], slot["surfaces"], model, tree
        )
        if cause in GENERATOR_CAUSES:
            generator[cause] += slot["links"]
            continue
        rows.append(
            {
                "to": target,
                "cause": cause,
                "symbol": symbol,
                "links": slot["links"],
                "from": sorted(slot["from"])[:3],
            }
        )
    return rows, dict(generator)


def pageless_class_findings(model: Model, tree: HtmlTree) -> list[dict[str, Any]]:
    """Classes with documented public members and no documentation of their own."""
    out = []
    for cid, comp in sorted(model.compounds.items()):
        if comp["kind"] not in CLASS_KINDS or comp["documented"]:
            continue
        docs = [
            m
            for m in comp["members"]
            if model.members[m]["documented"] and model.members[m]["prot"] == "public"
        ]
        if docs and (cid + ".html") not in tree.files:
            out.append(
                {
                    "name": comp["name"],
                    "file": comp["file"],
                    "line": comp["line"],
                    "documented_public_members": len(docs),
                }
            )
    return out


def group_findings(scan: dict[str, Any], model: Model) -> dict[str, list[Any]]:
    defs, adds, ingroups = scan["defgroup"], scan["addtogroup"], scan["ingroup"]
    out: dict[str, list[Any]] = {}
    out["group-id-defined-twice"] = [
        {"id": gid, "where": rows} for gid, rows in sorted(defs.items()) if len(rows) > 1
    ]
    out["group-defined-outside-groups-dox"] = [
        {"id": gid, "where": [r for r in rows if r["file"] != GROUPS_DOX]}
        for gid, rows in sorted(defs.items())
        if any(r["file"] != GROUPS_DOX for r in rows)
    ]
    out["group-addtogroup-with-title"] = [
        {"id": gid, "where": [r for r in rows if r["title"]]}
        for gid, rows in sorted(adds.items())
        if any(r["title"] for r in rows)
    ]
    used = set(adds) | set(ingroups)
    out["group-joined-but-never-defined"] = [
        {"id": gid, "where": (adds.get(gid, []) + ingroups.get(gid, []))[:4]}
        for gid in sorted(used - set(defs))
    ]
    out["group-defined-but-never-joined"] = [
        {"id": gid, "where": defs[gid]} for gid in sorted(set(defs) - used)
    ]
    titles: dict[str, list[str]] = collections.defaultdict(list)
    for comp in model.compounds.values():
        if comp["kind"] == "group":
            titles[comp["title"].strip().lower()].append(comp["name"])
    out["group-title-shared"] = [
        {"title": title, "ids": sorted(ids)}
        for title, ids in sorted(titles.items())
        if len(ids) > 1
    ]
    return out


def section_findings(model: Model) -> list[dict[str, Any]]:
    """A member-group (``@name``) title a page shows more than once."""
    per: dict[tuple[str, str], list[dict[str, Any]]] = collections.defaultdict(list)
    for sect in model.sections:
        per[(sect["compound"], sect["header"].strip())].append(sect)
    out = []
    for (cid, header), rows in sorted(per.items()):
        if len(rows) > 1:
            comp = model.compounds[cid]
            out.append(
                {
                    "page": comp["title"] or comp["name"],
                    "title": header,
                    "where": [f"{r['file']}:{r['line']}" for r in rows],
                }
            )
    return out


def using_copy_findings(
    model: Model, scan: dict[str, Any]
) -> tuple[list[dict[str, Any]], set[str]]:
    """What a using-declaration made Doxygen document a second time.

    ``using a::Thing;`` in another scope gives ``Thing`` a second compound, with
    a page of its own, under that scope; ``using a::fn;`` lists ``fn`` again as a
    member of it. Returns the findings and the ids of the copies, which the
    name checks leave out: a copy is the original under another name.
    """

    def line_at(file: str, line: int) -> str:
        rows = scan["lines"].get(file, [])
        return rows[line - 1] if 0 < line <= len(rows) else ""

    by_decl: dict[tuple[Any, ...], list[dict[str, Any]]] = collections.defaultdict(list)
    for comp in model.compounds.values():
        if comp["kind"] in CLASS_KINDS and comp["file"]:
            by_decl[(comp["file"], comp["line"], comp["name"].rpartition("::")[2])].append(comp)
    out: list[dict[str, Any]] = []
    copies: set[str] = set()
    for (file, line, _last), comps in sorted(by_decl.items()):
        if len(comps) < 2:
            continue
        original = next(
            (c for c in comps if file in model.inner_class_of_file.get(c["id"], ())),
            min(comps, key=lambda c: len(c["name"])),
        )
        for comp in comps:
            if comp is not original:
                copies.add(comp["id"])
        out.append(
            {
                "original": original["name"],
                "declared": f"{file}:{line}",
                "copies": sorted(c["name"] for c in comps if c is not original),
            }
        )
    for mem in model.members.values():
        text = line_at(mem["file"], mem["line"])
        # an indented one is class scope (an inherited constructor): C++, not a copy
        if USING_DECL_RE.match(text) and not text[:1].isspace():
            copies.add(mem["id"])
            out.append(
                {
                    "original": mem["qualified"],
                    "declared": f"{mem['file']}:{mem['line']}",
                    "copies": ["listed again by this using-declaration"],
                }
            )
    return out, copies


#: What a bare name can shadow: types and constants. Not functions.
NAMED_KINDS = ("class", "struct", "union", "enum", "typedef", "variable")


def namespace_scope_names(model: Model, copies: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Every documented namespace-scope type and constant, by bare name."""
    class_names = {c["name"] for c in model.compounds.values() if c["kind"] in CLASS_KINDS}
    seen: dict[str, dict[str, dict[str, Any]]] = collections.defaultdict(dict)
    for cid, comp in model.compounds.items():
        if (
            comp["kind"] in CLASS_KINDS
            and comp["documented"]
            and cid not in copies
            and scope_of(comp["name"]) not in class_names
        ):
            seen[comp["name"].rpartition("::")[2]].setdefault(
                comp["name"],
                {
                    "qualified": comp["name"],
                    "kind": comp["kind"],
                    "where": f"{comp['file']}:{comp['line']}",
                    "stands_for": "",
                },
            )
    for mid, mem in model.members.items():
        if (
            not mem["documented"]
            or mid in copies
            or scope_of(mem["qualified"]) in class_names
            or mem["kind"] not in NAMED_KINDS
        ):
            continue
        seen[mem["name"]].setdefault(
            mem["qualified"],
            {
                "qualified": mem["qualified"],
                "kind": mem["kind"],
                "where": f"{mem['file']}:{mem['line']}",
                "stands_for": mem["stands_for"],
            },
        )
    return {name: list(slots.values()) for name, slots in seen.items() if len(slots) > 1}


def names_other(inner: dict[str, Any], outer: dict[str, Any]) -> bool:
    """Whether one of the two is declared as the other (an alias, ``= outer``)."""
    for alias, target in ((inner, outer), (outer, inner)):
        text = alias["stands_for"]
        parts = target["qualified"].split("::")
        for start in range(len(parts)):
            tail = "::".join(parts[start:])
            if "::" in tail and re.search(
                r"(?<![\w:])(?:::)?" + re.escape(tail) + r"(?![\w:])", text
            ):
                return True
    return False


def repeated_name_findings(model: Model, copies: set[str]) -> tuple[list[dict[str, Any]], int]:
    """Names an enclosing namespace also declares, and the count of sibling repeats.

    ``a::Thing`` and ``a::b::Thing`` are two things of one name where the bare
    word means either, by where it is written: a finding. ``a::Thing`` and
    ``b::Thing`` are the idiom of a short word under a namespace that says which
    one: counted only. A class member and a function are never compared.
    """
    shadowed: list[dict[str, Any]] = []
    siblings = 0
    for name, slots in sorted(namespace_scope_names(model, copies).items()):
        pairs = []
        for outer in slots:
            for inner in slots:
                o, i = scope_of(outer["qualified"]), scope_of(inner["qualified"])
                if o != i and (o == "" or i.startswith(o + "::")) and not names_other(inner, outer):
                    pairs.append((outer, inner))
        if pairs:
            for outer, inner in pairs:
                shadowed.append(
                    {
                        "name": name,
                        "outer": outer["qualified"],
                        "outer_at": outer["where"],
                        "inner": inner["qualified"],
                        "inner_at": inner["where"],
                    }
                )
        else:
            siblings += 1
    return shadowed, siblings


WARNING_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+): (?:error|warning): (?P<text>.*)$")
#: An XML-only run has no dot; the graphs' absence is this run's choice.
IGNORED_WARNINGS = ("ignoring \\dot command because HAVE_DOT is not set",)


def warning_findings(lines: list[str], root: Path) -> list[dict[str, Any]]:
    """Warnings the build printed, with paths made relative to the tree."""
    out = []
    for line in lines:
        m = WARNING_RE.match(line)
        if not m or any(skip in m.group("text") for skip in IGNORED_WARNINGS):
            continue
        file = m.group("file")
        with contextlib.suppress(ValueError):
            file = Path(file).resolve().relative_to(root.resolve()).as_posix()
        out.append({"file": file, "line": int(m.group("line")), "text": m.group("text")})
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

#: Every class this gate fails on, in the order it reports them. The self-test
#: holds a plant for each (``tests/firmware/test_doxygen_gate.py`` checks
#: the two lists agree), so a class added here without one fails there.
FINDING_CLASSES = (
    "broken-link",
    "pageless-class",
    "glued-colon",
    "group-id-defined-twice",
    "group-defined-outside-groups-dox",
    "group-addtogroup-with-title",
    "group-joined-but-never-defined",
    "group-defined-but-never-joined",
    "group-title-shared",
    "member-group-title-twice",
    "using-copy",
    "shadowed-name",
    "file-basename",
    "xml-warning",
)


def check(root: Path, html: Path | None = None) -> dict[str, Any]:
    """Build what is missing, read the build, and return every finding.

    :param root: Tree holding ``Doxyfile``, ``firmware/src/`` and ``docs/``.
    :param html: A finished HTML build of that tree, or ``None`` to build one.
    :return: ``{"findings": {class: [rows]}, "counted": {name: number}}``.
    :raises DoxygenFailed: Doxygen is missing or did not finish.
    """
    with tempfile.TemporaryDirectory(prefix="meter-doc-links-") as tmp:
        out = Path(tmp)
        warnings = run_doxygen(root, out, with_html=html is None)
        tree = HtmlTree(html if html is not None else out / "html", document_images(root))
        model = Model(out / "xml")
        scan = scan_sources(root)
        broken, generator = broken_link_findings(tree, model)
        using, copies = using_copy_findings(model, scan)
        shadowed, siblings = repeated_name_findings(model, copies)
        findings: dict[str, list[Any]] = {
            "broken-link": broken,
            "pageless-class": pageless_class_findings(model, tree),
            "glued-colon": scan["glued"],
        }
        findings.update(group_findings(scan, model))
        findings.update(
            {
                "member-group-title-twice": section_findings(model),
                "using-copy": using,
                "shadowed-name": shadowed,
                "file-basename": [
                    {"name": k, "files": v} for k, v in sorted(scan["basenames"].items())
                ],
                "xml-warning": warning_findings(warnings, root),
            }
        )
        if tuple(findings) != FINDING_CLASSES:
            raise AssertionError(f"finding classes drifted: {tuple(findings)}")
        counted = {
            "pages": len(tree.pages),
            "compounds": len(model.compounds),
            "members": len(model.members),
            "names repeated in sibling namespaces (the idiom)": siblings,
        }
        counted.update(
            {f"links broken by Doxygen itself ({k})": v for k, v in sorted(generator.items())}
        )
        if tree.stale:
            counted["pages an earlier build left in the HTML directory (not read)"] = len(
                tree.stale
            )
    return {"findings": findings, "counted": counted}


def describe(kind: str, row: dict[str, Any]) -> str:
    """One line a person can act on."""
    if kind == "broken-link":
        frm = ", ".join(row["from"])
        symbol = row["symbol"] or "?"
        return f"{row['to']} ({row['cause']}; {symbol}; {row['links']} links, from {frm})"
    if kind == "pageless-class":
        return f"{row['name']} at {row['file']}:{row['line']} has no comment of its own"
    if kind == "glued-colon":
        return f"{row['file']}:{row['line']}: @ref {row['name']}: (end the name before the colon)"
    if kind.startswith("group-title"):
        return f'"{row["title"]}" titles {", ".join(row["ids"])}'
    if kind.startswith("group-"):
        where = ", ".join(f"{w['file']}:{w['line']}" for w in row["where"])
        return f"{row['id']} ({where})"
    if kind == "member-group-title-twice":
        return f'"{row["title"]}" on the page of {row["page"]}: {", ".join(row["where"])}'
    if kind == "using-copy":
        return f"{row['original']} ({row['declared']}): {', '.join(row['copies'])}"
    if kind == "shadowed-name":
        return f"{row['inner']} ({row['inner_at']}) under {row['outer']} ({row['outer_at']})"
    if kind == "file-basename":
        return f"{row['name']}: {', '.join(row['files'])}"
    return f"{row['file']}:{row['line']}: {row['text']}"


def report(result: dict[str, Any], limit: int = 12) -> int:
    """Print the result; return the number of findings."""
    total = 0
    for kind, rows in result["findings"].items():
        if not rows:
            continue
        total += len(rows)
        print(f"\n[{kind}] {len(rows)}")
        for row in rows[:limit]:
            print("  " + describe(kind, row))
        if len(rows) > limit:
            print(f"  … and {len(rows) - limit} more (--json FILE lists them all)")
    print()
    for name, value in result["counted"].items():
        print(f"  {name}: {value}")
    return total


# ---------------------------------------------------------------------------
# Self-test: plant each defect in a temporary tree and see it caught
# ---------------------------------------------------------------------------

#: A small tree with every construct the checks read and nothing wrong in it:
#: a grouped enum (whose values raise the generator's own broken links), a
#: free function defined in a .cpp, two member groups, an alias of an outer
#: name. Its sources sit flat in ``firmware/src``, as the Doxyfile reads them.
FIXTURE = {
    "docs/groups.dox": """\
/** @file groups.dox
 *  @brief Topics. */
/** @defgroup core Core
 *  @brief The core. */
/** @defgroup extra Extra
 *  @brief The extras. */
""",
    "docs/firmware.dox": "/** @mainpage Probe\n *  A probe tree. See @ref core. */\n",
    "firmware/src/thing.h": """\
/// @file thing.h
/// @brief Things.
/// @ingroup core
#pragma once
namespace lib {

/// @addtogroup core
/// @{

/// @brief A thing.
struct Thing {
    int x;  ///< X.
};

/// @brief A mode.
enum class Mode {
    Fast,  ///< Fast.
    Slow   ///< Slow.
};

/// @brief Handle of a thing.
using ThingId = int;

/// @name Running
/// @{

/// @brief Run a thing.
/// @param[in] thing The thing.
void run( const Thing& thing );

/// @}

/// @name Stopping
/// @{

/// @brief Stop a thing. See @ref run.
/// @param[in] thing The thing.
void stop( const Thing& thing );

/// @}

/// @}

}  // namespace lib
""",
    "firmware/src/thing.cpp": """\
#include "thing.h"
namespace lib {
void run( const Thing& ) {}
void stop( const Thing& thing ) { run( thing ); }
}  // namespace lib
""",
    "firmware/src/user.h": """\
/// @file user.h
/// @brief A user of things.
/// @ingroup extra
#pragma once
#include "thing.h"
namespace lib::app {

/// @addtogroup extra
/// @{

/// @brief The same handle under this namespace's name.
using ThingId = lib::ThingId;

/// @brief Holds a thing.
class Holder {
   public:
    /// @brief Hold one.
    /// @param[in] thing The thing to hold.
    void hold( const lib::Thing& thing );

   private:
    int held_ = 0;  ///< Things held.
};

/// @}

}  // namespace lib::app
""",
}

#: Each plant: the class it must raise, and the files it rewrites or adds. A
#: value is the new text of the file, or a (old, new) pair replaced in it.
PLANTS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "broken-link",
        "a function whose home is a .cpp with no page (the wrap opened outside"
        " the namespace groups only the namespace)",
        {
            "firmware/src/loose.h": "/// @file loose.h\n/// @brief Loose functions.\n"
            "#pragma once\n\n"
            "/// @addtogroup extra\n/// @{\n\nnamespace loose {\n\n"
            "/// @brief A loose function.\n/// @param[in] steps How far.\n"
            "void wander( int steps );\n\n}\n\n/// @}\n",
            "firmware/src/loose.cpp": '#include "loose.h"\nnamespace loose {\n'
            "void wander( int ) {}\n}\n",
        },
    ),
    (
        "broken-link",
        "a documented field of an unnamed struct in an unnamed union",
        {
            "firmware/src/cmd.h": "/// @file cmd.h\n/// @brief A command.\n#pragma once\n"
            "namespace cmd {\n"
            "/// @brief A command.\nstruct Cmd {\n    int kind;  ///< Kind.\n"
            "    union {\n        struct {\n            int mode;  ///< Mode.\n"
            "        } start;\n    } u;\n};\n}\n",
        },
    ),
    # This Doxyfile shows an undocumented class and warns on it, so the class
    # is raised only when a Doxyfile hides undocumented classes again.
    (
        "pageless-class",
        "a class with documented members and no comment, under a Doxyfile"
        " that hides undocumented classes",
        {
            "Doxyfile": ("HIDE_UNDOC_CLASSES     = NO", "HIDE_UNDOC_CLASSES     = YES"),
            "firmware/src/bare.h": "/// @file bare.h\n/// @brief A bare class.\n#pragma once\n"
            "namespace bare {\n"
            "class Bare {\n   public:\n    /// @brief Do it.\n    void act();\n};\n}\n",
        },
    ),
    (
        "glued-colon",
        "a colon glued to a @ref name",
        {
            "firmware/src/thing.h": (
                "/// @brief Stop a thing. See @ref run.",
                "/// @brief Stop a thing. Unlike @ref run: it ends.",
            ),
        },
    ),
    (
        "group-id-defined-twice",
        "a second @defgroup of one id",
        {
            "docs/groups.dox": (
                "/** @defgroup extra Extra",
                "/** @defgroup core Again\n *  @brief Twice. */\n/** @defgroup extra Extra",
            ),
        },
    ),
    (
        "group-defined-outside-groups-dox",
        "a @defgroup in a header",
        {
            "firmware/src/user.h": (
                "/// @addtogroup extra\n",
                "/// @defgroup extra2 Extra two\n/// @brief Two.\n/// @addtogroup extra\n",
            ),
        },
    ),
    (
        "group-joined-but-never-defined",
        "an @addtogroup of an id nothing defines",
        {
            "firmware/src/user.h": ("/// @addtogroup extra\n", "/// @addtogroup extras\n"),
        },
    ),
    (
        "group-defined-but-never-joined",
        "a @defgroup nothing joins",
        {
            "docs/groups.dox": (
                "/** @defgroup extra Extra",
                "/** @defgroup lonely Lonely\n *  @brief Alone. */\n/** @defgroup extra Extra",
            ),
        },
    ),
    (
        "group-addtogroup-with-title",
        "an @addtogroup that carries a title",
        {
            "firmware/src/user.h": ("/// @addtogroup extra\n", "/// @addtogroup extra Extras\n"),
        },
    ),
    (
        "group-title-shared",
        "two ids under one title",
        {
            "docs/groups.dox": ("/** @defgroup extra Extra", "/** @defgroup extra Core"),
        },
    ),
    (
        "member-group-title-twice",
        "one @name title in two namespaces of one topic",
        {
            "firmware/src/other.h": "/// @file other.h\n/// @brief Other things.\n"
            "/// @ingroup core\n#pragma once\n"
            "namespace lib::more {\n\n/// @addtogroup core\n/// @{\n\n/// @name Running\n"
            "/// @{\n\n/// @brief Run the other thing.\ninline void runOther() {}\n\n"
            "/// @}\n\n/// @}\n\n}  // namespace lib::more\n",
        },
    ),
    (
        "using-copy",
        "a using-declaration of a class at namespace scope",
        {
            "firmware/src/user.h": (
                "namespace lib::app {\n",
                "namespace lib::app {\nusing lib::Thing;\n",
            ),
        },
    ),
    (
        "shadowed-name",
        "a type an enclosing namespace also declares",
        {
            "firmware/src/user.h": (
                "/// @brief The same handle under this namespace's name.\n"
                "using ThingId = lib::ThingId;",
                "/// @brief Another handle of the same name.\nusing ThingId = long;",
            ),
        },
    ),
    (
        "file-basename",
        "two files of one name",
        {
            "firmware/src/extra/thing.h": "/// @file extra/thing.h\n/// @brief Another thing.\n"
            "#pragma once\n",
        },
    ),
    (
        "xml-warning",
        "a broken @ref in a private member's comment",
        {
            "firmware/src/user.h": (
                "int held_ = 0;  ///< Things held.",
                "int held_ = 0;  ///< Things held, as @ref counted_ says.",
            ),
        },
    ),
]


def write_fixture(root: Path, changes: dict[str, Any]) -> None:
    """Write the clean fixture into ``root`` and apply one plant's changes."""
    files = dict(FIXTURE)
    files["Doxyfile"] = read_text(REPO_ROOT / "Doxyfile")
    for rel, change in changes.items():
        if isinstance(change, tuple):
            old, new = change
            if files[rel].count(old) != 1:
                raise AssertionError(f"plant does not apply once to {rel}: {old!r}")
            files[rel] = files[rel].replace(old, new)
        else:
            files[rel] = change
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def selftest() -> int:
    """Plant each defect in a temporary tree and prove this gate reports it.

    A gate that reads a build and finds nothing reads exactly like a gate that
    cannot see. So the clean tree must pass, with the generator's own broken
    links counted and not failed; it must pass again over an output directory
    an earlier build left a page in; and every plant must raise its class.
    """
    if shutil.which("doxygen") is None:
        print("[FAIL] doxygen is not installed, so the self-test measured nothing.")
        return 1
    failures = 0
    with tempfile.TemporaryDirectory(prefix="meter-doc-links-selftest-") as tmp:
        clean = Path(tmp) / "clean"
        write_fixture(clean, {})
        result = check(clean)
        raised = sorted(k for k, rows in result["findings"].items() if rows)
        skipped = sum(v for k, v in result["counted"].items() if k.startswith("links broken by"))
        unexercised = [
            cause
            for cause in GENERATOR_CAUSES
            if not result["counted"].get(f"links broken by Doxygen itself ({cause})")
        ]
        if raised:
            failures += 1
            print(f"[FAIL] the clean tree raises {', '.join(raised)}")
            report(result)
        elif unexercised:
            failures += 1
            print(
                f"[FAIL] the clean tree has none of the generator's own broken links of"
                f" {', '.join(unexercised)}: the rule that skips them was not exercised"
            )
        else:
            print(f"  ok  clean tree: no finding, {skipped} generator links counted and not failed")
        # Doxygen never deletes what an earlier run wrote. A page of a symbol
        # that no longer exists, with its nav data, must be neither read nor a
        # valid target, or a developer's second build fails on the first one.
        reused = Path(tmp) / "reused"
        reused.mkdir()
        run_doxygen(clean, reused, with_html=True)
        dead_link = "never__built_8cpp.html#a0123456789abcdef"
        (reused / "html" / "structlib_1_1_gone.html").write_text(
            f'<html><body><a class="el" href="{dead_link}">gone</a></body></html>\n',
            encoding="utf-8",
        )
        (reused / "html" / "structlib_1_1_gone.js").write_text(
            f'var structlib_1_1_gone = [[ "x", "{dead_link}", null ]];\n', encoding="utf-8"
        )
        result = check(clean, reused / "html")
        raised = sorted(k for k, rows in result["findings"].items() if rows)
        left = sum(
            v for k, v in result["counted"].items() if k.startswith("pages an earlier build")
        )
        if raised or left != 1:
            failures += 1
            print(
                "[FAIL] a page an earlier build left behind: raised"
                f" {', '.join(raised) or 'nothing'},"
                f" {left} counted as left behind (expected no finding and 1)"
            )
        else:
            print("  ok  reused output directory: the page an earlier build left is not read")
        # The empty-key rule is as narrow as its defect: a key of the sidebar's
        # index that does carry a tree path is a link, and its missing page fails.
        nav = sorted((reused / "html").glob("navtreeindex*.js"))[0]
        text = read_text(nav)
        nav.write_text(text.replace("{\n", '{\n"ghost__page.html":[0,1],\n', 1), encoding="utf-8")
        result = check(clean, reused / "html")
        ghost = [
            row for row in result["findings"]["broken-link"] if row["to"] == "ghost__page.html"
        ]
        if not ghost:
            failures += 1
            print("[FAIL] a sidebar index key with a tree path to a missing page is not raised")
        else:
            print("  ok  broken-link: a sidebar index key with a path to a missing page")
        for number, (kind, what, changes) in enumerate(PLANTS, 1):
            tree = Path(tmp) / f"plant{number}"
            write_fixture(tree, changes)
            result = check(tree)
            if result["findings"].get(kind):
                print(f"  ok  {kind}: {what}")
            else:
                failures += 1
                raised = sorted(k for k, rows in result["findings"].items() if rows)
                print(
                    f"[FAIL] {kind} not raised by {what} (raised: {', '.join(raised) or 'nothing'})"
                )
    if failures:
        print(f"\n[FAIL] {failures} of {len(PLANTS) + 3} self-test cases failed.")
        return 1
    print(
        f"\n[PASS] the clean tree passes, a reused output directory passes, and each of"
        f" {len(PLANTS) + 1} planted defects is reported."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--html",
        help="a finished HTML build to check, e.g. build/doxygen/html (default: build one)",
    )
    parser.add_argument("--json", help="write every finding to this file")
    parser.add_argument(
        "--report",
        action="store_true",
        help="list every row of every finding class, not the first dozen",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="plant each defect in a temporary tree and see it caught",
    )
    args = parser.parse_args()
    if args.selftest:
        return selftest()

    html = Path(args.html).resolve() if args.html else None
    if html is not None and not (html / "index.html").is_file():
        build = "mkdir -p build && doxygen Doxyfile"
        print(f"[FAIL] {html} holds no HTML build (run `{build}` first).")
        return 1
    print("Reading the API reference…")
    try:
        result = check(REPO_ROOT, html)
    except DoxygenFailed as exc:
        print(f"\n[FAIL] {exc}\n       This gate measured nothing.")
        return 1
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    total = report(result, limit=10**9 if args.report else 12)
    if total:
        print(
            f"\n[FAIL] {total} findings in the API reference. The module docstring of this"
            " script says what each class means and how it is fixed."
        )
        return 1
    print(
        "\n[PASS] every internal link has its page and anchor, no topic or section is named"
        " twice, and no name shadows an outer one."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
