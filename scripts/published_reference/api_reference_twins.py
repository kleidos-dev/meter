#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Markdown twin of each page of the published reference, and its ``llms.txt``.

llmstxt.org asks for "a clean markdown version of those pages at the same URL
as the original page", with ``.md`` appended. The reference is served at
addresses without ``.html`` (``api_reference_links.py``), so the twin of
``/firmware/group__vault`` is ``/firmware/group__vault.md`` and the twin of
``/firmware/`` is ``/firmware/index.md``: a file ``group__vault.md`` beside
``group__vault.html``. Each page names its twin in its head
(``<link rel="alternate" type="text/markdown">``, written by
``api_reference_links.finish``).

**Converted from the published page, not from Doxygen's XML.** The XML holds
every source file, the line of every member, the initializers and the
compounds the reference leaves out, and none of the build's rewrites:
converting it would mean doing the curation a second time and guarding a
second copy of the words. The page has been curated and rewritten already,
and Doxygen's classes in it say what each part is (``memitem``, ``memproto``,
``params``, ``retval``, ``dl.section``, ``memberdecls``, ``fieldtable``,
``fragment``, ``directory``). This runs after the links are written clean and
before the guard, which reads the twins as it reads the pages
(``check_api_reference.py``, rule ``twin``): no private value, no withheld
passage, no word the page does not say, every heading, paragraph, parameter
and declaration of the page held, and every link to a page or anchor that is
there.

The converter is this module, with nothing but the standard library: the
kleidos.dev twins follow the same contract (a comment naming the page,
``Canonical:`` in plain text, links absolute, headings and paragraphs kept)
from their own code, and the site is to leave this repository.

What a twin holds, read from the page's ``<main>``:

- the note, which is the first thing the content says; what the page is
  and the scope it is in as one line (``Class · kleidos::ble``); the title as
  its one ``#`` heading (a class's kinds, ``abstract`` or ``final``, as code
  on the line below it); the topics it is in (``Topics: …``) or the folders
  above it as a line of links; then the page's text: headings at their own level,
  paragraphs, lists, tables as tables;
- a list of members (``memberdecls``) as a list, one line a member with its
  type, its name linked and its arguments, then its one line of description;
- a member as a heading with its whole declaration under it in a ``cpp``
  block, its qualifiers as code, then its text: parameters and return values
  as lists (with each parameter's direction), notes and warnings as labeled
  quotes, "See also" and the rest as labeled paragraphs; a label (a section's
  title, a parameter's name) shares its line with words only, and stands on a
  line of its own before an example, a list or a table, which a label on
  their line would turn into a paragraph's words; a list right after one of
  its kind takes the other marker (``+`` after ``-``, ``1)`` after ``1.``), or
  the two would be read as one;
- an example as a fenced block, line by line; a drawing as its caption.

What it leaves out is what is not the document: the page's contents box,
"Back to the top", a member's permalink, words only a screen reader is given
for a table's columns, and the drawing itself.

``llms.txt`` (:func:`llms_file`) lists every page of the reference by kind,
the front pages and the topics first, each with its own description (its
``<meta name="description">``, without a colon it may end with) and its twin; the host's own ``/llms.txt``
(``site/api/scripts/stage.mjs``) takes its first two sections.
"""

from __future__ import annotations

import html
import re
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import api_reference_links

#: The index llmstxt.org describes, in a reference's directory.
LLMS = "llms.txt"

#: How a twin's first line opens, and the line under it.
COMMENT = "<!-- The Markdown twin of {url}, converted from that page at build time."
CANONICAL_PREFIX = "Canonical: "

#: The elements HTML gives no end tag.
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)
#: The elements whose content becomes blocks of its own.
_BLOCKS = frozenset(
    {
        "address", "blockquote", "dd", "div", "dl", "dt", "figcaption", "figure", "h1", "h2",
        "h3", "h4", "h5", "h6", "li", "main", "nav", "ol", "p", "pre", "section", "table",
        "tbody", "td", "th", "thead", "tr", "ul",
    }
)  # fmt: skip
#: Inline elements, and the Markdown that wraps their text.
_WRAPS = {"b": "**", "strong": "**", "em": "*", "i": "*"}
#: The classes of what is not the document: the contents box, the way back to
#: the top, a member's permalink, a column's name given only to a screen reader.
_LEFT_OUT = frozenset({"summary", "to-top", "permalink", "visually-hidden"})
#: The sections a member's text gives, by Doxygen's class, that read as a
#: labeled quote: what a reader must not miss.
_QUOTED = frozenset(
    {"note", "warning", "attention", "remark", "pre", "post", "invariant", "deprecated", "bug",
     "todo"}
)  # fmt: skip
#: The pages a reader opens to look a name up, by the names of their files.
_INDEX_RE = re.compile(
    r"(annotated|functions|globals|namespacemembers|namespaces|files)(_.+)?\.html"
)
#: The kinds of page, by the names of their files, in the order ``llms.txt`` lists them.
KINDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Topics", re.compile(r"group__.+\.html")),
    ("Classes and records", re.compile(r"(class|struct|union|interface)[^_].*\.html")),
    ("Namespaces", re.compile(r"namespace[^m].*\.html")),
    ("Files", re.compile(r".+_8[a-z]+\.html")),
    ("Indexes", _INDEX_RE),
)
#: What opens a block of its own at the start of a line: a heading's marks, a
#: quote's, an item's (a dash, a plus) and an ordered item's number.
_LINE_START_RE = re.compile(r"^(#{1,6}(?=\s|$)|>|[-+=](?=\s|$))")
_NUMBER_START_RE = re.compile(r"^(\d+)([.)])(?=\s|$)")
#: What a block the converter writes opens with when it is no paragraph: a
#: fence, an item of a list, a row of a table, a heading or a quote. A label
#: written on the same line would turn it into the paragraph's words.
_OPENS_BLOCK_RE = re.compile(r"^(`{3,}|[-+*](?:\s|$)|\d+[.)](?:\s|$)|\||#|>)")
_SPACE_RE = re.compile(r"[ \t\n\r\f\u00a0]+")


@dataclass
class Node:
    """An element of a page, with its attributes and what it holds."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)

    @property
    def classes(self) -> list[str]:
        """Return the element's classes."""
        return self.attrs.get("class", "").split()

    def has(self, name: str) -> bool:
        """Say whether the element carries the class *name*."""
        return name in self.classes

    def elements(self) -> list[Node]:
        """Return the elements among what it holds."""
        return [child for child in self.children if isinstance(child, Node)]

    def find(self, tag: str, name: str | None = None) -> Node | None:
        """Return the first element below this one with *tag* (and class *name*)."""
        for child in self.elements():
            if child.tag == tag and (name is None or child.has(name)):
                return child
            found = child.find(tag, name)
            if found is not None:
                return found
        return None


class _Tree(HTMLParser):
    """Read a page into :class:`Node` elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self._open: list[Node] = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs})
        self._open[-1].children.append(node)
        if tag not in _VOID:
            self._open.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open[-1].children.append(Node(tag, {key: value or "" for key, value in attrs}))

    def handle_endtag(self, tag: str) -> None:
        for at in range(len(self._open) - 1, 0, -1):
            if self._open[at].tag == tag:
                del self._open[at:]
                return

    def handle_data(self, data: str) -> None:
        self._open[-1].children.append(data)


def parse(page: str) -> Node:
    """Return the elements of *page*."""
    tree = _Tree()
    tree.feed(page)
    tree.close()
    return tree.root


def text_of(node: Node | str) -> str:
    """Return every character of the text below *node*, as it is."""
    if isinstance(node, str):
        return node
    return "".join(text_of(child) for child in node.children)


def _left_out(node: Node) -> bool:
    """Say whether an element, and what it holds, stays out of the twin."""
    return node.tag in ("svg", "script", "style", "button") or any(
        name in _LEFT_OUT for name in node.classes
    )


def _unblocked(words: str) -> str:
    """Return a paragraph with what would open another block at its start taken as text."""
    return _NUMBER_START_RE.sub(r"\1\\\2", _LINE_START_RE.sub(r"\\\1", words))


def _labeled(head: str, blocks: Sequence[str], between: str) -> list[str]:
    """Return *blocks* under *head*: on the line of the first, when that is a paragraph.

    A label shares its line with words only. An example, a list, a table, a
    heading or a quote opens a line of its own: written after the label, the
    fence would open in the middle of a line (and never close), and the list
    would be the paragraph's words.
    """
    if not blocks:
        return [head] if head else []
    if not head:
        return list(blocks)
    first = blocks[0]
    if "\n" in first or _OPENS_BLOCK_RE.match(first):
        return [head, *blocks]
    return [f"{head}{between}{first}", *blocks[1:]]


#: The first item of a list a block opens with: its marker, by kind.
_LIST_MARK_RE = re.compile(r"^(?:([-+])|\d+([.)])) ")
#: The other marker of each kind, for a list right after one of its kind.
_OTHER_MARK = {"-": "+", "+": "-", ".": ")", ")": "."}


def _join(blocks: Sequence[str]) -> str:
    """Return *blocks* one after another, a blank line between each.

    A blank line does not end a list: CommonMark reads the next list of the
    same kind as more items of the first. A list right after one of its kind
    takes the other marker (``-`` and ``+``, ``1.`` and ``1)``), which starts a
    list of its own.
    """
    written: list[str] = []
    before = ""
    for block in blocks:
        mark = _LIST_MARK_RE.match(block)
        kind = (mark.group(1) or mark.group(2)) if mark else ""
        if kind and before and (kind in "-+") == (before in "-+") and kind == before:
            other = _OTHER_MARK[kind]
            if kind in "-+":
                block = re.sub(rf"^\{kind} ", f"{other} ", block, flags=re.M)
            else:
                block = re.sub(rf"^(\d+)\{kind} ", rf"\g<1>{other} ", block, flags=re.M)
            kind = other
        written.append(block)
        before = kind
    return "\n\n".join(written)


def _without(node: Node, name: str) -> Node:
    """Return a copy of *node* without the elements below it that carry the class *name*."""
    kept = Node(node.tag, node.attrs)
    for child in node.children:
        if isinstance(child, Node):
            if child.has(name):
                continue
            child = _without(child, name)
        kept.children.append(child)
    return kept


def squeeze(text: str) -> str:
    """Return *text* with each run of white space one space, and none at either end."""
    return _SPACE_RE.sub(" ", text).strip()


def escape(text: str) -> str:
    """Return prose *text* with what Markdown would read as markup taken as text."""
    text = text.replace("\\", "\\\\")
    for mark in ("`", "*", "[", "]"):
        text = text.replace(mark, "\\" + mark)
    # An underscore is left as it is: the names it joins (``s_prevKeys``) are
    # split by a run of text at a break (``<wbr/>``), and inside a word
    # Markdown reads it as a letter.
    return text.replace("<", "&lt;")


def code_span(text: str) -> str:
    """Return *text* as a code span, fenced with more backticks than it holds."""
    text = squeeze(text)
    if not text:
        return ""
    run = max((len(each) for each in re.findall(r"`+", text)), default=0)
    fence = "`" * (run + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _fence(text: str, language: str = "") -> str:
    """Return *text* as a fenced block."""
    run = max((len(each) for each in re.findall(r"`{3,}", text)), default=2)
    fence = "`" * (run + 1)
    return f"{fence}{language}\n{text}\n{fence}"


class Twin:
    """Write the twin of one page."""

    def __init__(self, name: str, reference: str = api_reference_links.REFERENCE) -> None:
        self.name = name
        self.url = api_reference_links.canonical(name, reference)

    def link(self, href: str) -> str:
        """Return *href* as an absolute address."""
        return urllib.parse.urljoin(self.url, href)

    # --- Inline -------------------------------------------------------------

    def inline(self, node: Node | str, code: bool = False) -> str:
        """Return the Markdown of what *node* says on a line.

        :param node: An element or a run of text.
        :param code: Whether its text is code: a run of it becomes a code
            span and a link's words a code span inside the link.
        """
        if isinstance(node, str):
            return code_span(node) if code else escape(node)
        if _left_out(node):
            return ""
        tag = node.tag
        if tag in ("br", "wbr"):
            return " " if tag == "br" else ""
        if tag == "img":
            alt = escape(squeeze(node.attrs.get("alt", "")))
            return f"![{alt}]({self.link(node.attrs.get('src', ''))})"
        if tag == "a" and node.attrs.get("href"):
            words = self.words(node, code)
            return f"[{words}]({self.link(node.attrs['href'])})" if words else ""
        if tag == "code" or node.has("tt"):
            return self.words(node, code=True)
        if node.has("mlabel"):
            return f" {code_span(text_of(node))} "
        if node.has("kind") and not code:
            inner = self.words(node)
            return f"({inner})" if inner else ""
        inner = self.words(node, code)
        if tag in _WRAPS and inner and not code:
            mark = _WRAPS[tag]
            return f"{mark}{inner}{mark}"
        return inner

    def words(self, node: Node, code: bool = False) -> str:
        """Return the Markdown of what *node* holds, on one line.

        In code, the runs of text between two links are joined before they
        become one code span each.
        """
        pieces: list[str] = []
        run = ""
        for child in node.children:
            if code and (isinstance(child, str) or not self._linked(child)):
                run += child if isinstance(child, str) else self._code_text(child)
                continue
            if run:
                pieces.append(code_span(run))
                run = ""
            pieces.append(self.inline(child, code))
        if run:
            pieces.append(code_span(run))
        return squeeze(" ".join(piece for piece in pieces if piece) if code else "".join(pieces))

    def _linked(self, node: Node) -> bool:
        """Say whether an element is, or holds, a link."""
        if _left_out(node):
            return False
        if node.tag == "a" and node.attrs.get("href"):
            return True
        return any(self._linked(child) for child in node.elements())

    def _code_text(self, node: Node) -> str:
        """Return the text of an element inside code, without what is left out."""
        if _left_out(node):
            return ""
        if node.tag == "br":
            return " "
        return "".join(
            child if isinstance(child, str) else self._code_text(child) for child in node.children
        )

    # --- Blocks -------------------------------------------------------------

    def flow(self, node: Node) -> list[str]:
        """Return the blocks of what *node* holds: its runs of text as paragraphs."""
        blocks: list[str] = []
        line: list[Node | str] = []

        def close() -> None:
            words = squeeze("".join(self.inline(each) for each in line))
            if words:
                blocks.append(_unblocked(words))
            line.clear()

        for child in node.children:
            if isinstance(child, Node) and child.tag in _BLOCKS and not _left_out(child):
                close()
                blocks.extend(self.block(child))
            else:
                line.append(child)
        close()
        return blocks

    def block(self, node: Node) -> list[str]:
        """Return the blocks one block element becomes."""
        tag = node.tag
        if re.fullmatch(r"h[1-6]", tag):
            # A class's kinds (``abstract``, ``final``) are qualifiers on the
            # line below its title, as a member's are below its declaration.
            labels = node.find("span", "mlabels")
            words = self.words(_without(node, "mlabels") if labels is not None else node)
            if not words:
                return []
            marks = [] if labels is None else [code_span(text_of(e)) for e in labels.elements()]
            heading = f"{'#' * int(tag[1])} {words}"
            return [heading, " ".join(m for m in marks if m)] if any(marks) else [heading]
        if tag in ("ul", "ol"):
            return [self.listing(node)]
        if tag == "dl":
            return self.definitions(node)
        if tag == "table":
            return self.table(node)
        if tag == "pre":
            return [_fence(text_of(node).strip("\n"))]
        if tag == "div":
            if node.has("fragment"):
                return [self.example(node)]
            if node.has("memitem"):
                return self.member(node)
            if node.has("navpath"):
                return self.path(node)
            if node.has("compoundTemplParams"):
                return [_fence(squeeze(self._code_text(node)), "cpp")]
        if tag == "nav" and node.has("letters"):
            letters = [self.inline(each) for each in node.elements()]
            return [" ".join(each for each in letters if each)]
        if tag == "nav" and node.has("trail"):
            return self.trail(node)
        if tag in ("p", "nav") and node.has("eyebrow"):
            return self.eyebrow(node)
        if tag == "figure":
            caption = node.find("figcaption")
            return self.flow(caption) if caption is not None else []
        return self.flow(node)

    def eyebrow(self, node: Node) -> list[str]:
        """Return what a page is and the scope it is in as one line (``Class · a::b``)."""
        parts: list[str] = []
        for each in node.elements():
            if each.tag in ("ul", "ol"):
                steps = [self.words(item) for item in each.elements() if item.tag == "li"]
                parts.append("::".join(step for step in steps if step))
            else:
                parts.append(squeeze(self.inline(each)))
        return [_unblocked(" · ".join(part for part in parts if part))]

    def trail(self, node: Node) -> list[str]:
        """Return the topics a page is in as one line of links after the word Topics."""
        items = node.find("ol")
        items = node.find("ul") if items is None else items
        steps = [] if items is None else [self.words(e) for e in items.elements() if e.tag == "li"]
        steps = [each for each in steps if each]
        return [f"Topics: {' · '.join(steps)}"] if steps else []

    def path(self, node: Node) -> list[str]:
        """Return the folders above a page as one line."""
        steps = [self.words(each) for each in node.elements() if each.tag == "ul"]
        items = node.find("ul")
        if items is not None:
            steps = [self.words(each) for each in items.elements() if each.tag == "li"]
        return [" / ".join(each for each in steps if each)]

    def listing(self, node: Node, ordered: bool | None = None) -> str:
        """Return a list, each item's blocks under its mark."""
        ordered = node.tag == "ol" if ordered is None else ordered
        items: list[str] = []
        number = int(node.attrs.get("start", "1") or 1)
        for item in node.elements():
            if item.tag != "li":
                continue
            mark = f"{number}. " if ordered else "- "
            number += 1
            items.append(self.item(mark, self.flow(item)))
        return "\n".join(each for each in items if each.strip())

    @staticmethod
    def item(mark: str, blocks: Sequence[str]) -> str:
        """Return one item of a list: its first block after *mark*, the rest under it."""
        if not blocks:
            return ""
        indent = " " * len(mark)
        lines = _join(blocks).split("\n")
        return (
            mark + lines[0] + "".join("\n" + (indent + each if each else "") for each in lines[1:])
        )

    def definitions(self, node: Node) -> list[str]:
        """Return a definition list: a section of a member's text, or terms and values."""
        terms = [each for each in node.elements() if each.tag == "dt"]
        values = [each for each in node.elements() if each.tag == "dd"]
        # A title that ends in a colon of its own is labeled with one colon.
        label = self.words(terms[0]).rstrip(":").rstrip() if terms else ""
        if node.has("params") or node.has("retval") or node.has("tparams"):
            table = node.find("table")
            rows = self.parameters(table) if table is not None else []
            return [f"**{label}:**", "\n".join(rows)] if rows else []
        if node.has("section"):
            blocks = [b for value in values for b in self.flow(value)]
            if not blocks:
                return []
            labeled = _labeled(f"**{label}:**" if label else "", blocks, " ")
            if any(name in _QUOTED for name in node.classes):
                return [
                    "\n".join(
                        f"> {line}" if line else ">" for line in _join(labeled).split("\n")
                    )
                ]
            return labeled
        blocks = []
        for child in node.elements():
            if child.tag == "dt":
                words = self.words(child)
                if words:
                    blocks.append(f"**{words}**")
            elif child.tag == "dd":
                blocks.extend(self.flow(child))
        return blocks

    def parameters(self, table: Node) -> list[str]:
        """Return the rows of a table of parameters, values or template parameters."""
        rows: list[str] = []
        for row in self._rows(table):
            cells = [each for each in row.elements() if each.tag == "td"]
            if not cells:
                continue
            direction = next((c for c in cells if c.has("paramdir")), None)
            named = next((c for c in cells if c.has("paramname")), None)
            rest = [c for c in cells if c is not direction and c is not named]
            words = code_span(self._code_text(named)) if named is not None else ""
            way = squeeze(text_of(direction)).strip("[]") if direction is not None else ""
            head = words + (f" ({way})" if way else "")
            blocks = _labeled(head, [b for cell in rest for b in self.flow(cell)], ": ")
            rows.append(self.item("- ", blocks))
        return rows

    @staticmethod
    def _rows(table: Node) -> list[Node]:
        """Return a table's rows, in order, wherever they are below it."""
        rows: list[Node] = []
        for child in table.elements():
            if child.tag == "tr":
                rows.append(child)
            elif child.tag in ("thead", "tbody", "tfoot"):
                rows.extend(each for each in child.elements() if each.tag == "tr")
        return rows

    def table(self, node: Node) -> list[str]:
        """Return the blocks a table becomes, by what it lays out."""
        if node.has("memberdecls"):
            return self.declarations(node)
        if node.has("directory"):
            return [self.directory(node)]
        if node.has("fieldtable"):
            return self.fields(node)
        return self.grid(node)

    def declarations(self, node: Node) -> list[str]:
        """Return a list of members: its heading, then a line a member."""
        blocks: list[str] = []
        items: list[str] = []
        template = ""

        def close() -> None:
            if items:
                blocks.append("\n".join(items))
                items.clear()

        for row in self._rows(node):
            kind = row.attrs.get("class", "")
            cells = [each for each in row.elements() if each.tag == "td"]
            if kind.startswith("memitem"):
                left = next((c for c in cells if c.has("memItemLeft")), None)
                right = next((c for c in cells if c.has("memItemRight")), None)
                templ = next((c for c in cells if c.has("memTemplParams")), None)
                if templ is not None and right is None:
                    template = squeeze(self._code_text(templ))
                    continue
                items.append("- " + self.declared(template, left, right))
                template = ""
            elif kind.startswith("memdesc"):
                desc = next((c for c in cells if c.has("mdescRight")), None)
                words = self.description(desc) if desc is not None else ""
                if words and items:
                    items[-1] += f": {words}"
            else:
                close()
                for cell in cells:
                    blocks.extend(self.flow(cell))
        close()
        return blocks

    def declared(self, template: str, left: Node | None, right: Node | None) -> str:
        """Return one member's line of a list of members."""
        before = squeeze(self._code_text(left)) if left is not None else ""
        links = [] if right is None else [c for c in right.elements() if c.tag == "a"]
        plain = (
            right is not None
            and not before
            and not template
            and len(links) == 1
            and not squeeze("".join(c for c in right.children if isinstance(c, str)))
        )
        if plain:
            return self.words(right) if right is not None else ""
        said = [code_span(template)] if template else []
        if before:
            said.append(code_span(before))
        if right is not None:
            said.append(self.words(right, code=True))
        return " ".join(each for each in said if each)

    def description(self, cell: Node) -> str:
        """Return a member's line of description, without its link to "More"."""
        kept = Node(cell.tag, cell.attrs)
        for child in cell.children:
            more = (
                isinstance(child, Node)
                and child.tag == "a"
                and child.attrs.get("href", "").endswith("#details")
            )
            if not more:
                kept.children.append(child)
        return self.words(kept)

    def directory(self, node: Node) -> str:
        """Return a tree of entries (topics, classes, files) as a nested list."""
        lines: list[str] = []
        for row in self._rows(node):
            level = next((int(c[6:]) for c in row.classes if re.fullmatch(r"level-\d+", c)), 1)
            cells = [each for each in row.elements() if each.tag == "td"]
            entry = next((c for c in cells if c.has("entry")), cells[0] if cells else None)
            desc = next((c for c in cells if c.has("desc")), None)
            if entry is None:
                continue
            words = self.words(entry)
            about = self.words(desc) if desc is not None else ""
            lines.append("  " * (level - 1) + "- " + words + (f": {about}" if about else ""))
        return "\n".join(lines)

    def fields(self, node: Node) -> list[str]:
        """Return the fields of a table of enumerators or fields: its label, then a line each."""
        label = ""
        items: list[str] = []
        for row in self._rows(node):
            heads = [each for each in row.elements() if each.tag == "th"]
            if heads:
                label = squeeze(" ".join(self.words(each) for each in heads))
                continue
            cells = [each for each in row.elements() if each.tag == "td"]
            named = next((c for c in cells if c.has("fieldname")), None)
            rest = [c for c in cells if c is not named]
            head = code_span(self._code_text(named)) if named is not None else ""
            blocks = _labeled(head, [b for cell in rest for b in self.flow(cell)], ": ")
            items.append(self.item("- ", blocks))
        return ([f"**{label}:**"] if label else []) + (["\n".join(items)] if items else [])

    def grid(self, node: Node) -> list[str]:
        """Return a table of text as a Markdown table, its first row the head."""
        rows = []
        for row in self._rows(node):
            cells = [each for each in row.elements() if each.tag in ("td", "th")]
            if cells:
                rows.append(
                    [
                        " ".join(self.flow(each)).replace("|", "\\|").replace("\n", " ")
                        for each in cells
                    ]
                )
        if not rows:
            return []
        width = max(len(each) for each in rows)
        rows = [each + [""] * (width - len(each)) for each in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines += ["| " + " | ".join(each) + " |" for each in rows[1:]]
        return ["\n".join(lines)]

    def example(self, node: Node) -> str:
        """Return an example as a fenced block, a line of it a line."""
        lines = [
            self._code_text(each).rstrip()
            for each in node.elements()
            if each.tag == "div" and each.has("line")
        ]
        return _fence("\n".join(lines) if lines else text_of(node).strip("\n"))

    def member(self, node: Node) -> list[str]:
        """Return one member: its declaration, its qualifiers, its text."""
        blocks: list[str] = []
        proto = next((c for c in node.elements() if c.has("memproto")), None)
        doc = next((c for c in node.elements() if c.has("memdoc")), None)
        if proto is not None:
            said = [
                squeeze(self._code_text(each))
                for each in proto.elements()
                if each.has("memtemplate") or each.has("memname")
            ]
            if not said:
                said = [squeeze(self._code_text(proto))]
            blocks.append(_fence("\n".join(each for each in said if each), "cpp"))
            labels = proto.find("span", "mlabels")
            if labels is not None:
                marks = [code_span(text_of(each)) for each in labels.elements()]
                if marks:
                    blocks.append(" ".join(marks))
        if doc is not None:
            blocks.extend(self.flow(doc))
        return blocks

    # --- The page -----------------------------------------------------------

    def convert(self, page: str) -> str:
        """Return the twin of *page*."""
        root = parse(page)
        main = root.find("main")
        note = root.find("p", "api-note")
        head = [
            COMMENT.format(url=self.url)
            + " The page is the original; these are the same words, with the markup taken off. -->",
            CANONICAL_PREFIX + self.url,
        ]
        # The note is the first thing a page's content says, so the twin says
        # it there; a page that holds it outside its content gets it here.
        if note is not None and (main is None or main.find("p", "api-note") is None):
            head.append(squeeze(text_of(note)))
        body = self.flow(main) if main is not None else []
        return "\n".join(head) + "\n\n" + _join(body) + "\n"


def twin_name(name: str) -> str:
    """Return the file of the twin of the page *name* (``x.html`` → ``x.md``)."""
    return api_reference_links.twin(name)


def page_name(twin: str) -> str:
    """Return the file of the page the twin *twin* is of."""
    return twin.removesuffix(".md") + ".html"


def twin_url(name: str, reference: str = api_reference_links.REFERENCE) -> str:
    """Return the absolute address of the twin of the page *name*."""
    return f"{api_reference_links.ORIGIN}{reference}/{twin_name(name)}"


def convert(name: str, page: str, reference: str = api_reference_links.REFERENCE) -> str:
    """Return the twin of the page *name*, whose markup is *page*."""
    return Twin(name, reference).convert(page)


_TITLE_RE = re.compile(r'<h1 class="title">(.*?)</h1>', re.S)
_DESCRIPTION_RE = re.compile(r'<meta name="description" content="([^"]*)"')
_TAG_RE = re.compile(r"<[^>]*>")


@dataclass(frozen=True)
class Entry:
    """What ``llms.txt`` says of one page."""

    name: str
    title: str
    description: str
    size: int


def entry(name: str, page: str, twin: str) -> Entry:
    """Return what ``llms.txt`` says of the page *name*: its title, its description."""
    found = _TITLE_RE.search(page)
    title = squeeze(html.unescape(_TAG_RE.sub("", found.group(1)))) if found else name
    described = _DESCRIPTION_RE.search(page)
    description = squeeze(html.unescape(described.group(1))) if described else ""
    return Entry(name, title, description, len(twin.encode("utf-8")))


def write_twins(out: Path, names: Sequence[str]) -> list[Entry]:
    """Write the twin of each page of *names* beside it; return what each says.

    Shared out among processes by ``build_api_reference.py``, so it reads the
    pages it converts from the tree.
    """
    done: list[Entry] = []
    for name in names:
        page = (out / name).read_text(encoding="utf-8")
        twin = convert(name, page)
        (out / twin_name(name)).write_text(twin, encoding="utf-8")
        done.append(entry(name, page, twin))
    return done


def kind_of(name: str) -> str:
    """Return the section of ``llms.txt`` the page *name* is listed in."""
    return next((kind for kind, pattern in KINDS if pattern.fullmatch(name)), "Pages")


def bar_pages(index: str) -> list[str]:
    """Return the pages the bar of the index page *index* names, in its order."""
    bar = re.search(r'<nav class="api-nav".*?</nav>', index, re.S)
    if bar is None:
        return []
    targets = re.findall(r'<a\b[^>]*\bhref="([^"]*)"', bar.group(0))
    return [api_reference_links.page_of(each) for each in targets]


def topic_order(topics: str) -> list[str]:
    """Return the topics in the order the page of topics lists them."""
    seen: list[str] = []
    main = topics[topics.find("<main") :]
    for target in re.findall(r'<a\b[^>]*\bhref="(group__[^"#]+)"', main):
        name = api_reference_links.page_of(target)
        if name not in seen:
            seen.append(name)
    return seen


def _line(item: Entry, reference: str) -> str:
    """Return one page's entry of ``llms.txt``: its title, its description, its twin."""
    # A description that opens a list on its page stops on a colon, which
    # here would lead into the link to the twin.
    described = item.description.rstrip(": ")
    about = f": {described}" if described else ""
    url = api_reference_links.canonical(item.name, reference)
    # llmstxt.org's reference parser reads a title up to its first "]",
    # escaped or not, so a bracket of a name (``char[N]``) is written as its
    # full-width form, which reads the same.
    title = item.title.replace("[", "\uff3b").replace("]", "\uff3d")
    return f"- [{title}]({url}){about} ([Markdown]({twin_url(item.name, reference)}))"


def llms_file(
    entries: Iterable[Entry],
    index: str,
    topics: str = "",
    reference: str = api_reference_links.REFERENCE,
) -> str:
    """Return the ``llms.txt`` of a reference: every page, by kind, with its twin.

    The format is the one llmstxt.org's reference parser reads: an ``# H1``,
    one ``>`` line, prose, then ``##`` sections whose every line is one link
    entry on one line. The front pages (the bar's, but the indexes) and the
    topics come first; the indexes of names are under ``## Optional``.

    :param entries: What each page says (:func:`entry`).
    :param index: The reference's index page, whose bar names the front pages
        and whose head gives the summary.
    :param topics: The page of topics, whose order the topics take.
    :param reference: The reference's directory.
    """
    by_name = {each.name: each for each in entries}
    front = [n for n in bar_pages(index) if n in by_name and not _INDEX_RE.fullmatch(n)]
    order = {name: at for at, name in enumerate(topic_order(topics))}
    sections: dict[str, list[Entry]] = {}
    for name, item in by_name.items():
        if name in front:
            continue
        sections.setdefault(kind_of(name), []).append(item)
    sections.get("Topics", []).sort(key=lambda e: (order.get(e.name, len(order)), e.title))
    for kind, items in sections.items():
        if kind != "Topics":
            items.sort(key=lambda e: (e.title.casefold(), e.name))
    bar = bar_pages(index)
    if "Indexes" in sections:
        sections["Indexes"].sort(
            key=lambda e: (bar.index(e.name) if e.name in bar else len(bar), e.title, e.name)
        )
    summary = entry(api_reference_links.INDEX, index, "")
    note = re.search(r'<p class="api-note">(.*?)</p>', index, re.S)
    total = sum(each.size for each in by_name.values())
    title = re.search(r"<title>([^<]*)</title>", index)
    heading = html.unescape(title.group(1)).split(" · ")[-1] if title else "Reference"
    lines = [
        f"# {heading}",
        "",
        f"> {summary.description or summary.title}",
        "",
        *([squeeze(html.unescape(_TAG_RE.sub("", note.group(1)))), ""] if note else []),
        f"Every page of this reference is also served as Markdown at its own address with"
        f" `.md` appended (`index.md` for the reference's own address), converted from the page"
        f" when it is built: the same words, with the markup taken off. There is no single file"
        f" of them all: the {len(by_name):,} twins hold {total / 1e6:.1f} MB.",
        "",
        "## Front pages",
        "",
        *(_line(by_name[name], reference) for name in front),
    ]
    for kind, _pattern in KINDS[:-1]:
        if sections.get(kind):
            lines += ["", f"## {kind}", "", *(_line(e, reference) for e in sections[kind])]
    if sections.get("Pages"):
        lines += ["", "## Pages", "", *(_line(e, reference) for e in sections["Pages"])]
    if sections.get("Indexes"):
        lines += ["", "## Optional", "", *(_line(e, reference) for e in sections["Indexes"])]
    return "\n".join(lines) + "\n"
