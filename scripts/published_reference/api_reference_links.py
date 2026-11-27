#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the published reference's links at the addresses the host serves.

The host (``site/api/wrangler.jsonc``, ``html_handling: "auto-trailing-slash"``)
serves ``firmware/group__vault.html`` at ``/firmware/group__vault`` and the
index at ``/firmware/``, and answers each old ``.html`` address with a
permanent redirect (``site/api/scripts/stage.mjs``, ``_redirects``). The tree
stays flat and its files keep the names Doxygen gives them; only the links
change, here, as the build's last step (``scripts/build_api_reference.py``):

- a link to a page of the tree, written relative to it and by its file name
  (``group__vault.html``, ``group__vault.html#ga…``), is written without the
  extension (``group__vault``, ``group__vault#ga…``), and a link to the index
  as the directory (``./``, ``./#…``). A page at ``/firmware/x`` sits in the
  same directory as ``/firmware/x.html`` did, so every other relative link
  (``../assets/api.css``, ``../``, ``#anchor``) resolves where it did;
- each page gets ``<link rel="canonical">`` with the clean absolute address a
  reader should keep, ``<link rel="alternate" type="text/markdown">`` naming
  its Markdown twin (the page's address with ``.md``, ``index.md`` for the
  index: ``api_reference_twins.py``) and ``<link rel="describedby">`` naming
  the host's ``/llms.txt``, as kleidos.dev's pages do.

Everything before this step reads file names (the marks of the bar, the
anchors, the letters of an index), so it runs last. The guard reads the
result (``scripts/check_api_reference.py``, rules ``clean-link``,
``canonical`` and ``twin``), and :func:`page_of` is how it, and the prose rules, map a
clean link back to its file.
"""

from __future__ import annotations

import re

#: Where the host serves the tree, and the reference's directory on it.
ORIGIN = "https://api.kleidos.dev/"
REFERENCE = "firmware"

#: The page a directory's address serves.
INDEX = "index.html"

#: A link to a page of the tree by its file name: relative, in the same
#: directory (no ``/``), with no scheme or query, and with or without a
#: fragment. ``href`` and an inline drawing's ``xlink:href`` alike.
_PAGE_LINK_RE = re.compile(r'(\b(?:xlink:)?href=")([^"/:#?]+)\.html(#[^"]*)?(")')

#: Where a page's head ends, before which its canonical address goes.
_HEAD_END = "</head>"

#: The index of the host that llmstxt.org describes, which every page names.
LLMS_INDEX = "/llms.txt"


def served(name: str) -> str:
    """Return the address of the page *name* relative to its directory.

    ``index.html`` is the directory itself (an empty path) and any other page
    its name without ``.html``.
    """
    return "" if name == INDEX else name.removesuffix(".html")


def served_path(path: str) -> str:
    """Return the address the host serves the file at the address *path* at.

    ``/firmware/topics.html`` is ``/firmware/topics``, an ``index.html`` its
    directory (``/firmware/``, ``/``), and anything else itself: what the host's
    ``_redirects`` sends an old address to.
    """
    head, _, tail = path.rpartition("/")
    if not tail.endswith(".html"):
        return path
    return f"{head}/{served(tail)}"


def canonical(name: str, reference: str = REFERENCE) -> str:
    """Return the clean absolute address of the page *name* of a reference."""
    return f"{ORIGIN}{reference}/{served(name)}"


def canonical_link(name: str, reference: str = REFERENCE) -> str:
    """Return the ``<link rel="canonical">`` of the page *name*."""
    return f'<link rel="canonical" href="{canonical(name, reference)}"/>'


def twin(name: str) -> str:
    """Return the file of the Markdown twin of the page *name*, beside it."""
    return name.removesuffix(".html") + ".md"


def head_links(name: str, reference: str = REFERENCE) -> str:
    """Return the ``<link>``s a page's head gets: its address, its twin, the host's index."""
    return "\n".join(
        (
            canonical_link(name, reference),
            f'<link rel="alternate" type="text/markdown" href="{twin(name)}"'
            ' title="This page in Markdown"/>',
            f'<link rel="describedby" href="{LLMS_INDEX}"'
            ' title="LLM context index (llmstxt.org)"/>',
        )
    )


def clean_target(stem: str, fragment: str = "") -> str:
    """Return the clean form of a link to the page *stem* ``.html`` with *fragment*."""
    return ("./" if stem == "index" else stem) + fragment


def clean_links(text: str) -> str:
    """Return *text* with every link to a page of the tree written without ``.html``."""
    return _PAGE_LINK_RE.sub(
        lambda match: match.group(1)
        + clean_target(match.group(2), match.group(3) or "")
        + match.group(4),
        text,
    )


def finish(name: str, text: str, reference: str = REFERENCE) -> str:
    """Return the page *name* with clean links, and its addresses in its head."""
    text = clean_links(text)
    if _HEAD_END in text:
        text = text.replace(_HEAD_END, head_links(name, reference) + "\n" + _HEAD_END, 1)
    return text


def page_of(target: str) -> str:
    """Return the file of the tree a relative link's path names.

    ``""``, ``.`` and ``./`` are the index; a name with no extension is the
    page of that name (``group__vault`` is ``group__vault.html``); anything
    else (``graph.svg``, ``topics.html`` as Doxygen writes it) is itself.
    """
    path = target.split("#", 1)[0].split("?", 1)[0]
    if path in ("", ".", "./"):
        return INDEX
    if path.endswith("/"):
        return path + INDEX
    tail = path.rsplit("/", 1)[-1]
    return path if "." in tail else f"{path}.html"
