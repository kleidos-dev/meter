#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""check_doc_paths.py — every path a document writes in code is here, or says whose it is.

The meter left the Kleidos firmware repository on 2026-11-19, and its documents
still point into that repository: the QA harness that takes the measurements
(``qa/tools/power_campaign.py``), its power store, ``scripts/pio-job``, its
research notes. Written bare, such a path reads as one of this repository and
sends the reader to a file that is not here. ESP-IDF's sources
(``hal/i2c_ll.h``) and MikroE's SDK are the same case. This gate reads every
tracked Markdown file, outside fenced blocks, and fails on an inline code span
that is a repository-relative path, names nothing in this tree and carries no
qualifier.

**A path** is a code span with a ``/`` in it, no space, no URL scheme, not
absolute (``/``, ``~``, ``$``) and with no character a file name here does not
use, that is also file-like: its last part has an extension
(``docs/scpi.md``), it ends in ``/`` (``calibration/records/``), or its first
part is a top-level entry of this tree (``scripts/pio-job``). A line citation
(``:120``, ``:43,83``, ``:1193-1206``) and a ``#fragment`` are cut off first.
So ``origin/main``, ``dVos/dT``, ``GPIO19/20`` and ``kicad/kicad:10.0.8`` are
not paths.

**It names something here** when it is a tracked file or a directory that holds
one, read from the repository's root or from the document's own directory
(``host/README.md`` writes ``tests/test_cli_doc.py``). A part written as a
placeholder (``<serial>``, ``<name>``) or with ``*`` matches any one name, and
such a pattern with no instance yet still names the directory its fixed part
is (``calibration/records/<serial>/<event>.md``, before the first guided
calibration is filed). A path under a build output (``GENERATED``) names
something a build writes and passes; a path whose first part is a placeholder
(``<data>/overrides/...``) is not repository-relative and is not read.

**It is qualified** when the reader is told whose it is:

* it is the text of a link to an ``https:`` address (a permalink:
  ``[`docs/x.md`](https://github.com/.../blob/<commit>/docs/x.md#L10)``);
* its sentence names the repository it belongs to before it (``OWNERS``:
  "the Kleidos firmware repository's ``qa/tools/power_campaign.py``",
  "ESP-IDF's ``hal/i2c_ll.h``"), code spans and link targets not counted;
* or its document is a record whose paths are another repository's by its own
  opening (``RECORDS``), as the 0.1.0 design is.

Usage:
    python3 scripts/check_doc_paths.py              # check every tracked .md file
    python3 scripts/check_doc_paths.py FILE...      # check these files only
    python3 scripts/check_doc_paths.py --selftest   # plant each case, see it judged

Exit code:
    0 — every path resolves or is qualified.
    1 — at least one finding (or a self-test case judged wrongly).
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent

#: Documents whose paths are another repository's, said by their own opening,
#: with the reason. They are records and are not edited to add qualifiers.
RECORDS: dict[str, str] = {
    "CHANGELOG.md": "history: each entry's paths are as they were in its release",
    "docs/firmware-design.md": (
        "the frozen 0.1.0 design, written while the meter was the Kleidos firmware "
        "repository's nested project; its opening says so"
    ),
}

#: Who a path may belong to, and the words in its sentence that name them.
OWNERS: dict[str, re.Pattern[str]] = {
    "the Kleidos firmware repository": re.compile(
        r"\bKleidos\b(?![-_ ]?[Mm]eter\b)|\bfirmware repository\b"
    ),
    "ESP-IDF": re.compile(r"\bESP-IDF\b"),
    "MikroE's SDK": re.compile(r"\bMikroE\b|\bmikrosdk"),
    "the brand repository": re.compile(r"\bbrand repository\b"),
    "Zephyr": re.compile(r"\bZephyr\b"),
    "Linux": re.compile(r"\bLinux\b"),
}

#: Build outputs, from the repository's root: a path under one names what a
#: build writes (``.gitignore`` keeps them out of the tree).
GENERATED = (
    "build/",
    "firmware/.pio/",
    "firmware/compile_commands.json",
    "firmware/sdkconfig.meter",
    "firmware/test/native/build/",
    "host/.venv/",
)

#: An inline code span (one or more backticks, matched by count).
CODE_SPAN = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")

#: A link whose text is the span at hand: ``[`path`](target)``.
LINK_AROUND = re.compile(r"\[(`+)([^`]+)\1\]\(\s*<?([^)\s>]+)")

#: Any link's target, masked before owners are looked for.
LINK_TARGET = re.compile(r"\]\([^)]*\)")

#: A line citation or a fragment after a path.
SUFFIX = re.compile(r"(?::\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*|#[\w.-]*)$")

#: The characters a path here is written with: names, ``.``, ``-``, ``+``,
#: ``@``, placeholders and ``*``.
PATH_CHARS = re.compile(r"^[\w.\-+@<>*]+(?:/[\w.\-+@<>*]+)*/?$")

#: A last part with an extension.
EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,9}$")

#: Where a sentence ends: after ``.``, ``!`` or ``?`` and a space, and at a
#: table cell's border.
SENTENCE_END = re.compile(r"[.!?]\s+|\|")


@dataclass(frozen=True)
class Finding:
    """One path that names nothing here and does not say whose it is."""

    file: str
    line: int
    path: str

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.line}: `{self.path}` is not in this repository; "
            "name its owner in the sentence (OWNERS) or link it to a permalink"
        )


class Tree:
    """The tracked files of a checkout, and the directories that hold them."""

    def __init__(self, files: set[str]) -> None:
        self.files = files
        self.dirs = {str(parent) for name in files for parent in PurePosixPath(name).parents}
        self.dirs.discard(".")
        self.top = {PurePosixPath(name).parts[0] for name in files}

    @classmethod
    def of(cls, root: Path) -> Tree:
        """Read ``git ls-files`` in *root*: the index, so a staged file counts."""
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached"],
            capture_output=True,
            check=True,
        ).stdout.split(b"\0")
        return cls({name.decode() for name in listed if name})

    def holds(self, path: str) -> bool:
        """Whether *path* (from the root, normalized) is a file or a directory here."""
        bare = path.rstrip("/")
        if "<" in bare or "*" in bare:
            pattern = re.sub(r"<[^>/]*>", "*", bare)
            depth = pattern.count("/")
            names = self.files | self.dirs
            if any(
                name.count("/") == depth and fnmatch.fnmatchcase(name, pattern) for name in names
            ):
                return True
            fixed = re.split(r"/[^/]*[<*]", "/" + bare, maxsplit=1)[0].lstrip("/")
            return bool(fixed) and fixed in self.dirs
        if path.endswith("/"):
            return bare in self.dirs
        return bare in self.files or bare in self.dirs


def _normalize(base: PurePosixPath, path: str) -> str | None:
    """*path* read from *base*, ``..`` resolved; ``None`` when it leaves the tree."""
    parts: list[str] = []
    for part in (base / path).parts:
        if part == ".":
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
            continue
        parts.append(part)
    joined = "/".join(parts)
    return joined + "/" if path.endswith("/") and joined else joined


def path_of(span: str, tree: Tree) -> str | None:
    """The repository-relative path a code span writes, or ``None`` if it is none."""
    text = SUFFIX.sub("", span.strip())
    if "/" not in text or "://" in text or text.startswith(("/", "~", "$")):
        return None
    if not PATH_CHARS.match(text):
        return None
    parts = [part for part in text.split("/") if part]
    if not parts or parts[0].startswith("<"):
        return None
    if text.endswith("/") or EXTENSION.search(parts[-1]) or parts[0] in tree.top:
        return text
    return None


def resolves(path: str, document: str, tree: Tree) -> bool:
    """Whether *path*, written in *document*, names a file, a directory or a build output."""
    for base in (PurePosixPath("."), PurePosixPath(document).parent):
        normal = _normalize(base, path)
        if normal is None:
            continue
        if tree.holds(normal):
            return True
        if any(normal == out.rstrip("/") or normal.startswith(out) for out in GENERATED):
            return True
    return False


def _masked(line: str) -> str:
    """*line* with every code span and link target blanked, lengths kept."""
    line = CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)
    return LINK_TARGET.sub(lambda m: "]" + " " * (len(m.group(0)) - 1), line)


def _sentence_start(masked: str, at: int) -> int:
    """Where the sentence that holds column *at* starts."""
    start = 0
    for end in SENTENCE_END.finditer(masked, 0, at):
        start = end.end()
    return start


def qualified(line: str, start: int, end: int) -> bool:
    """Whether the span at ``line[start:end]`` is a permalink's text or follows its owner."""
    for link in LINK_AROUND.finditer(line):
        if link.start() <= start and end <= link.end() and link.group(3).startswith("https:"):
            return True
    masked = _masked(line)
    before = masked[_sentence_start(masked, start) : start]
    return any(owner.search(before) for owner in OWNERS.values())


def _prose(text: str) -> list[tuple[int, str]]:
    """The lines outside fenced blocks, numbered from 1."""
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
        lines.append((number, line))
    return lines


def check_text(document: str, text: str, tree: Tree) -> list[Finding]:
    """The findings of one document's *text*, *document* being its path from the root."""
    if document in RECORDS:
        return []
    found = []
    for number, line in _prose(text):
        for span in CODE_SPAN.finditer(line):
            path = path_of(span.group(2), tree)
            if path is None or resolves(path, document, tree):
                continue
            if qualified(line, span.start(), span.end()):
                continue
            found.append(Finding(document, number, span.group(2).strip()))
    return found


def check(tree: Tree, root: Path, documents: list[str]) -> list[Finding]:
    """The findings of *documents* (paths from *root*)."""
    found = []
    for document in documents:
        found += check_text(document, (root / document).read_text(encoding="utf-8"), tree)
    return found


def stale_records(tree: Tree) -> list[str]:
    """``RECORDS`` entries that name no tracked file."""
    return [name for name in RECORDS if name not in tree.files]


def selftest() -> int:
    """Plant each case on a small tree and check it is judged as documented."""
    tree = Tree({"docs/scpi.md", "docs/usage.md", "host/README.md", "host/tests/test_x.py",
                 "scripts/run_tests.sh", "calibration/records/KM-1/e1.md"})  # fmt: skip
    cases = [
        ("docs/usage.md", "Read `docs/scpi.md` and `../host/README.md`.", True),
        ("host/README.md", "Run `tests/test_x.py`.", True),
        ("docs/usage.md", "Filed under `calibration/records/<serial>/<event>.md`.", True),
        ("docs/usage.md", "Built into `build/doxygen/html`.", True),
        ("docs/usage.md", "One per run, `scripts/<name>.sh`.", True),
        ("docs/usage.md", "One per run, `calibration/new/<id>.json`.", False),
        ("docs/usage.md", "Cited as `docs/scpi.md:120` and `docs/scpi.md#the-line`.", True),
        ("docs/usage.md", "`origin/main`, `dVos/dT`, `GPIO19/20`, `kicad/kicad:10.0.8`.", True),
        ("docs/usage.md", "Kept in `<data>/overrides/<serial>.json`; `/dev/cu.x`.", True),
        ("docs/usage.md", "Run `scripts/pio-job` first.", False),
        ("docs/usage.md", "See `docs/research/notes.md`.", False),
        ("docs/usage.md", "Planted `docs/dead.md` here.", False),
        ("docs/usage.md", "The `qa/old/` folder.", False),
        ("docs/usage.md", "Kleidos's own. Run `scripts/pio-job` here.", False),
        ("docs/usage.md", "The `kleidos-meter` CLI and `scripts/pio-job`.", False),
        ("docs/usage.md", "The Kleidos firmware repository's `scripts/pio-job`.", True),
        ("docs/usage.md", "That firmware repository's `qa/a.py` and `docs/b.md`.", True),
        ("docs/usage.md", "ESP-IDF's low-level layer (`hal/i2c_ll.h`).", True),
        ("docs/usage.md", "| Kleidos | `qa/a.py` |", False),
        ("docs/usage.md", "[`qa/a.py`](https://github.com/o/r/blob/0123abc/qa/a.py#L3)", True),
        ("docs/usage.md", "[`qa/a.py`](qa/a.py)", False),
        ("docs/firmware-design.md", "The `qa/old/` project.", True),
        ("docs/usage.md", "```text\n`qa/fenced.py`\n```", True),
    ]
    failures = 0
    for document, text, passes in cases:
        found = check_text(document, text, tree)
        if (not found) != passes:
            failures += 1
            verdict = "passed" if not found else "failed"
            print(f"selftest: {document}: {text!r} {verdict}, expected the other")
    print(f"selftest: {len(cases)} case(s), {failures} judged wrongly")
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """Check the named documents, or every tracked Markdown file."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "files", nargs="*", help="Markdown files to check (default: every tracked one)"
    )
    parser.add_argument(
        "--selftest", action="store_true", help="plant each case and check the verdicts"
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    tree = Tree.of(ROOT)
    if args.files:
        documents = [Path(name).resolve().relative_to(ROOT).as_posix() for name in args.files]
        documents = [name for name in documents if name.endswith(".md")]
    else:
        documents = sorted(name for name in tree.files if name.endswith(".md"))
    found = check(tree, ROOT, documents)
    stale = stale_records(tree)
    for finding in found:
        print(finding)
    for name in stale:
        print(f"RECORDS names {name}, which is not tracked")
    print(f"check_doc_paths: {len(documents)} document(s), {len(found)} finding(s)")
    return 1 if found or stale else 0


if __name__ == "__main__":
    sys.exit(main())
