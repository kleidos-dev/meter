#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""check_reference.py — the published site holds nothing its host blocks, no private value.

``scripts/build_reference.py`` runs this over the tree it wrote; it reads the
bytes that would go out, not the configuration that made them. The rules are
the Kleidos firmware repository's guard of its published reference
(``scripts/check_api_reference.py``) as they apply to this tree, each with its
name:

* ``kind`` — a file that is not a page, a page's Markdown twin, the site's
  ``llms.txt``, one of the search's two lists or an image of the documents.
* ``script`` — what the host's policy blocks or a static host has no use
  for: a ``<script>`` other than the host's search's one tag
  (:data:`SEARCH_SCRIPT`), an ``<iframe>``, an inline ``style=``, an ``on…=``
  handler, a ``javascript:`` address.
* ``search`` — a page that loads the host's search more than once; and, while
  the pages load it, a list of the search (``search-pages.json``,
  ``search-members.json``) that is missing, not the shape the build writes,
  over its budget, naming a page or an anchor the tree does not hold, or
  holding two entries a reader cannot tell apart
  (``api_reference_search.problems``, the firmware repository's own rule).
* ``source`` — a page of source, or a link to one.
* ``broken-link`` — a link or an image in the tree whose file, or whose anchor
  in that page, is not there; and an outbound link with no host.
* ``clean-link`` — a link to a page written with its ``.html``: the host serves
  each page at its address without it.
* ``canonical`` — a page without ``<link rel="canonical">`` to its own clean
  address.
* ``twin`` — a page without its twin, a twin without its page, a page whose
  head does not name its twin, an ``llms.txt`` entry whose page is not there.
* ``note`` — a page whose content does not open with the note "In
  development." (the site follows the main branch).
* ``title`` — a page without exactly one ``<h1>``.
* ``doubled-text`` — a link whose words are written again right after it,
  what Doxygen 1.17.0 makes of a Markdown link to an anchor
  (``scripts/doxygen_md_filter.py`` writes those links as HTML so it never
  does).
* ``private`` — a private value, in a page, a twin, the ``llms.txt`` or a list
  of the search: a MAC
  address, a USB serial number or a serial port named by one (the patterns
  of ``.gitleaks.toml`` and ``.gitleaks-mac.toml``, their fakes allowed), a
  private, loopback-free or link-local IPv4 address, a host name of a local
  network (``.local``, ``.lan``, ``.home``, ``.internal``), a path of
  somebody's computer (``/Users/…``, ``/home/…``, ``C:\\Users\\…``), an e-mail
  address other than ``mail@kleidos.dev``.
* ``not-public`` — a link to a repository that is not public
  (:data:`NOT_PUBLIC`): it would lead a reader to a page that is not there.

Usage:
    python3 scripts/check_reference.py <tree>        # check a built tree
    python3 scripts/check_reference.py --selftest    # plant each defect, see it caught

Exit code: 0 when nothing is found, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts" / "published_reference"))

import api_reference_search  # noqa: E402

#: Where the host serves the site.
ORIGIN = "https://api.kleidos.dev/"
REFERENCE = "meter"

#: The note every page opens its content with.
NOTE = '<p class="api-note"><strong>In development.</strong>'

#: The one e-mail address a page may name.
MAIL = "mail@kleidos.dev"

#: The repositories a link must not lead to, as a path on github.com: the
#: Kleidos firmware repository is not public yet.
NOT_PUBLIC = ("jmrplens/kleidos",)

#: The kinds of file the tree may hold besides its pages and twins.
IMAGE_SUFFIXES = frozenset({".png", ".svg", ".jpg", ".jpeg", ".gif", ".webp"})
LLMS = "llms.txt"

#: The one script a page may load: the host's search, deferred, from the
#: host's assets beside the site (``docs/published-reference/header.html``).
SEARCH_SCRIPT = '<script src="../assets/search.js" defer></script>'

#: The search's two lists, beside the pages (``scripts/build_reference.py``).
SEARCH_LISTS = frozenset(api_reference_search.FILES)

#: The files of the host a page links outside its tree (the header's).
HOST_FILES = frozenset(
    {"../", "/llms.txt", "../assets/tokens.css", "../assets/api.css", "../favicon.ico"}
)
_HOST_PREFIX = "../assets/"

_HREF_RE = re.compile(r'\b(?:href|src)="([^"]*)"')
_ID_RE = re.compile(r'\bid="([^"]+)"')
_MAIN_RE = re.compile(r'<main id="doc-content">\s*(.{0,200})', re.S)
_CANONICAL_RE = re.compile(r'<link rel="canonical" href="([^"]+)"/>')
_TWIN_LINK_RE = re.compile(r'<link rel="alternate" type="text/markdown" href="([^"]+)"')
_BLOCKED: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r'<script\b(?! src="\.\./assets/search\.js" defer></script>)', re.I),
        "a <script> that is not the host's search",
    ),
    (re.compile(r"<iframe\b", re.I), "an <iframe>"),
    (re.compile(r"\sstyle=\"", re.I), "an inline style"),
    (re.compile(r"\son[a-z]+=\"", re.I), "an event handler"),
    (re.compile(r"javascript:", re.I), "a javascript: address"),
)
_DOUBLED_RE = re.compile(r"<a\b[^>]*>([^<]{3,})</a>\1")
_SOURCE_RE = re.compile(r"_source(?:\.html)?(?:[\"#]|$)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_LOCAL_HOST_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*\.(?:local|lan|home|internal)\b", re.I)
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/|C:\\Users\\)[A-Za-z0-9._-]+")
_LLMS_ENTRY_RE = re.compile(r"^- \[[^\]]*\]\((https://[^)\s]+)\)", re.M)


@dataclass(frozen=True)
class Finding:
    """One thing wrong with the tree: where, which rule, what."""

    where: str
    rule: str
    what: str

    def __str__(self) -> str:
        """Return the finding as one line."""
        return f"{self.where}: [{self.rule}] {self.what}"


@dataclass(frozen=True)
class Rule:
    """A private-value rule of the gitleaks files, with the fakes its allowlists let through."""

    name: str
    pattern: re.Pattern[str]
    fakes: tuple[re.Pattern[str], ...]


def _gitleaks_rules(root: Path) -> list[Rule]:
    """Return the private-value rules of the gitleaks files under *root*.

    An allowlist's fakes apply to the rules it names (``targetRules``), as
    gitleaks applies them; a path allowlist (build output) is not about values.
    """
    rules: list[Rule] = []
    for name in (".gitleaks.toml", ".gitleaks-mac.toml"):
        path = root / name
        if not path.is_file():
            continue
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        fakes: dict[str, list[re.Pattern[str]]] = {}
        for allow in data.get("allowlists", []):
            for target in allow.get("targetRules", []):
                fakes.setdefault(target, []).extend(re.compile(e) for e in allow.get("regexes", []))
        rules += [
            Rule(rule["id"], re.compile(rule["regex"]), tuple(fakes.get(rule["id"], [])))
            for rule in data.get("rules", [])
            if "regex" in rule
        ]
    return rules


#: The private-value rules, each with the fakes a document may show.
PRIVATE_RULES = _gitleaks_rules(REPO_ROOT)


def private_values(text: str) -> list[str]:
    """Return what in *text* is shaped like a private value, each said once."""
    found: list[str] = []
    for rule in PRIVATE_RULES:
        for match in rule.pattern.finditer(text):
            value = match.group(match.lastindex or 0)
            if not any(fake.search(value) for fake in rule.fakes):
                found.append(f"a value gitleaks refuses ({rule.name}: {value})")
    for match in _IPV4_RE.finditer(text):
        try:
            address = ipaddress.IPv4Address(match.group(1))
        except ipaddress.AddressValueError:
            continue
        if (address.is_private or address.is_link_local) and not address.is_loopback:
            found.append(f"a private address ({address})")
    found += [f"a local network's host ({m.group(0)})" for m in _LOCAL_HOST_RE.finditer(text)]
    found += [f"a path of somebody's computer ({m.group(0)})" for m in _HOME_PATH_RE.finditer(text)]
    found += [
        f"an e-mail address ({m.group(0)})"
        for m in _EMAIL_RE.finditer(text)
        if m.group(0).lower() != MAIL
    ]
    found += [
        f"a link to {repo}, which is not public"
        for repo in NOT_PUBLIC
        if f"github.com/{repo}/" in text
        or f'github.com/{repo}"' in text
        or f"githubusercontent.com/{repo}/" in text
    ]
    return list(dict.fromkeys(found))


def page_of(target: str) -> str:
    """Return the file of the tree a relative link's path names (``""`` is the index)."""
    path = target.split("#", 1)[0].split("?", 1)[0]
    if path in ("", ".", "./"):
        return "index.html"
    tail = path.rsplit("/", 1)[-1]
    return path if "." in tail else f"{path}.html"


def served(name: str) -> str:
    """Return the address of the page *name* relative to its directory."""
    return "" if name == "index.html" else name.removesuffix(".html")


def _page_findings(
    name: str, text: str, anchors: dict[str, set[str]], files: set[str]
) -> Iterable[Finding]:
    """Return what is wrong with the page *name*, whose text is *text*."""
    for pattern, what in _BLOCKED:
        if pattern.search(text):
            yield Finding(name, "script", what)
    if text.count(SEARCH_SCRIPT) > 1:
        yield Finding(name, "search", "the host's search loaded more than once")
    for match in _DOUBLED_RE.finditer(text):
        yield Finding(
            name, "doubled-text", f"the words of a link written twice: {match.group(1)[:60]}"
        )
    if text.count("<h1") != 1:
        yield Finding(name, "title", f"{text.count('<h1')} first-level headings")
    main = _MAIN_RE.search(text)
    if main is None or not main.group(1).startswith(NOTE):
        yield Finding(name, "note", "the content does not open with the note")
    canonical = _CANONICAL_RE.search(text)
    expected = f"{ORIGIN}{REFERENCE}/{served(name)}"
    if canonical is None or canonical.group(1) != expected:
        yield Finding(name, "canonical", f"no canonical address {expected}")
    twin = _TWIN_LINK_RE.search(text)
    if twin is None or twin.group(1) != name.removesuffix(".html") + ".md":
        yield Finding(name, "twin", "the head does not name the page's twin")
    for target in _HREF_RE.findall(text):
        yield from _link_findings(name, target, anchors, files)


def _link_findings(
    name: str, target: str, anchors: dict[str, set[str]], files: set[str]
) -> Iterable[Finding]:
    """Return what is wrong with the link *target* of the page *name*."""
    if re.match(r"^(?:https?|ftp)://$", target):
        yield Finding(name, "broken-link", f"{target}: an outbound link with no host")
        return
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
        if _SOURCE_RE.search(target) and REFERENCE in target:
            yield Finding(name, "source", f"{target}: a page of source")
        return
    if target in HOST_FILES or target.startswith(_HOST_PREFIX):
        return
    path, _, fragment = target.partition("#")
    if _SOURCE_RE.search(path + '"'):
        yield Finding(name, "source", f"{target}: a link to a page of source")
    if path.endswith(".html"):
        yield Finding(name, "clean-link", f"{target}: written with its .html")
    page = page_of(target) if path else name
    if page not in files:
        yield Finding(name, "broken-link", f"{target}: no {page} in the tree")
    elif fragment and page.endswith(".html") and fragment not in anchors.get(page, set()):
        yield Finding(name, "broken-link", f"{target}: no anchor {fragment} in {page}")


def findings(tree: Path) -> list[Finding]:
    """Return everything wrong with the built tree *tree*."""
    found: list[Finding] = []
    files = {path.name for path in tree.iterdir() if path.is_file()}
    for path in sorted(tree.iterdir()):
        if path.is_dir():
            found.append(Finding(path.name + "/", "kind", "a folder"))
        elif not (
            path.suffix in (".html", ".md")
            or path.name == LLMS
            or path.name in SEARCH_LISTS
            or path.suffix.lower() in IMAGE_SUFFIXES
        ):
            found.append(Finding(path.name, "kind", "a kind of file the site does not publish"))
    pages = {
        name: (tree / name).read_text(encoding="utf-8")
        for name in sorted(files)
        if name.endswith(".html")
    }
    anchors = {name: set(_ID_RE.findall(text)) for name, text in pages.items()}
    for name, text in pages.items():
        found += _page_findings(name, text, anchors, files)
        if name.removesuffix(".html") + ".md" not in files:
            found.append(Finding(name, "twin", "no Markdown twin"))
    for name in sorted(files):
        if name.endswith(".md") and name.removesuffix(".md") + ".html" not in files:
            found.append(Finding(name, "twin", "a twin with no page"))
    llms = (tree / LLMS).read_text(encoding="utf-8") if LLMS in files else ""
    if not llms:
        found.append(Finding(LLMS, "twin", "no llms.txt"))
    prefix = f"{ORIGIN}{REFERENCE}/"
    for url in _LLMS_ENTRY_RE.findall(llms):
        if not url.startswith(prefix) or page_of(url.removeprefix(prefix)) not in files:
            found.append(Finding(LLMS, "twin", f"{url}: an entry whose page is not in the tree"))
    if any(SEARCH_SCRIPT in text for text in pages.values()):
        held = {name: frozenset(ids) for name, ids in anchors.items()}
        found += [
            Finding(file, "search", what)
            for file, what in api_reference_search.problems(tree, pages, held)
        ]
    for name in sorted(files):
        if name.endswith((".html", ".md")) or name == LLMS or name in SEARCH_LISTS:
            text = pages.get(name) or (tree / name).read_text(encoding="utf-8")
            found += [
                Finding(name, "private" if "not public" not in what else "not-public", what)
                for what in private_values(text)
            ]
    return found


def run(tree: Path) -> int:
    """Check *tree*, say what was found and return the exit code."""
    found = findings(tree)
    pages = sum(1 for path in tree.iterdir() if path.suffix == ".html")
    for each in found:
        print(f"check_reference: {each}")
    if found:
        print(f"check_reference: {len(found)} findings in {pages} pages")
        return 1
    print(f"check_reference: {pages} pages, nothing found")
    return 0


# ---------------------------------------------------------------------------
# Self-test: a good tree, then each defect planted in a copy of it.
# ---------------------------------------------------------------------------


def _page(name: str, body: str, extra_head: str = "") -> str:
    """Return a page of a planted tree, good unless *body* or *extra_head* says otherwise."""
    canonical = f"{ORIGIN}{REFERENCE}/{served(name)}"
    twin = name.removesuffix(".html") + ".md"
    return (
        "<!doctype html><html><head>"
        '<link rel="stylesheet" href="../assets/api.css"/>'
        f'<link rel="canonical" href="{canonical}"/>'
        f'<link rel="alternate" type="text/markdown" href="{twin}" title="This page in Markdown"/>'
        f"{extra_head}</head><body>"
        f'<main id="doc-content">{NOTE} It changes without notice.</p>'
        f'<h1 class="title">{name}</h1>{body}</main></body></html>'
    )


def _good_tree(tree: Path) -> None:
    """Write a tree with nothing wrong in it into *tree*."""
    pages = {
        "index.html": (
            '<p id="top">See <a href="usage_page#start">the start</a> and'
            ' <a href="mailto:mail@kleidos.dev">us</a>.</p>'
        ),
        "usage_page.html": (
            '<h2 id="start">Start</h2><p><a href="./">Back</a>'
            ' <img src="live.png" alt="The view"/> MAC</p>'
        ),
    }
    for name, body in pages.items():
        (tree / name).write_text(_page(name, body, SEARCH_SCRIPT), encoding="utf-8")
        (tree / (name.removesuffix(".html") + ".md")).write_text(f"# {name}\n", encoding="utf-8")
    api_reference_search.write(tree, sorted(pages))
    (tree / "live.png").write_bytes(b"\x89PNG")
    (tree / LLMS).write_text(
        f"# Site\n\n> A site.\n\n## Front pages\n\n- [Usage]({ORIGIN}{REFERENCE}/usage_page)\n",
        encoding="utf-8",
    )


Plant = Callable[[Path], None]


def _edit(name: str, old: str, new: str) -> Plant:
    """Return a plant that replaces *old* by *new* in the file *name*."""

    def plant(tree: Path) -> None:
        path = tree / name
        text = path.read_text(encoding="utf-8")
        if old not in text:
            raise ValueError(f"the plant does not apply to {name}: {old!r}")
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    return plant


def _add(name: str, text: str) -> Plant:
    """Return a plant that writes the file *name*."""

    def plant(tree: Path) -> None:
        (tree / name).write_text(text, encoding="utf-8")

    return plant


#: Each planted defect, with the rule that must see it.
PLANTS: tuple[tuple[str, str, Plant], ...] = (
    ("kind", "a stylesheet", _add("doxygen.css", "body{}")),
    ("script", "a script", _edit("index.html", "<p id", "<script>x()</script><p id")),
    (
        "script",
        "another script from the host's assets",
        _edit("index.html", "<p id", '<script src="../assets/other.js" defer></script><p id'),
    ),
    ("search", "the search loaded twice", _edit("index.html", "<p id", SEARCH_SCRIPT + "<p id")),
    (
        "search",
        "a list of the search missing",
        lambda tree: (tree / api_reference_search.MEMBERS_FILE).unlink(),
    ),
    (
        "search",
        "a list of the search naming a page the tree does not hold",
        _edit(api_reference_search.PAGES_FILE, '"page","usage_page"', '"page","gone_page"'),
    ),
    (
        "search",
        "a list of the search that is not JSON",
        _add(api_reference_search.PAGES_FILE, "[not json"),
    ),
    (
        "script",
        "an inline style",
        _edit("index.html", '<p id="top"', '<p style="color:red" id="top"'),
    ),
    ("script", "a handler", _edit("index.html", '<p id="top"', '<p onclick="x()" id="top"')),
    (
        "source",
        "a link to source",
        _edit("index.html", 'href="usage_page#start"', 'href="main_8cpp_source"'),
    ),
    (
        "broken-link",
        "a missing page",
        _edit("index.html", 'href="usage_page#start"', 'href="gone_page"'),
    ),
    (
        "broken-link",
        "a missing anchor",
        _edit("index.html", 'href="usage_page#start"', 'href="usage_page#end"'),
    ),
    (
        "broken-link",
        "a missing image",
        _edit("usage_page.html", 'src="live.png"', 'src="dead.png"'),
    ),
    (
        "broken-link",
        "a hostless link",
        _edit("index.html", 'href="usage_page#start"', 'href="https://"'),
    ),
    (
        "clean-link",
        "a link with .html",
        _edit("index.html", 'href="usage_page#start"', 'href="usage_page.html#start"'),
    ),
    (
        "canonical",
        "another page's address",
        _edit("usage_page.html", f'{REFERENCE}/usage_page"', f'{REFERENCE}/"'),
    ),
    ("twin", "a page without its twin", lambda tree: (tree / "usage_page.md").unlink()),
    ("twin", "a twin without its page", _add("old_page.md", "# Old\n")),
    ("twin", "an llms.txt entry with no page", _edit(LLMS, "/usage_page)", "/gone_page)")),
    (
        "note",
        "a page without the note",
        _edit("usage_page.html", "<strong>In development.</strong>", "<strong>Note.</strong>"),
    ),
    (
        "doubled-text",
        "a link's words twice",
        _edit("index.html", "the start</a>", "the start</a>the start"),
    ),
    ("title", "two titles", _edit("index.html", '<p id="top">', '<h1>Again</h1><p id="top">')),
    (
        "private",
        "a MAC address",
        _edit(
            "usage_page.html", " MAC<", " " + ":".join(("dc", "54", "75", "12", "34", "56")) + "<"
        ),
    ),
    (
        "private",
        "a USB serial port",
        _edit("index.html", "<p id", "<p>/dev/cu." + "usbmodem" + "5A7A01234</p><p id"),
    ),
    ("private", "a private address", _edit("index.html", "<p id", "<p>192.168.1.20</p><p id")),
    ("private", "a local host", _edit("index.html", "<p id", "<p>bench-mac.local</p><p id")),
    (
        "private",
        "a path of somebody's computer",
        _edit("index.html", "<p id", "<p>/Users/someone/x</p><p id"),
    ),
    (
        "private",
        "another e-mail address",
        _edit("index.html", "<p id", "<p>someone@example.org</p><p id"),
    ),
    ("private", "a private value in a twin", _edit("usage_page.md", "\n", "\n10.0.0.7\n")),
    (
        "private",
        "a private value in a list of the search",
        _edit(api_reference_search.PAGES_FILE, '"usage_page.html"', '"10.0.0.7"'),
    ),
    (
        "not-public",
        "a link to the firmware repository",
        _edit(
            "index.html",
            "<p id",
            '<p><a href="https://github.com/jmrplens/kleidos/blob/main/x.md">x</a></p><p id',
        ),
    ),
)


def selftest() -> int:
    """Plant each defect in a good tree and require its rule to see it; return the exit code."""
    failed = 0
    with tempfile.TemporaryDirectory(prefix="check-reference-") as tmp:
        good = Path(tmp) / "good"
        good.mkdir()
        _good_tree(good)
        clean = findings(good)
        if clean:
            print("check_reference: selftest: the good tree has findings:")
            print("\n".join(f"  {each}" for each in clean))
            failed += 1
        for at, (rule, what, plant) in enumerate(PLANTS):
            tree = Path(tmp) / f"plant{at}"
            tree.mkdir()
            _good_tree(tree)
            plant(tree)
            rules = {each.rule for each in findings(tree)}
            if rule not in rules:
                print(
                    f"check_reference: selftest: {what} was not seen by [{rule}]"
                    f" (saw {sorted(rules)})"
                )
                failed += 1
    print(
        f"check_reference: selftest: {len(PLANTS) - failed} of {len(PLANTS)} plants seen"
        + ("" if not failed else ", FAILED")
    )
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments and check."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("tree", nargs="?", type=Path, help="the built tree")
    parser.add_argument(
        "--selftest", action="store_true", help="plant each defect and see it caught"
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.tree is None:
        parser.error("a tree, or --selftest")
    return run(args.tree)


if __name__ == "__main__":
    sys.exit(main())
