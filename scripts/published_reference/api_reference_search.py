#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the published reference's search index, from the pages it publishes.

The search of ``api.kleidos.dev`` is the host's own (``site/api/assets/search.js``,
no third-party code): a script in the reader's browser ranks two lists the
build writes beside the pages, and nothing is asked of a server. The lists are
made from the finished pages (``scripts/build_api_reference.py`` runs this
after the links are written clean and before the guard), so they hold the
words the pages hold and no other, and the guard reads them as it reads a
page (``scripts/check_api_reference.py``: :func:`problems`, the private-value
scan, the withheld passages):

- ``search-pages.json``: every page but the indexes by letter, as
  ``[name, kind, address, description, scope]``: its title (a compound's bare
  name), what it is (``topic``, ``class``, ``struct``, ``union``,
  ``namespace``, ``file``, ``page``), its clean address, its own
  ``<meta name="description">`` and the scope its opening names (a namespace
  joined by ``::``, a file's folders by ``/``).
- ``search-members.json``: every member a page documents under an anchor of its
  own, as ``[name, page, anchor, kind, brief, parameters]``: the name its line
  of the page says, the page's place in the first list, the anchor, what it is
  (``function``, ``variable``, ``enum``, ``type``, ``macro``), the first
  sentence of its brief description, cut to :data:`BRIEF_CHARS`, and, for an
  overload, what its line says after the name (``(int32_t y) const``), which
  the search shows after it; empty for a name the page holds once.

No two entries of a list may read alike to a reader (the name, the kind, where
it is, its words and, for a member, its parameters): :func:`problems` finds
them.

The script fetches the first when the search opens and the second right after
it, so page names answer while members load. Each has a budget, gzipped as the
edge serves it (:data:`BUDGETS`), which the guard holds.
"""

from __future__ import annotations

import gzip
import html
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import api_reference_links
import api_reference_pages

#: The two files, beside the pages of the reference.
PAGES_FILE = "search-pages.json"
MEMBERS_FILE = "search-members.json"
FILES = (PAGES_FILE, MEMBERS_FILE)

#: How a page loads the search: the host's one script of its own, deferred, from
#: the host's assets beside the reference (``docs/published-reference/header.html``).
SCRIPT = '<script src="../assets/search.js" defer></script>'

#: The most bytes each may take gzipped (level 9), as the edge compresses it.
BUDGETS = {PAGES_FILE: 80 * 1024, MEMBERS_FILE: 300 * 1024}

#: How long a member's brief may be, in characters.
BRIEF_CHARS = 100

#: The pages left out: the indexes by letter and by kind, which list names
#: every other entry already is.
_SKIPPED_RE = re.compile(r"(?:functions|namespacemembers|globals)(?:_[^.]+)?\.html")

_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r'<h1 class="title">(.*?)</h1>', re.S)
_HEAD_TITLE_RE = re.compile(r"<title>([^<]*)</title>")
_DESCRIPTION_RE = re.compile(r'<meta name="description" content="([^"]*)"')
_KIND_RE = re.compile(r'<span class="eyebrow-kind">(.*?)</span>', re.S)
_SCOPE_RE = re.compile(r'<(span|ul) class="eyebrow-scope">(.*?)</\1>', re.S)
_STEP_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S)
_LABELS_RE = re.compile(r'<span class="mlabels">.*?</span></span>', re.S)
_TABLE_RE = re.compile(r'<table class="memberdecls"[^>]*>(.*?)</table>', re.S)
_HEADER_RE = re.compile(r'<h2 id="header-([a-z-]+)"')
_ROW_RE = re.compile(
    r'<tr class="memitem:[^"]*"[^>]*>(.*?)</tr>\s*(?:<tr class="memdesc:[^"]*"[^>]*>(.*?)</tr>)?',
    re.S,
)
_LEFT_RE = re.compile(r'<td class="memItemLeft"[^>]*>(.*?)</td>', re.S)
_RIGHT_RE = re.compile(r'<td class="memItemRight"[^>]*>(.*?)</td>', re.S)
_LOCAL_LINK_RE = re.compile(r'<a class="el" href="#([^"]+)">(.*?)</a>(.*)', re.S)
_DESC_RE = re.compile(r'<td class="mdescRight"[^>]*>(.*?)</td>', re.S)
_MORE_RE = re.compile(r"<a [^>]*>More<.*?</a>", re.S)
#: A stop that may end a sentence: one before a space or the end.
_STOP_RE = re.compile(r"[.!?](?=\s|$)")

#: The lists of a page whose entries are pages of their own, not members.
_NOT_MEMBERS = frozenset({"namespaces", "nested-classes", "files", "groups", "friends"})


def text(markup: str) -> str:
    """Return what *markup* says: its words, its spaces as one."""
    return " ".join(html.unescape(_TAG_RE.sub("", markup.replace("<wbr/>", ""))).split())


def page_kind(name: str, page: str) -> str:
    """Return what the page *name* is: the word of its opening, a topic or a page."""
    said = _KIND_RE.search(page)
    if said:
        return text(said.group(1)).lower()
    return "topic" if name.startswith("group__") else "page"


def scope_of(page: str) -> str:
    """Return the scope a page's opening names: a namespace's steps joined by ``::``."""
    scope = _SCOPE_RE.search(page)
    if scope is None:
        return ""
    steps = [text(each) for each in _STEP_RE.findall(scope.group(2))]
    return "::".join(steps) if steps else text(scope.group(2))


def page_entry(name: str, page: str) -> list[str]:
    """Return the entry of the page *name*: name, kind, address, description, scope."""
    title = _TITLE_RE.search(page)
    said = text(_LABELS_RE.sub("", title.group(1))) if title else ""
    if not said:
        head = _HEAD_TITLE_RE.search(page)
        said = text(head.group(1)).split(" · ")[0] if head else name
    described = _DESCRIPTION_RE.search(page)
    return [
        said,
        page_kind(name, page),
        api_reference_links.served(name) or "./",
        html.unescape(described.group(1)) if described else "",
        scope_of(page),
    ]


def member_kind(left: str, right: str) -> str:
    """Return what a member is, from the two cells of its line."""
    if left.startswith("#define"):
        return "macro"
    if left.startswith("enum"):
        return "enum"
    if left.startswith(("using", "typedef")):
        return "type"
    if right.lstrip().startswith("("):
        return "function"
    return "variable"


def brief(markup: str) -> str:
    """Return the first sentence of a brief description, cut to :data:`BRIEF_CHARS`.

    A sentence ends where :func:`api_reference_pages.ends_sentence` says, so an
    abbreviation (``(e.g. Load the RAM)``) or an aside in brackets does not
    cut it.
    """
    said = text(_MORE_RE.sub("", markup))
    for stop in _STOP_RE.finditer(said):
        if api_reference_pages.ends_sentence(said, stop.end()):
            said = said[: stop.end()]
            break
    if len(said) > BRIEF_CHARS:
        said = said[: BRIEF_CHARS - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return said


def member_entries(page: str, at: int) -> list[list[str | int]]:
    """Return the members the page documents under an anchor of its own.

    :param page: The page.
    :param at: Its place in the list of pages.
    """
    found: list[list[str | int]] = []
    seen: set[str] = set()
    for table in _TABLE_RE.finditer(page):
        header = _HEADER_RE.search(table.group(1))
        if header is None or header.group(1) in _NOT_MEMBERS:
            continue
        for row, described in _ROW_RE.findall(table.group(1)):
            right = _RIGHT_RE.search(row)
            left = _LEFT_RE.search(row)
            linked = _LOCAL_LINK_RE.search(right.group(1)) if right else None
            if linked is None or linked.group(1) in seen:
                continue
            seen.add(linked.group(1))
            words = _DESC_RE.search(described or "")
            after = text(linked.group(3))
            found.append(
                [
                    text(linked.group(2)),
                    at,
                    linked.group(1),
                    member_kind(text(left.group(1)) if left else "", after),
                    brief(words.group(1)) if words else "",
                    after,
                ]
            )
    # An overload is told from the others of its name by its parameters (and
    # its ``const``), whole, since what tells two apart may be the last one; a
    # name the page holds once needs none.
    names = Counter(entry[0] for entry in found)
    for entry in found:
        if names[entry[0]] < 2:
            entry[5] = ""
    return found


def indexes(pages: Mapping[str, str]) -> dict[str, list[list[str | int]]]:
    """Return the two lists, by file name, from the finished pages by name."""
    listed: list[list[str | int]] = []
    members: list[list[str | int]] = []
    for name in sorted(pages):
        if _SKIPPED_RE.fullmatch(name):
            continue
        listed.append(list(page_entry(name, pages[name])))
        members += member_entries(pages[name], len(listed) - 1)
    return {PAGES_FILE: listed, MEMBERS_FILE: members}


def encoded(entries: Iterable[object]) -> bytes:
    """Return a list as the file holds it: compact JSON in UTF-8."""
    return json.dumps(list(entries), separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def write(out: Path, names: Iterable[str]) -> dict[str, int]:
    """Write the two files into *out* from its pages *names*; return their gzipped sizes."""
    pages = {name: (out / name).read_text(encoding="utf-8") for name in names}
    sizes: dict[str, int] = {}
    for file, entries in indexes(pages).items():
        data = encoded(entries)
        (out / file).write_bytes(data)
        sizes[file] = len(gzip.compress(data, 9))
    return sizes


def problems(
    tree: Path, pages: Iterable[str], anchors: Mapping[str, frozenset[str]]
) -> list[tuple[str, str]]:
    """Return what is wrong with the search index of *tree*, as ``(file, what)``.

    A file missing while the pages load the search, a file that is not the
    shape :func:`indexes` writes, over its budget, naming a page the tree
    does not hold or an anchor its page does not hold, or holding two entries
    a reader cannot tell apart (:func:`shown`).

    :param tree: The built reference.
    :param pages: Its pages.
    :param anchors: The ids of each page, by name.
    """
    found: list[tuple[str, str]] = []
    held = set(pages)
    listed: list[object] = []
    for file in FILES:
        path = tree / file
        if not path.is_file():
            found.append((file, "missing, and the pages load the search"))
            continue
        data = path.read_bytes()
        size = len(gzip.compress(data, 9))
        if size > BUDGETS[file]:
            found.append((file, f"{size:,} bytes gzipped, over its budget of {BUDGETS[file]:,}"))
        try:
            entries = json.loads(data)
        except ValueError as error:
            found.append((file, f"not JSON ({error})"))
            continue
        if not isinstance(entries, list):
            found.append((file, "not a list"))
            continue
        labels: Counter[tuple[str, ...]] = Counter()
        if file == PAGES_FILE:
            listed = entries
            for each in entries:
                if not (isinstance(each, list) and len(each) == 5 and all(isinstance(v, str) for v in each)):
                    found.append((file, f"an entry that is not [name, kind, address, description, scope]: {str(each)[:60]}"))
                    continue
                if api_reference_links.page_of(each[2]) not in held:
                    found.append((file, f"an entry of {each[2]}, which the tree does not hold"))
                labels[shown(each, listed)] += 1
            found += _alike(file, labels)
            continue
        for each in entries:
            ok = (
                isinstance(each, list)
                and len(each) == 6
                and isinstance(each[1], int)
                and all(isinstance(each[i], str) for i in (0, 2, 3, 4, 5))
            )
            if not ok:
                found.append((file, f"an entry that is not [name, page, anchor, kind, brief, parameters]: {str(each)[:60]}"))
                continue
            if not 0 <= each[1] < len(listed):
                found.append((file, f"{each[0]}: a page that the list of pages does not hold"))
                continue
            page = listed[each[1]]
            name = api_reference_links.page_of(page[2]) if isinstance(page, list) else ""
            if each[2] not in anchors.get(name, frozenset()):
                found.append((file, f"{each[0]}: an anchor {name} does not hold (#{each[2]})"))
            labels[shown(each, listed)] += 1
        found += _alike(file, labels)
    return found


def shown(entry: list[Any], pages: list[Any]) -> tuple[str, ...]:
    """Return what a reader sees of an entry in the search's results.

    A page: its name, its kind, its scope and its description. A member: its
    name, its parameters, its kind, where it is (its page's qualified name, or
    a topic's or a page's title, as ``search.js`` says it) and its brief.

    :param entry: An entry of either list.
    :param pages: The list of pages, which a member's place points into.
    """
    if len(entry) == 5 and isinstance(entry[1], str):
        return tuple(str(v) for v in (entry[0], entry[1], entry[4], entry[3]))
    owner = pages[entry[1]] if isinstance(pages[entry[1]], list) and len(pages[entry[1]]) == 5 else ["", "", "", "", ""]
    joint = "/" if owner[1] == "file" else "::"
    where = owner[0] if owner[1] in ("topic", "page") or not owner[4] else f"{owner[4]}{joint}{owner[0]}"
    return tuple(str(v) for v in (entry[0], entry[5], entry[3], where, entry[4]))


def _alike(file: str, labels: Counter[tuple[str, ...]]) -> list[tuple[str, str]]:
    """Return a finding for each label of *labels* that more than one entry wears."""
    return [
        (file, f"{count} entries a reader cannot tell apart: {' · '.join(v for v in label if v)}")
        for label, count in labels.items()
        if count > 1
    ]
