#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The shape of a published page: what Doxygen's markup becomes for a reader.

``build_api_reference.py`` makes of what Doxygen wrote a tree its host can
serve. This module is the part of that work that a reader sees: each function
takes a page, or a piece of one, and returns it with one thing changed.

- **Opening** (:func:`open_with_the_text`): the one line of a page is its first
  paragraph and the text follows it, without a heading that says there is a
  text, and without the line said twice.
- **Contents** (:func:`contents_after_title`): the links to a page's own parts,
  after its title, as a list in a landmark.
- **Declarations** (:func:`flat_declarations`): one line, or one line per
  parameter, instead of two nested tables that no phone can show.
- **Tables** (:func:`directions_after_names`, :func:`heads_on_tables`,
  :func:`labels_on_tables`, :func:`boxed_tables`): a parameter's direction after
  its name, as a word; the heads of a table of parameters, for who cannot see
  its columns; the head of each cell of a wide one, for a window that has no
  room for the columns; and a box around each table of text, which scrolls it
  where it is wider than the column.
- **Names** (:func:`break_names`): where a long name may break: after ``::``,
  ``/`` and ``_``, and nowhere else while it fits.
- **Lists** (:func:`tree_rows`, :func:`kinds_as_words`, :func:`index_entries`,
  :func:`owners_linked`): a row says how deep it is, and what it is with a word
  and not a letter; in an index by letter the letter is a heading that says it,
  the name is the link, and a namespace member's namespace leads to its page.
- **Diagrams** (:func:`drawing`): the drawing itself, in the page, with no color,
  face or size of its own (the host's sheet draws it in both themes), its boxes
  rounded, and the badge of its kind beside its caption.
- **Heads** (:func:`head`): the page's own title, and its first sentence.
- **Title** (:func:`opening`, :func:`note_in_main`): what a page is and where,
  above its bare name; the topics it is in as a trail; the note first in the
  content.

Nothing here reads a file or knows of a build.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Callable

_TAG_RE = re.compile(r"<[^>]*>")


def plain(markup: str) -> str:
    """Return the words of a piece of a page, in one line."""
    return " ".join(html.unescape(_TAG_RE.sub("", markup)).split())


# --- The opening -------------------------------------------------------------

_OPENING_RE = re.compile(
    r'(<div class="contents">\s*)'
    r'(?:<p>((?:(?!</p>).)*?)\s*<a href="#details">More\.\.\.</a></p>\s*)?'
    r'<a name="details" id="details"></a>'
    r'<h2 id="header-details" class="groupheader">Detailed Description</h2>\s*'
    r'(<div class="textblock">(?:<div class="compoundTemplParams">.*?</div>)?)'
    r"(?:<p>((?:(?!</?p>).)*)</p>\s*(</div>)?)?",
    re.S,
)
_BRIEF_RE = re.compile(
    r'(<div class="contents">\s*)<p>((?:(?!</p>).)*?)\s*<a href="#details">More\.\.\.</a></p>', re.S
)


def open_with_the_text(text: str) -> str:
    """Return a page that opens with its one line and goes on with its text.

    The layout puts the text right after the line, so a heading between the two
    and a link to what comes next say nothing, and neither does the text's first
    paragraph, which is the line again. A text that is only the line goes.

    Args:
        text: The page.
    """

    def opening(match: re.Match[str]) -> str:
        contents, brief, block, first, closed = match.groups()
        rest = match.group(0)[match.start(4) - 3 - match.start() :] if first is not None else ""
        if brief is None:
            return f'{contents}<a id="details"></a>\n{block}{rest}'
        line = f'{contents}<a id="details"></a>\n<p class="brief">{brief}</p>\n'
        if first is None or plain(first) != plain(brief):
            return f"{line}{block}{rest}"
        if closed is not None and "compoundTemplParams" not in block:
            return line.rstrip("\n")
        return f"{line}{block}{closed or ''}"

    done, opened = _OPENING_RE.subn(opening, text, count=1)
    return done if opened else _BRIEF_RE.sub(r'\1<p class="brief">\2</p>', text, count=1)


# --- The title, and the contents under it ------------------------------------

_SUMMARY_RE = re.compile(
    r'<div class="summary">\s*(?P<links>.*?)\s*</div>\s*(?P<title><div class="headertitle">.*?)'
    r"(?=</div><!--header-->)",
    re.S,
)
_LINK_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.S)


#: The name of a page's contents: the landmark's, and the fold's that holds them.
CONTENTS = "On this page"


def contents_after_title(text: str) -> str:
    """Return a page whose contents follow its title, as a list in a landmark.

    The list follows a fold's control, closed until it is opened, and is drawn
    while that control is open; where the window has room for a rail beside the
    page the sheet draws the list there and not the control. The list is never
    the fold's content: WebKit leaves a closed fold's content out of what a
    reader's tools are given, whatever the sheet draws of it. Its words over the
    rail are hidden from those tools, which name the landmark by its label.
    """

    def moved(match: re.Match[str]) -> str:
        items = "".join(f"<li>{link}</li>" for link in _LINK_RE.findall(match["links"]))
        nav = (
            f'<nav class="summary toc" aria-label="{CONTENTS}"><details class="toc-fold">'
            f'<summary class="toc-title">{CONTENTS}</summary></details><div class="toc-body">'
            f'<p class="toc-head" aria-hidden="true">{CONTENTS}</p>'
            f'<ul class="toc-list">{items}</ul></div></nav>'
        )
        return f"{match['title'].rstrip()}\n{nav}\n"

    return _SUMMARY_RE.sub(moved, text, count=1)


MAIN = '<main id="doc-content">'
_NAVPATH_RE = re.compile(r'<nav id="nav-path" class="navpath"([^>]*)>(.*?)</nav>', re.S)
_STEP_RE = re.compile(r'<li class="navelem">(.*?)</li>', re.S)


def path_to_here(name: str, text: str) -> str:
    """Return a page whose path does not link itself.

    The path of a file is its folders, which have no pages: with nothing to
    follow it is no landmark, so it goes where the page's content is.

    Args:
        name: The page's file.
        text: The page.
    """

    moved = ""

    def path(match: re.Match[str]) -> str:
        nonlocal moved
        steps = _STEP_RE.findall(match.group(2))
        items = [
            f'<li class="navelem" aria-current="page">{_TAG_RE.sub("", step)}</li>'
            if f'href="{name}"' in step
            else f'<li class="navelem">{step}</li>'
            for step in steps
        ]
        listed = f"<ul>{''.join(items)}</ul>"
        if any("<a " in item for item in items):
            return f'<nav id="nav-path" class="navpath"{match.group(1)}>{listed}</nav>'
        kind = "navpath folders" if re.search(r"_8\w+\.html$", name) else "navpath"
        moved = f'\n<div id="nav-path" class="{kind}">{listed}</div>'
        return ""

    text = _NAVPATH_RE.sub(path, text, count=1)
    return text.replace(MAIN, MAIN + moved, 1)


_TITLE_RE = re.compile(r'(<h1 class="title">)(.*?)(</h1>)', re.S)
_HEAD_TITLE_RE = re.compile(r"<title>(.*?) · ([^<]*)</title>")
_LABELS_RE = re.compile(r'<span class="mlabels">(?:<span[^>]*>[^<]*</span>)*</span>')
_FIRST_WORDS_RE = re.compile(
    r'<p class="brief">(.*?)</p>|<div class="textblock">(?:<[^p][^>]*>)*\s*(?:<p>)?(.*?)(?:</p>|</div>)',
    re.S,
)

#: The longest a page's description is, in characters: what a search result shows.
DESCRIPTION = 200


#: A paragraph, after the first one a page's text opens with.
_PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.S)
#: Where a sentence may end: its stop after a word, and a capital after it.
_SENTENCE_END_RE = re.compile(r"(?<=[\w)\]'\"][.!?])\s+(?=[A-Z])")
#: The abbreviations whose stop never ends a sentence, and the one that ends
#: it only before a capital (``etc.``), all in lower case.
_ABBREVIATIONS = frozenset({"e.g.", "i.e.", "cf.", "vs."})
_ABBREVIATION_THEN_CAPITAL = frozenset({"etc."})


def ends_sentence(words: str, stop: int) -> bool:
    """Return whether the stop that ends at *stop* in *words* ends a sentence.

    Not after an abbreviation (``e.g.``, ``i.e.``, ``cf.``, ``vs.``; ``etc.``
    only before a capital) and not inside brackets left open, where a
    brief's aside goes on (``(e.g. Load the RAM. Then refresh it)``).
    """
    before = words[:stop]
    said = before.rsplit(None, 1)[-1].lstrip("([{\"'").lower() if before.strip() else ""
    if said in _ABBREVIATIONS:
        return False
    if said in _ABBREVIATION_THEN_CAPITAL and not words[stop:].lstrip()[:1].isupper():
        return False
    return before.count("(") <= before.count(")") and before.count("[") <= before.count("]")


def description(text: str) -> str:
    """Return the first words of a page, to say what it is.

    A paragraph with no words (a Markdown page opens with Doxygen's anchor
    alone in one) is passed over for the next. A paragraph that opens a list
    stops on a colon, which is left out. One longer than a description ends
    with the last sentence that fits, or, when not even the first does, at a
    word and with an ellipsis.
    """
    content = text.partition('<div class="contents">')[2]
    match = _FIRST_WORDS_RE.search(content)
    words = plain(match.group(1) or match.group(2) or "") if match else ""
    if match and not words and match.group(1) is None:
        for paragraph in _PARAGRAPH_RE.finditer(content, match.end()):
            words = plain(paragraph.group(1))
            if words:
                break
    words = words.rstrip(" :")
    if len(words) <= DESCRIPTION:
        return words
    ends = [
        m.start()
        for m in _SENTENCE_END_RE.finditer(words)
        if m.start() <= DESCRIPTION and ends_sentence(words, m.start())
    ]
    if ends:
        return words[: ends[-1]]
    return words[:DESCRIPTION].rsplit(" ", 1)[0].rstrip(" ,;:(") + "…"


#: A compound's title as Doxygen writes it, in plain words: its qualified
#: name, then its kind and "Reference".
_KIND_TITLE_RE = re.compile(
    r"(?P<name>.+?) (?P<kind>Class|Struct|Union|Namespace|File|Interface|Concept)"
    r"(?P<template> Template)? Reference",
    re.S,
)


def tab_title(name: str) -> str:
    """Return what a page's ``<title>`` names it by, from its plain, escaped title.

    A compound is its bare name, its kind and the scope it is in
    (``BandedFrame (class) · kleidos::hal::display::driver``), never Doxygen's
    "Class Reference": the head's title is the tab, the history and a screen
    reader's name of the page, and the name first keeps a phone's tab short.
    A file has no scope here; any other page keeps its title.
    """
    found = _KIND_TITLE_RE.fullmatch(name)
    if found is None:
        return name
    kind = found["kind"].lower() + (" template" if found["template"] else "")
    qualified = found["name"].strip()
    bare = qualified if kind == "file" else _last_name(qualified)
    scope = qualified[: len(qualified) - len(bare)].rstrip().removesuffix("::")
    return f"{bare} ({kind}) · {scope}" if scope else f"{bare} ({kind})"


def head(text: str) -> str:
    """Return a page whose head says its title and its first words."""
    title = _TITLE_RE.search(text)
    if title is None:
        return text
    name = tab_title(html.escape(plain(_LABELS_RE.sub("", title.group(2))), quote=False))
    words = html.escape(description(text), quote=True)
    said = f'\n<meta name="description" content="{words}"/>' if words else ""
    return _HEAD_TITLE_RE.sub(lambda m: f"<title>{name} · {m.group(2)}</title>{said}", text, count=1)


# --- The opening: what the page is, its name, the topics it is in ------------

#: A title as Doxygen writes it for a compound: its qualified name, its kind,
#: and the labels after it (``abstract``, ``final``).
_COMPOUND_TITLE_RE = re.compile(
    r'(?P<open><div class="headertitle">)<h1 class="title">(?P<name>.*?) '
    r"(?P<kind>Class|Struct|Union|Namespace|File|Interface|Concept)(?P<template> Template)?"
    r" Reference(?P<labels>\s*<span class=\"mlabels\">.*?)?</h1>",
    re.S,
)
_GROUP_TITLE_RE = re.compile(r'(?P<open><div class="headertitle">)(?=<h1 class="title">)')
_SCOPES_RE = re.compile(
    r'\n?<(nav|div) id="nav-path" class="navpath(?: folders)?"[^>]*><ul>(.*?)</ul></\1>', re.S
)
_ITEM_RE = re.compile(r'<li class="navelem"( aria-current="page")?>(.*?)</li>', re.S)
_INGROUPS_RE = re.compile(r'<div class="ingroups">((?:(?!</?div\b).)*)</div>', re.S)
_NOTE_RE = re.compile(r'\n?<p class="api-note">.*?</p>', re.S)

#: The name of the topics a page is in, read in order from the widest.
TRAIL = "Topics this is in"


def _last_name(qualified: str) -> str:
    """Return a qualified name's last part: what follows its last ``::`` outside ``<>``."""
    depth = 0
    cut = 0
    index = 0
    while index < len(qualified):
        if qualified.startswith("&lt;", index):
            depth += 1
            index += 4
        elif qualified.startswith("&gt;", index):
            depth -= 1
            index += 4
        elif depth == 0 and qualified.startswith("::", index):
            index += 2
            cut = index
        else:
            index += 1
    return qualified[cut:].strip()


def opening(name: str, text: str) -> str:
    """Return a page that opens with what it is, its name, and the topics it is in.

    A compound's title is its name alone (``BandedFrame``); what it is and the
    scope it is in (``display::driver``, or a file's folders) go above it, as a
    line of small capitals, its scopes each a link where it has a page. Doxygen's
    "Class Reference" is gone. A topic says it is one. The topics a page is in
    are a trail under its title, from the widest, the separators read as nothing.

    Args:
        name: The page's file.
        text: The page (:func:`path_to_here` done, and :func:`head`, whose head
            names the kind and the scope beside the bare name).
    """
    scopes = ""
    match = _SCOPES_RE.search(text)
    if match is not None:
        items = [item for current, item in _ITEM_RE.findall(match.group(2)) if not current]
        if re.search(r"_8\w+\.html$", name):
            path = "/".join(_TAG_RE.sub("", item) for item in items)
            scopes = f' <span class="eyebrow-scope">{path}</span>' if path else ""
        elif items:
            listed = "".join(f"<li>{item}</li>" for item in items)
            scopes = f' <ul class="eyebrow-scope">{listed}</ul>'

    def eyebrow(kind: str) -> str:
        said = f'<span class="eyebrow-kind">{kind}</span>{scopes}'
        if "<a " in scopes:
            return f'<nav id="nav-path" class="eyebrow" aria-label="Where this is">{said}</nav>'
        return f'<p class="eyebrow">{said}</p>'

    def compound(found: re.Match[str]) -> str:
        kind = found["kind"].lower()
        kind = f"{kind} template" if found["template"] else kind
        bare = found["name"].strip() if kind == "file" else _last_name(found["name"])
        return (
            f'{found["open"]}{eyebrow(kind.capitalize())}'
            f'<h1 class="title">{bare}{found["labels"] or ""}</h1>'
        )

    text, named = _COMPOUND_TITLE_RE.subn(compound, text, count=1)
    if not named and name.startswith("group__"):
        scopes = ""
        text, named = _GROUP_TITLE_RE.subn(lambda m: m["open"] + eyebrow("Topic"), text, count=1)
    if named and match is not None:
        text = _SCOPES_RE.sub("", text, count=1)

    def trail(found: re.Match[str]) -> str:
        steps = [step.strip() for step in found.group(1).split("&raquo;")]
        listed = "".join(f"<li>{step}</li>" for step in steps if step)
        return f'<nav class="trail" aria-label="{TRAIL}"><ol>{listed}</ol></nav>'

    return _INGROUPS_RE.sub(trail, text, count=1)


def note_in_main(text: str) -> str:
    """Return a page whose "In development" note is the first thing its content says.

    The header template writes the note after the bar, where the bar's height
    would grow by it on every page; in the content it is a line the page opens
    with, and still in a landmark.
    """
    match = _NOTE_RE.search(text)
    if match is None or MAIN not in text or match.start() > text.index(MAIN):
        return text
    text = text[: match.start()] + text[match.end() :]
    return text.replace(MAIN, f"{MAIN}\n{match.group(0).strip()}", 1)


# --- Members -----------------------------------------------------------------

_MEMTITLE_RE = re.compile(
    r'<h2 class="memtitle"><span class="permalink"><a href="([^"]+)">[^<]*</a></span>(.*?)</h2>',
    re.S,
)


def member_titles(text: str) -> str:
    """Return a page whose members are headings under their section's.

    The link to a member follows its name and says where it leads.
    """

    def titled(match: re.Match[str]) -> str:
        target, name = match.groups()
        label = html.escape(plain(name), quote=True)
        return (
            f'<h3 class="memtitle">{name.strip()} <span class="permalink">'
            f'<a href="{target}" aria-label="Link to {label}">#</a></span></h3>'
        )

    return _MEMTITLE_RE.sub(titled, text)


_PROTO_RE = re.compile(r'<div class="memproto">(.*?)</div><div class="memdoc">', re.S)
_TEMPLATE_RE = re.compile(r'<div class="memtemplate">\s*(.*?)\s*</div>', re.S)
_DECLARED_RE = re.compile(r'<table class="memname">(.*?)</table>', re.S)
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r'<td(?: class="([^"]*)")?>(.*?)</td>', re.S)
_PARAMETER_RE = re.compile(r'<span class="paramname"><em>(.*?)</em></span>', re.S)
_BADGES_RE = re.compile(r'<td class="mlabels-right">\s*(.*?)\s*</td>', re.S)
#: Doxygen's word for an ``enum class``, which is not the language's.
_STRONG_RE = re.compile(r'<span class="mlabel strong">strong</span>')
_NO_BADGES_RE = re.compile(r'<span class="mlabels"></span>')
#: The shapes of a declaration's row, by the classes of its cells.
_NAMED = ("memname", "", "paramtype", "paramname")
_ROWS = frozenset(
    {
        ("memname",),
        ("memname", "", "paramname", "", ""),
        (*_NAMED, "", ""),
        _NAMED,
        ("paramkey", "", "paramtype", "paramname"),
    }
)


def _parameter(kind: str, name: str) -> str:
    """Return one parameter of a declaration: its type and its name."""
    name = _PARAMETER_RE.sub(
        lambda m: f'<span class="paramname">{m.group(1)}</span>' if m.group(1) else "", name
    )
    kind = kind.strip().replace("&#160;", " ")
    return f'<span class="paramtype">{kind}</span> {name}'.strip() if kind else name


def declaration(table: str) -> str | None:
    """Return a declaration as one line of text, or one per parameter.

    Args:
        table: The rows of the table Doxygen lays it out with.

    Returns:
        The declaration, or None when a row is of a shape this does not know.
    """
    rows = [_CELL_RE.findall(row) for row in _ROW_RE.findall(table)]
    if not rows or any(tuple(kind for kind, _ in row) not in _ROWS for row in rows):
        return None
    name = str(rows[0][0][1]).strip()
    if len(rows[0]) == 1:
        return name
    parameters = []
    end = ""
    for row in rows:
        cells = [cell for _, cell in row]
        kinds = [kind for kind, _ in row]
        kind = cells[kinds.index("paramtype")] if "paramtype" in kinds else ""
        last = cells[kinds.index("paramname")]
        # What closes the list is in cells of its own, or in the last name's.
        if kinds[-1] != "paramname":
            end = f"{cells[-2]}{cells[-1].rstrip()}"
        elif "&#160;)" in last:
            last, _, after = last.rpartition("&#160;)")
            end = f"){after.rstrip()}"
        parameters.append(_parameter(kind, last.strip()))
    if len(parameters) == 1:
        return f"{name}({parameters[0]}{end}"
    # No line of its own for the parenthesis that closes: the last parameter's.
    parameters[-1] += end
    listed = " ".join(f'<span class="parameter">{each}</span>' for each in parameters)
    return f"{name}({listed}"


def flat_declarations(text: str) -> str:
    """Return a page whose declarations are text, with their badges after them."""

    def flat(match: re.Match[str]) -> str:
        body = match.group(1)
        table = _DECLARED_RE.search(body)
        line = declaration(table.group(1)) if table else None
        if table is None or line is None:
            return match.group(0).replace("<table ", '<table role="presentation" ')
        templates = "".join(
            f'<div class="memtemplate">{each}</div>' for each in _TEMPLATE_RE.findall(body)
        )
        badges = _BADGES_RE.search(body)
        labels = f"\n{badges.group(1)}" if badges else ""
        return (
            f'<div class="memproto">{templates}<div class="memname">{line}</div>{labels}'
            '</div><div class="memdoc">'
        )

    text = _PROTO_RE.sub(flat, text)
    return _NO_BADGES_RE.sub("", _STRONG_RE.sub("", text))


_MEMBER_RE = re.compile(
    r'(<a id="([^"]+)" name="[^"]*"></a>\s*<h[23] class="memtitle">.*?<div class="memproto">)'
    r"(.*?)(?=<div class=\"memdoc\">)",
    re.S,
)
_OWN_LINK_RE = re.compile(r'<a class="el" href="#([^"]+)"[^>]*>(.*?)</a>', re.S)


def unlink_self(text: str) -> str:
    """Return a page where no declaration links the member it declares."""

    def unlink(match: re.Match[str]) -> str:
        opening, own, declared = match.groups()
        return opening + _OWN_LINK_RE.sub(
            lambda link: link.group(2) if link.group(1) == own else link.group(0), declared
        )

    return _MEMBER_RE.sub(unlink, text)


_MEMBER_ROW_RE = re.compile(r'<tr class="memitem:.*?(?=<tr class="memitem:|</table>)', re.S)
_ROW_NAME_RE = re.compile(
    r'<td class="memItemRight">(?:<a [^>]*>(?P<link>.*?)</a>|<b>(?P<bold>.*?)</b>)', re.S
)
_MORE_RE = re.compile(r'(<a href="[^"]+">)More\.\.\.</a>')
_EMPTY_LEFT_RE = re.compile(r'(<td class="(?:memItemLeft|mdescLeft)">)\s*&#160;</td>')
_LINE_END_RE = re.compile(r"\s*<br />\s*</td>")
_LEFT_END_RE = re.compile(r"&#160;</td>(?=<td class=\"memItemRight\">)")
#: The values of an enumeration in a list, which Doxygen breaks into lines of three.
_VALUES_RE = re.compile(r"\{ <br />(?:(?!</td>).)*\}(?=</td>)", re.S)


def _values(match: re.Match[str]) -> str:
    """Return the values of an enumeration as one run of words."""
    run = match.group(0).replace("<br />", " ").replace("&#160;", " ")
    return " ".join(run.split()).replace(" ,", ",")


def member_rows(text: str) -> str:
    """Return a page whose lists of members say where each *More* leads.

    The lists are laid out with tables, which say nothing of rows or columns.
    """

    def row(match: re.Match[str]) -> str:
        if "More..." not in match.group(0):
            return match.group(0)
        name = _ROW_NAME_RE.search(match.group(0))
        of = plain(name["link"] or name["bold"] or "") if name else ""
        about = f'<span class="visually-hidden"> about {html.escape(of)}</span>' if of else ""
        return _MORE_RE.sub(lambda more: f"{more.group(1)}More{about}</a>", match.group(0))

    text = _MEMBER_ROW_RE.sub(row, text)
    text = _EMPTY_LEFT_RE.sub(r"\1</td>", text)
    text = _LEFT_END_RE.sub("</td>", text)
    text = _LINE_END_RE.sub("</td>", text)
    text = _VALUES_RE.sub(_values, text)
    # The title of a group of members (``@name``) is a heading under the list's.
    return _GROUP_DIV_RE.sub(r'<h3 class="groupHeader">\1</h3>', text)


_GROUP_DIV_RE = re.compile(r'<div class="groupHeader">(.*?)</div>', re.S)
_DECLS_RE = re.compile(r'<table class="memberdecls">(.*?)</table>', re.S)
_DECLS_TITLE_RE = re.compile(r'<h2 id="([^"]+)" class="groupheader">')
_ROW_CELL_RE = re.compile(r"<(tr|td)\b([^>]*)>")


def member_tables(text: str) -> str:
    """Return a page whose lists of members are tables a reader's tools see rows in.

    The sheet lays their rows and cells out as boxes, which takes a table's
    meaning away in some browsers; the roles give it back, so a member's line
    and its brief description are rows of their own and not one run of text.
    A list is named by its heading.
    """

    def table(match: re.Match[str]) -> str:
        rows = match.group(1)
        title = _DECLS_TITLE_RE.search(rows)
        named = f' aria-labelledby="{title.group(1)}"' if title else ""
        rows = _ROW_CELL_RE.sub(
            lambda tag: f'<{tag[1]}{tag[2]} role="{"row" if tag[1] == "tr" else "cell"}">', rows
        )
        return f'<table class="memberdecls" role="table"{named}>{rows}</table>'

    return _DECLS_RE.sub(table, text)


# --- Headings ----------------------------------------------------------------

#: A paragraph of bold words alone, which a comment writes as the title of what
#: follows it.
_LABEL_RE = re.compile(r"<p><b>([^<]+?)</b>(?:\s|&#160;)*</p>")
_ANY_H_RE = re.compile(r'<h([1-6])(?![^>]*class="doxlabel")[\s>]')


def label_headings(text: str) -> str:
    """Return a page whose bold titles of what follows them are headings.

    Each is one level under the heading before it that is not such a title, so
    two of them in a row are of the same level; its colon goes. A reader's
    tools reach it, and print keeps it with what follows it.
    """
    out: list[str] = []
    at = 0
    level = 1
    for found in _LABEL_RE.finditer(text):
        before = text[at : found.start()]
        for heading in _ANY_H_RE.finditer(before):
            level = int(heading.group(1))
        out.append(before)
        words = found.group(1).strip().removesuffix(":").strip()
        own = min(level + 1, 6)
        out.append(f'<h{own} class="doxlabel">{words}</h{own}>')
        at = found.end()
    out.append(text[at:])
    return "".join(out)


# --- Tables ------------------------------------------------------------------

#: What each column of a table of parameters holds, by the class of its cells.
_COLUMNS: dict[str, dict[str, str]] = {
    "params": {"paramdir": "Direction", "paramtype": "Type", "paramname": "Name", "": "Meaning"},
    "tparams": {"paramtype": "Type", "paramname": "Name", "": "Meaning"},
    "retval": {"paramname": "Value", "": "Meaning"},
    "exception": {"paramname": "Exception", "": "Meaning"},
}
_FIELDS_RE = re.compile(rf'(<table class="({"|".join(_COLUMNS)})">)(.*?)(?=</table>)', re.S)


def heads_on_tables(text: str) -> str:
    """Return a page whose tables of parameters have heads, for who hears them."""

    def headed(match: re.Match[str]) -> str:
        opening, kind, body = match.groups()
        first = _ROW_RE.search(body)
        cells = [each for each, _ in _CELL_RE.findall(first.group(1))] if first else []
        names = [_COLUMNS[kind].get(each) for each in cells]
        if not names or None in names:
            return match.group(0)
        heads = "".join(
            f'<th scope="col"><span class="visually-hidden">{name}</span></th>' for name in names
        )
        return f"{opening}<thead><tr>{heads}</tr></thead>{body}"

    return _FIELDS_RE.sub(headed, text)


#: A row of a table of parameters as Doxygen writes it: the direction, then the name.
_DIRECTED_ROW_RE = re.compile(
    r'<tr><td class="paramdir">([^<]*)</td>(<td class="paramname">.*?</td>)', re.S
)
#: Doxygen's way of writing a direction, and how many of them there are.
_DIRECTION_RE = re.compile(r"\[(in|out)(?:,\s*(in|out))?\]")


def directions_after_names(text: str) -> str:
    """Return a page whose parameters say their name first, then their direction.

    The direction is a word in a box of its own (``in``, ``out``, ``in, out``),
    not Doxygen's ``[in,out]``, so the sheet can draw it as the site's badge; a
    parameter that has none keeps an empty cell, so the columns stay one.
    """

    def moved(match: re.Match[str]) -> str:
        said = _DIRECTION_RE.fullmatch(match.group(1).strip())
        badge = ""
        if said is not None:
            words = ", ".join(each for each in said.groups() if each)
            badge = f'<span class="dir">{words}</span>'
        elif match.group(1).strip():
            return match.group(0)
        return f'<tr>{match.group(2)}<td class="paramdir">{badge}</td>'

    return _DIRECTED_ROW_RE.sub(moved, text)


_MARKDOWN_RE = re.compile(r'<table class="markdownTable"([^>]*)>(.*?)</table>', re.S)
_TH_RE = re.compile(r"<th\b[^>]*>(.*?)</th>", re.S)
_TR_RE = re.compile(r"<tr\b.*?</tr>", re.S)
_TD_RE = re.compile(r"<td\b")
#: From how many columns on the first cell of a row does not say what the others
#: are, and from how many a row is too wide for a window under 1100 px.
LABELED_COLUMNS = 3
WIDE_COLUMNS = 5


def labels_on_tables(text: str) -> str:
    """Return a page whose tables say, cell by cell, the head of its column."""

    def labeled(match: re.Match[str]) -> str:
        names = [html.escape(plain(each), quote=True) for each in _TH_RE.findall(match.group(2))]
        wide = " wide" if len(names) >= WIDE_COLUMNS else ""

        def cells(row: re.Match[str]) -> str:
            at = iter(names)
            return _TD_RE.sub(lambda _: f'<td data-label="{next(at, "")}"', row.group(0))

        body = match.group(2)
        if len(names) >= LABELED_COLUMNS:
            body = _TR_RE.sub(cells, body)
        return f'<table class="markdownTable{wide}"{match.group(1)}>{body}</table>'

    return _MARKDOWN_RE.sub(labeled, text)


#: A table of text, as the steps before leave it.
_TEXT_TABLE_RE = re.compile(r'<table class="(?:markdownTable|fieldtable)\b.*?</table>', re.S)


def boxed_tables(text: str) -> str:
    """Return a page whose tables of text each sit in the site's table box.

    The box (``div.data-wrap``) draws the table's outline and scrolls it, with
    its first column held, where it is wider than the column of the page.
    """
    return _TEXT_TABLE_RE.sub(lambda m: f'<div class="data-wrap">{m.group(0)}</div>', text)


# --- Names -------------------------------------------------------------------

_MAIN_RE = re.compile(r'(<main id="doc-content">)(.*)(</main>)', re.S)
#: What no break is written into: an example, whose lines do not wrap, and a drawing.
_WHOLE_RE = re.compile(
    r'(<div class="fragment".*?</div><!-- fragment -->|<pre\b.*?</pre>|<svg\b.*?</svg>|<[^>]*>)',
    re.S,
)
_LONG_NAME_RE = re.compile(r"[^\s&]{14,}|(?:[^\s&]|&\w+;){14,}")
_BREAK_RE = re.compile(r"(::|(?<!/)/(?!/)|_)(?=\w)")


def break_names(text: str) -> str:
    """Return a page whose long names may break after ``::``, ``/`` and ``_``.

    A route or an absolute path (a word that starts with ``/``) stays whole: the
    part after a break would read as a path of its own.
    """

    def broken(name: str) -> str:
        if name.lstrip("(\"'").startswith("/"):
            return name
        return _BREAK_RE.sub(r"\1<wbr/>", name)

    def words(piece: str) -> str:
        return _LONG_NAME_RE.sub(lambda m: broken(m.group(0)), piece)

    def content(match: re.Match[str]) -> str:
        pieces = _WHOLE_RE.split(match.group(2))
        pieces[::2] = [words(piece) if len(piece) >= 14 else piece for piece in pieces[::2]]
        return match.group(1) + "".join(pieces) + match.group(3)

    return _MAIN_RE.sub(content, text, count=1)


# --- Lists and indexes -------------------------------------------------------

_KIND_RE = re.compile(
    r'<span class="icona"><span class="icon">\w</span></span>(<a\b[^>]*\bhref="([^"]*)"[^>]*>.*?</a>'
    r"|<b>.*?</b>)",
    re.S,
)
_KINDS = ("namespace", "class", "struct", "union", "interface", "concept")


def kinds_as_words(text: str, spoken: bool = True) -> str:
    """Return a list whose rows say what they are with a word.

    Args:
        text: The page.
        spoken: Whether a namespace's row says so: where every row is one, none does.
    """

    def worded(match: re.Match[str]) -> str:
        target = match.group(2) or "namespace"
        kind = next((each for each in _KINDS if target.startswith(each)), "")
        if not kind or (kind == "namespace" and not spoken):
            return match.group(1)
        return f'{match.group(1)} <span class="kind">{kind}</span>'

    return _KIND_RE.sub(worded, text).replace(' target="_self"', "")


_TREE_ROW_RE = re.compile(
    r'<tr id="[^"]*" class="(?:even|odd)">(<td class="entry">)((?:<span class="tree-pad"></span>)+)'
)


def tree_rows(text: str) -> str:
    """Return a tree whose rows say how deep they are, where they had a spacer per level."""
    text = _TREE_ROW_RE.sub(
        lambda m: f'<tr class="level-{m[2].count("<span")}">{m[1]}', text
    )
    return text.replace('<table class="directory">', '<table class="directory" role="presentation">')


#: The trees of scopes: each entry is named inside the one above it.
SCOPE_TREES = frozenset({"annotated.html", "namespaces.html"})
_ENTRY_RE = re.compile(
    r'(<tr class="level-(\d+)"><td class="entry"><(?:a\b[^>]*|b)>)(.*?)(?=</(?:a|b)>)', re.S
)


def qualified_names(text: str) -> str:
    """Return a tree of scopes (:func:`tree_rows`) whose entries say their qualified names.

    Doxygen names an entry by its last name alone, so two scopes of one name in
    two others (``kleidos::ble::detail``, ``kleidos::json::detail``) read alike;
    each is named from the top, with a break it may take after each ``::``. The
    indentation stays, to show which entry is in which.
    """
    scopes: list[str] = []

    def qualified(match: re.Match[str]) -> str:
        level = int(match.group(2))
        if level > len(scopes) + 1:
            raise ValueError(f"an entry at level {level} under {len(scopes)} scopes")
        scopes[level - 1 :] = [match.group(3).strip()]
        return match.group(1) + "::<wbr/>".join(scopes)

    return _ENTRY_RE.sub(qualified, text)


_LETTER_RE = re.compile(
    r'(<h2[^>]*><a id="index_[^"]+" name="[^"]*"></a>)- (.+?) -</h2>\s*<ul>', re.S
)
_INDEX_ITEM_RE = re.compile(r'<li>([^<]+?)&#160;:&#160;(<a class="el".*?)</li>', re.S)


def index_entries(text: str) -> str:
    """Return an index by letter whose letters say themselves and whose names are the links.

    A name of several classes is not one link: each class is.
    """

    def entry(match: re.Match[str]) -> str:
        name, links = match.groups()
        targets = re.findall(r'<a class="el" href="([^"]+)"[^>]*>(.*?)</a>', links, re.S)
        if len(targets) == 1:
            href, of = targets[0]
            return (
                f'<li><a class="el" href="{href}">{name}</a> <span class="of">in {of}</span></li>'
            )
        return f'<li><b>{name}</b> <span class="of">in {links.strip()}</span></li>'

    if not _LETTER_RE.search(text):
        return text
    text = _LETTER_RE.sub(
        lambda m: f'{m.group(1)}{m.group(2).upper()}</h2>\n<ul class="names">', text
    )
    return _INDEX_ITEM_RE.sub(entry, text)


_OWNER_RE = re.compile(r'<span class="of">in ([A-Za-z_][\w:]*)</span>')


def namespace_page(scope: str) -> str:
    """Return the file Doxygen writes a namespace's page to (``CASE_SENSE_NAMES = YES``)."""
    return "namespace" + scope.replace("_", "__").replace(":", "_1") + ".html"


def owners_linked(name: str, text: str) -> str:
    """Return a letter of the namespace members whose owner leads to its namespace's page.

    Doxygen links an entry to its member and writes the namespace it is in as
    words; the guard's broken-link rule holds every namespace named so to a
    page of its own.
    """
    if not name.startswith("namespacemembers"):
        return text
    return _OWNER_RE.sub(
        lambda m: f'<span class="of">in <a class="el" href="{namespace_page(m[1])}">{m[1]}</a></span>',
        text,
    )


_LETTERS_RE = re.compile(r'(?:<br\s*/>\s*)?<div class="qindex">(.*?)</div>', re.S)
_SEPARATOR = '<div class="alphasepar"></div>'


def letter_row(name: str, text: str) -> str:
    """Return an index whose row of letters is a landmark that marks the letter it is on."""

    def row(match: re.Match[str]) -> str:
        links = match.group(1).replace(
            f'<a class="qindex" href="{name}#', f'<a class="qindex" aria-current="page" href="{name}#'
        )
        return f'<nav class="letters" aria-label="By letter">{links}</nav>'

    return _LETTERS_RE.sub(row, text, count=1)


def without_separators(text: str) -> str:
    """Return an index without the empty boxes between its letters."""
    return text.replace(_SEPARATOR, "")


def title_of_letters(text: str) -> str:
    """Return a page of an index by letter whose title and head say its letters."""
    letters = re.findall(r'<h2[^>]*><a id="index_[^"]+"[^>]*></a>(.+?)</h2>', text)
    if not letters:
        return text
    said = f": {', '.join(letters)}"
    text = _TITLE_RE.sub(lambda m: f"{m[1]}{m[2].rstrip()}{said}{m[3]}", text, count=1)
    return _HEAD_TITLE_RE.sub(lambda m: f"<title>{m[1]}{said} · {m[2]}</title>", text, count=1)


# --- Anchors -----------------------------------------------------------------

_ID_RE = re.compile(r'(<[a-z][^>]*?\sid=")([^"]+)(")')


def unique_ids(text: str) -> str:
    """Return a page where every id is one element's.

    Doxygen gives the same id to every section a comment names, and to the two
    declarations of a member that one condition or another compiles. The second
    and those after it are numbered, with the links of their own row or heading.
    """
    counted = Counter(match.group(2) for match in _ID_RE.finditer(text))
    repeated = {each for each, times in counted.items() if times > 1}
    if not repeated:
        return text
    seen: Counter[str] = Counter()

    def number(match: re.Match[str]) -> str:
        name = match.group(2)
        if name not in repeated:
            return match.group(0)
        seen[name] += 1
        if seen[name] == 1:
            return match.group(0)
        return f"{match.group(1)}{name}-{seen[name]}{match.group(3)}"

    text = _ID_RE.sub(number, text)

    def follow(match: re.Match[str]) -> str:
        own, at = match.group(1), match.group(2)
        return match.group(0).replace(f'href="#{own}"', f'href="#{own}-{at}"')

    text = re.sub(r'<tr class="memitem:[^"]*" id="r_(\w+)-(\d+)">.*?</tr>', follow, text)
    return re.sub(
        r'<a id="(\w+)-(\d+)" name="[^"]*"></a>\s*<h[23] class="memtitle">.*?</h[23]>',
        follow,
        text,
    )


# --- The way back ------------------------------------------------------------

#: From how many characters of markup on a page is long enough to need a way back.
LONG_PAGE = 40_000
TO_TOP = '<p class="to-top"><a href="#top">Back to the top</a></p>\n'
_DOCUMENTED_RE = re.compile(r'<a name="doc-[\w-]+" id="doc-[\w-]+"></a><h2 ')


def ways_back(text: str) -> str:
    """Return a long page with a link to its top at each part's end."""
    if len(text) < LONG_PAGE:
        return text
    text = _DOCUMENTED_RE.sub(lambda m: TO_TOP + m.group(0), text)
    return text.replace("</div><!-- contents -->", f"{TO_TOP}</div><!-- contents -->", 1)


# --- Diagrams ----------------------------------------------------------------

#: The elements of a drawing, and the attributes each may keep: its shape. Its
#: colors, its face and its names are the sheet's and the page's.
_DRAWN = frozenset({"svg", "g", "a", "path", "polygon", "polyline", "ellipse", "text"})
_SHAPE = frozenset(
    "class transform points d x y cx cy rx ry text-anchor stroke-dasharray".split()
)
_SVG_RE = re.compile(
    r'<svg width="([\d.]+)pt" height="([\d.]+)pt"\s+viewBox="([^"]+)"[^>]*>(.*)</svg>', re.S
)
_GROUND_RE = re.compile(r'<polygon fill="white" stroke="(?:none|transparent)"[^>]*/>')
_ELEMENT_RE = re.compile(r"<(/?)([a-zA-Z]+)((?:\s+[\w:-]+\s*=\s*\"[^\"]*\")*)\s*(/?)>")
_ATTRIBUTE_RE = re.compile(r"([\w:-]+)\s*=\s*\"([^\"]*)\"")
#: CSS pixels in a point.
PX_PER_PT = 4 / 3
#: The largest a drawing's text is, in points: the small role's 14 px, in a box
#: ``dot`` drew for 11 points of a narrower face.
TEXT_PT = 10.5


#: A box of a drawing: the first shape of a node or a cluster, a polygon.
_BOX_RE = re.compile(r'(<g class="(?:node|cluster)">(?:<a\b[^>]*>)?)<polygon points="([^"]+)"/>')
#: How round a box's corners are, in the drawing's points: the site's
#: smallest radius (6 px) at the drawing's scale.
CORNER_PT = 4.5


def rounded(points: str) -> str | None:
    """Return a rectangle's outline as a path with round corners, or nothing.

    Args:
        points: A polygon's points, as ``dot`` writes them; a rectangle is its
            four corners, the first repeated or not.
    """
    pairs = [tuple(float(n) for n in each.split(",")) for each in points.split()]
    if len(pairs) == 5 and pairs[0] == pairs[-1]:
        pairs = pairs[:4]
    xs = sorted({x for x, _ in pairs})
    ys = sorted({y for _, y in pairs})
    if len(pairs) != 4 or len(xs) != 2 or len(ys) != 2:
        return None
    (left, right), (top, bottom) = xs, ys
    r = min(CORNER_PT, (right - left) / 4, (bottom - top) / 4)

    def n(value: float) -> str:
        return f"{value:.2f}".rstrip("0").rstrip(".")

    arc = f"A{n(r)},{n(r)} 0 0 1 "
    return (
        f"M{n(left + r)},{n(top)}H{n(right - r)}{arc}{n(right)},{n(top + r)}"
        f"V{n(bottom - r)}{arc}{n(right - r)},{n(bottom)}"
        f"H{n(left + r)}{arc}{n(left)},{n(bottom - r)}"
        f"V{n(top + r)}{arc}{n(left + r)},{n(top)}Z"
    )


def drawing(svg: str, caption: str) -> str:
    """Return a diagram as an element of the page that shows it.

    Args:
        svg: The file ``dot`` wrote.
        caption: What the diagram says, in words.

    Raises:
        ValueError: When the file holds something a drawing does not.
    """
    found = _SVG_RE.search(svg)
    if found is None:
        raise ValueError("a diagram that is not one drawing in points")
    width, height, box, body = found.groups()
    body = re.sub(r"<!--.*?-->|<title>.*?</title>", "", body, flags=re.S)
    body = _GROUND_RE.sub("", body, count=1)
    linked = False

    def element(match: re.Match[str]) -> str:
        nonlocal linked
        closing, tag, attributes, closed = match.groups()
        if tag not in _DRAWN:
            raise ValueError(f"a diagram with a <{tag}>")
        if closing:
            return f"</{tag}>"
        kept = []
        for key, value in _ATTRIBUTE_RE.findall(attributes):
            if tag == "a" and key in ("href", "xlink:href"):
                linked = True
                kept.append(f' href="{value}"')
            elif key == "font-size":
                kept.append(f' font-size="{min(float(value), TEXT_PT):g}"')
            elif key in _SHAPE:
                kept.append(f' {key}="{value}"')
        return f"<{tag}{''.join(kept)}{closed}>"

    drawn = _ELEMENT_RE.sub(element, body)
    drawn = re.sub(r">\s*\n\s*<", "><", drawn).strip()

    def corners(match: re.Match[str]) -> str:
        path = rounded(match.group(2))
        return match.group(0) if path is None else f'{match.group(1)}<path d="{path}"/>'

    drawn = _BOX_RE.sub(corners, drawn)
    if re.search(r"<(?!/?(?:svg|g|a|path|polygon|polyline|ellipse|text)\b)", drawn):
        raise ValueError("a diagram with something that is no shape")
    role = "" if linked else ' role="img"'
    said = f"<title>{caption}</title>" if caption else ""
    hidden = "" if caption else ' aria-hidden="true"'
    w, h = (f"{float(each) * PX_PER_PT:.0f}" for each in (width, height))
    return (
        f'<svg class="drawing"{role}{hidden} width="{w}" height="{h}" viewBox="{box}">'
        f"{said}{drawn}</svg>"
    )


#: The badge a diagram wears: a drawing, not the device.
DIAGRAM_BADGE = "Diagram"

_GRAPH_RE = re.compile(
    r'<div class="dotgraph">\s*(?P<frame><iframe\b[^>]*>.*?</iframe>)\s*'
    r'(?:<div class="caption">\s*(?P<caption>.*?)\s*</div>\s*)?</div>',
    re.S,
)
_SOURCE_RE = re.compile(r'\bsrc="([^"]+)"')


def draw_diagrams(text: str, read: Callable[[str], str]) -> str:
    """Return a page that holds its diagrams, each with its badge and its caption under it.

    Args:
        text: The page as Doxygen wrote it.
        read: Returns what a diagram's file holds.
    """

    def drawn(match: re.Match[str]) -> str:
        source = _SOURCE_RE.search(match["frame"])
        assert source is not None
        words = html.escape(plain(match["caption"] or ""), quote=False)
        under = f"<figcaption>{words}</figcaption>" if words else ""
        svg = drawing(read(source.group(1)), words)
        badge = f'<span class="prov">{DIAGRAM_BADGE}</span>'
        return f'<figure class="diagram">{svg}{badge}{under}</figure>'

    return _GRAPH_RE.sub(drawn, text)


# --- A page ------------------------------------------------------------------


def shaped(name: str, text: str) -> str:
    """Return a page in the shape a reader gets, but for its diagrams and its indexes.

    Args:
        name: The page's file.
        text: The page, with its landmarks and its title.
    """
    steps: tuple[Callable[[str], str], ...] = (
        open_with_the_text,
        contents_after_title,
        flat_declarations,
        member_titles,
        label_headings,
        unlink_self,
        member_rows,
        directions_after_names,
        heads_on_tables,
        labels_on_tables,
        boxed_tables,
        without_separators,
        index_entries,
        tree_rows,
        unique_ids,
        ways_back,
        member_tables,
    )
    for step in steps:
        text = step(text)
    text = owners_linked(name, text)
    text = kinds_as_words(path_to_here(name, text), spoken=name != "namespaces.html")
    if name in SCOPE_TREES:
        text = qualified_names(text)
    return break_names(note_in_main(opening(name, head(text))))


_ANCHOR_RE = re.compile(r'<a (?:name="[^"]*" )?id="([^"]*)"(?: name="[^"]*")?></a>')


def finished(name: str, text: str, index: str = "") -> str:
    """Return a page with what needs the whole tree done: its letters and its anchors.

    Args:
        name: The page's file.
        text: The page (:func:`shaped`), its row of letters in it and its links repaired.
        index: The first page of the index by letter it is of, if it is of one.
    """
    if index:
        text = letter_row(name, text)
        if name != index:
            text = title_of_letters(text)
    return _ANCHOR_RE.sub(r'<a id="\1"></a>', text)
