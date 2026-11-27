#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The inventories docs/misra.md's register is held to, read from the sources.

    python3 scripts/misra_register.py           # print every inventory as the tables
    python3 scripts/misra_register.py --check   # compare them with docs/misra.md

docs/misra.md states, deviation by deviation, what the firmware departs from
and how often. A count written by hand goes stale with the next change, so
each one this script can read is read from ``firmware/src`` (comments and
literals removed) and compared, table by table, with the register by
``tests/firmware/test_misra_register.py``:

``static-locals`` (D9, rule 6.7.1)
    Every variable of static storage declared inside a function body that is
    neither ``const`` nor ``constexpr`` (the rule does not apply to those, by
    MathWorks' reading): its file, its function and its name.
``language`` (D10, rule 4.1.1)
    Every construct of the image's sources that ISO/IEC 14882:2017 does not
    have: the C++20 and later keywords, headers and library names the sources
    use, the GNU attributes (``[[gnu::...]]``, ``__attribute__``) and the
    ESP-IDF attribute macros, each with the files it is in.
``suppressions`` (the tool suppressions)
    Every ``NOLINT``, ``NOLINTNEXTLINE``, ``NOLINTBEGIN`` and
    ``cppcheck-suppress`` under ``firmware/`` and every entry of
    ``.cppcheck-suppress``, by file, marker and checks, with how many.
``wraps`` (D7)
    Every ``__wrap_*`` function the sources define, the name the linker's
    ``--wrap`` gives a replacement: D7 names each, and no other.
``counts`` (D5)
    Every call whose result is cast to ``void``, by the function called.
    (D2's counts, the formatted output the formatter rewrite took away, went
    with D2 in FW-26; ``scripts/misra_rules.py`` holds 21.10.1 and 30.0.1.)
``integer names`` (rule 6.9.2)
    Every name of a standard signed or unsigned integer type (``int``,
    ``unsigned``, ``long``, ``short``, ``signed`` and their combinations,
    ``unsigned char`` and ``signed char`` included) outside an alias
    declaration, each a difference: the firmware names ``std::int32_t``,
    ``std::uint32_t``, ``std::size_t`` and their kin instead (FW-23). The
    printf formats that needed a few (D2) went in FW-26.

What this script cannot read (the array decays of 7.11.2, the C arrays) comes
from ``scripts/ast_census.py``, which reads clang's AST.

The scanner is textual: it knows braces, parentheses, comments, string and
character literals (and the digit separator), and classifies each brace as a
namespace, a class, a function body or an initializer from the text before
it. It is held to planted cases by its tests.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "firmware" / "src"
FIRMWARE = ROOT / "firmware"
REGISTER = ROOT / "docs" / "misra.md"
CPPCHECK_SUPPRESSIONS = ROOT / ".cppcheck-suppress"


# --- Reading the code ----------------------------------------------------------


@functools.lru_cache(maxsize=256)
def code_of(text: str) -> str:
    """*text* with comments removed and literals emptied, its lines kept.

    A comment becomes spaces (its newlines kept); a string or character
    literal keeps its quotes and loses its contents, so a line's tokens stay
    where they were. An apostrophe after a digit or letter is C++14's digit
    separator, not a character literal.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif c == '"' or (c == "'" and not (i > 0 and text[i - 1].isalnum())):
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            out.append(c + " " * (min(j, n) - i - 1) + (c if j < n else ""))
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def line_of(text: str, index: int) -> int:
    """The 1-based line of *index* in *text*."""
    return text.count("\n", 0, index) + 1


def own_sources(root: Path = SOURCES) -> list[Path]:
    """The firmware's sources, ``.cpp`` and ``.h``, in name order."""
    return sorted(p for p in root.iterdir() if p.suffix in (".cpp", ".h") and p.is_file())


# --- 6.7.1: static locals --------------------------------------------------------


@dataclass(frozen=True, order=True)
class StaticLocal:
    """One local variable of static storage."""

    file: str
    function: str
    name: str
    line: int


#: The qualifiers that may follow a parameter list.
_TRAILING_QUALIFIER = re.compile(
    r"(?:\b(?:const|volatile|noexcept|override|final|mutable)|&&|&|noexcept\s*\([^()]*\))\s*$"
)
_TRAILING_RETURN = re.compile(r"->\s*[\w:<>,\s*&]+$")


def _is_signature(head: str) -> bool:
    """Whether *head*, the text before a brace, ends with a parameter list."""
    text = head.rstrip()
    stripped = _TRAILING_RETURN.sub("", text).rstrip()
    if stripped.endswith(")") or stripped != text:
        text = stripped
    while True:
        shorter = _TRAILING_QUALIFIER.sub("", text).rstrip()
        if shorter == text:
            break
        text = shorter
    return text.endswith(")")


_CTOR_INIT = re.compile(r"\)\s*(?:noexcept\b\s*(?:\([^()]*\))?\s*)?:(?!:)")
_CLASS_HEAD = re.compile(r"\b(?:class|struct|union|enum)\b")
_NAMESPACE_HEAD = re.compile(r"\bnamespace\b|\bextern\s*\"\s*\"\s*$")
_ATTRIBUTES = re.compile(
    r"\[\[.*?\]\]|\balignas\s*\([^()]*\)|__attribute__\s*\(\s*\(.*?\)\s*\)", re.S
)
_FUNCTION_NAME = re.compile(r"((?:[A-Za-z_]\w*\s*::\s*)*~?[A-Za-z_]\w*|operator\s*\S+?)\s*\($")


def _function_name(head: str) -> str:
    """The name of the function whose signature *head* is (before its body)."""
    depth = 0
    for index in range(len(head) - 1, -1, -1):
        ch = head[index]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                match = _FUNCTION_NAME.search(head[: index + 1])
                if match:
                    name = re.sub(r"\s+", "", match.group(1))
                    if name in ("if", "for", "while", "switch", "catch"):
                        return "?"
                    return name
                return "(lambda)" if head[:index].rstrip().endswith("]") else "?"
    return "?"


def _is_const_object(declaration: str) -> bool:
    """Whether the object *declaration* (after ``static``) declares is const."""
    if re.search(r"\bconstexpr\b", declaration):
        return True
    head = re.split(r"[=;{(\[]", declaration, maxsplit=1)[0]
    if "*" in head:
        return bool(re.search(r"\*\s*const\b[^*]*$", head))
    return bool(re.search(r"\bconst\b", head))


def static_locals(name: str, text: str) -> list[StaticLocal]:
    """Every non-const local of static storage in one source (*name*, *text*)."""
    code = code_of(text)
    found: list[StaticLocal] = []
    # Each scope: (kind, function name); kinds: ns, class, func, init.
    scopes: list[tuple[str, str]] = [("ns", "")]
    head_start = 0  # where the current statement's text starts
    paren = 0
    for index, ch in enumerate(code):
        kind, function = scopes[-1]
        if ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "{":
            if kind in ("func", "init") or paren:
                scopes.append(("func" if kind == "func" else "init", function))
                continue
            head = _ATTRIBUTES.sub(" ", code[head_start:index]).strip()
            if _NAMESPACE_HEAD.search(head):
                scopes.append(("ns", ""))
            elif _is_signature(head) and not re.search(r"(?<![:=!<>])=(?!=)\s*[\w:<>]*\s*\(", head):
                scopes.append(("func", _function_name(head)))
            elif (init := _CTOR_INIT.search(head)) is not None:
                # A constructor's initializer list: a brace after a member's
                # name initializes it, and the one after the last initializer
                # is the body.
                if re.search(r"[\w>]\s*$", head):
                    scopes.append(("init", function))
                else:
                    scopes.append(("func", _function_name(head[: init.start() + 1])))
            elif _CLASS_HEAD.search(head) and not head.rstrip().endswith("="):
                scopes.append(("class", ""))
            else:
                scopes.append(("init", function))
            if scopes[-1][0] != "init":
                head_start = index + 1
        elif ch == "}":
            closed = scopes.pop() if len(scopes) > 1 else scopes[0]
            if closed[0] != "init":
                head_start = index + 1
        elif ch == ";" and not paren and kind != "func":
            head_start = index + 1
        elif kind == "func" and code.startswith("static", index):
            before = code[index - 1] if index else " "
            after = code[index + 6] if index + 6 < len(code) else " "
            if (before.isalnum() or before == "_") or (after.isalnum() or after == "_"):
                continue
            end = min(
                (p for p in (code.find(";", index), code.find("{", index)) if p >= 0),
                default=len(code),
            )
            declaration = code[index + 6 : end]
            if _is_const_object(declaration):
                continue
            names = re.findall(r"([A-Za-z_]\w*)\s*(?:=|\{|\(|\[|$)", declaration.strip())
            found.append(
                StaticLocal(name, function, names[0] if names else "?", line_of(code, index))
            )
    return found


def all_static_locals(root: Path = SOURCES) -> list[StaticLocal]:
    """Every non-const static local of the firmware's sources."""
    return sorted(
        local
        for path in own_sources(root)
        for local in static_locals(path.name, path.read_text(encoding="utf-8"))
    )


# --- 4.1.1: what C++17 does not have --------------------------------------------

#: The attributes C++17 itself defines; any other is an extension.
STANDARD_ATTRIBUTES = frozenset(
    {"noreturn", "carries_dependency", "deprecated", "fallthrough", "nodiscard", "maybe_unused"}
)

#: Keywords and tokens of C++20 and later.
LATER_KEYWORDS = {
    "consteval": r"\bconsteval\b",
    "constinit": r"\bconstinit\b",
    "concept": r"\bconcept\b",
    "requires": r"\brequires\b",
    "co_await": r"\bco_await\b",
    "co_yield": r"\bco_yield\b",
    "co_return": r"\bco_return\b",
    "char8_t": r"\bchar8_t\b",
    "<=>": r"<=>",
    "using enum": r"\busing\s+enum\b",
    "if consteval": r"\bif\s+consteval\b",
    "[[likely]]": r"\[\[\s*likely\s*\]\]",
    "[[unlikely]]": r"\[\[\s*unlikely\s*\]\]",
    "[[no_unique_address]]": r"\[\[\s*no_unique_address\s*\]\]",
    "[[assume]]": r"\[\[\s*assume\b",
    "__VA_OPT__": r"\b__VA_OPT__\b",
}

#: Standard headers that C++17 does not have.
LATER_HEADERS = frozenset(
    {
        "barrier", "bit", "compare", "concepts", "coroutine", "expected",
        "flat_map", "flat_set", "format", "generator", "latch", "mdspan", "numbers", "print",
        "ranges", "semaphore", "source_location", "span", "spanstream", "stacktrace",
        "stdfloat", "stop_token", "syncstream", "version",
    }
)  # fmt: skip

#: Standard library names of C++20 and later (``std::`` qualified).
LATER_LIBRARY = frozenset(
    {
        "bit_cast", "popcount", "countl_zero", "countr_zero", "countl_one", "countr_one",
        "bit_width", "bit_ceil", "bit_floor", "has_single_bit", "rotl", "rotr", "byteswap",
        "endian", "span", "dynamic_extent", "to_array", "ssize", "midpoint", "lerp",
        "in_range", "cmp_equal", "cmp_not_equal", "cmp_less", "cmp_greater",
        "cmp_less_equal", "cmp_greater_equal", "is_constant_evaluated", "to_underlying",
        "unreachable", "remove_cvref", "remove_cvref_t", "type_identity", "type_identity_t",
        "erase_if", "ranges", "views", "jthread", "atomic_ref", "source_location",
        "assume_aligned", "bind_front", "identity", "format", "expected", "unexpected",
        "u8string", "starts_with", "ends_with",
    }
)  # fmt: skip

#: Member functions of C++20 and later the sources may call.
LATER_MEMBERS = {
    ".starts_with()": r"\.\s*starts_with\s*\(",
    ".ends_with()": r"\.\s*ends_with\s*\(",
    ".contains()": r"\.\s*contains\s*\(",
}

#: GNU and other compiler extensions, by the token that shows each.
EXTENSIONS = {
    "__builtin_*": r"\b__builtin_\w+",
    "asm": r"\b(?:__asm__|asm)\b",
    "__extension__": r"\b__extension__\b",
    "typeof": r"\b(?:__typeof__|typeof)\b",
    "__restrict": r"\b__restrict\w*\b",
    "__int128": r"\b__int128\b",
    "statement expression": r"\(\s*\{",
    "case range": r"\bcase\b[^:;]*\.\.\.",
}


def language_features(text: str) -> set[str]:
    """The constructs of one source that ISO/IEC 14882:2017 does not have."""
    code = code_of(text)
    found: set[str] = set()
    for attribute in re.findall(r"\[\[\s*([\w:]+)", code):
        if attribute not in STANDARD_ATTRIBUTES and not attribute.startswith("using"):
            found.add(f"[[{attribute}]]")
    for attribute in re.findall(r"__attribute__\s*\(\s*\(\s*(\w+)", code):
        found.add(f"__attribute__(({attribute}))")
    for macro in re.findall(r"\b([A-Z][A-Z0-9_]*_ATTR)\b", code):
        found.add(macro)
    for name, pattern in LATER_KEYWORDS.items():
        if re.search(pattern, code):
            found.add(name)
    for header in re.findall(r"^\s*#\s*include\s*<([\w/]+)>", text, flags=re.M):
        if header in LATER_HEADERS:
            found.add(f"<{header}>")
    for name in re.findall(r"\bstd::(\w+)", code):
        if name in LATER_LIBRARY:
            found.add(f"std::{name}")
    for name, pattern in {**LATER_MEMBERS, **EXTENSIONS}.items():
        if re.search(pattern, code):
            found.add(name)
    return found


def all_language_features(root: Path = SOURCES) -> dict[str, list[str]]:
    """Each construct C++17 does not have, with the sources that use it."""
    where: dict[str, list[str]] = {}
    for path in own_sources(root):
        for feature in language_features(path.read_text(encoding="utf-8")):
            where.setdefault(feature, []).append(path.name)
    return {feature: sorted(files) for feature, files in sorted(where.items())}


# --- Tool suppressions -----------------------------------------------------------


@dataclass(frozen=True, order=True)
class Suppression:
    """One kind of suppression in one file: where, the marker and what it names."""

    file: str
    marker: str
    checks: str


_NOLINT = re.compile(r"\b(NOLINTNEXTLINE|NOLINTBEGIN|NOLINTEND|NOLINT)\b(?:\(([^)]*)\))?")
_CPPCHECK_INLINE = re.compile(r"cppcheck-suppress(?:-begin|-end|-file)?\s+([\w\[\]-]+)")


def suppressions_in(name: str, text: str) -> Counter[Suppression]:
    """The suppressions in the comments of one source."""
    found: Counter[Suppression] = Counter()
    for line in text.splitlines():
        comment = line[line.find("//") :] if "//" in line else ""
        if not comment:
            continue
        for marker, checks in _NOLINT.findall(comment):
            if marker == "NOLINTEND":
                continue
            found[Suppression(name, marker, (checks or "*").replace(" ", ""))] += 1
        for check in _CPPCHECK_INLINE.findall(comment):
            found[Suppression(name, "cppcheck-suppress", check)] += 1
    return found


def cppcheck_list(path: Path = CPPCHECK_SUPPRESSIONS) -> Counter[Suppression]:
    """The entries of the cppcheck suppressions list."""
    found: Counter[Suppression] = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            found[Suppression(path.name, "list", entry)] += 1
    return found


def all_suppressions(root: Path = ROOT) -> Counter[Suppression]:
    """Every suppression of the firmware's sources, its host tests and the cppcheck list."""
    found: Counter[Suppression] = Counter()
    firmware = root / "firmware"
    for folder in (firmware / "src", firmware / "test" / "native"):
        for directory, subdirectories, names in os.walk(folder):
            subdirectories[:] = sorted(d for d in subdirectories if d != "build")
            for name in sorted(names):
                path = Path(directory, name)
                if path.suffix in (".cpp", ".h"):
                    found.update(
                        suppressions_in(
                            str(path.relative_to(root)), path.read_text(encoding="utf-8")
                        )
                    )
    found.update(cppcheck_list(root / ".cppcheck-suppress"))
    return found


# --- D5: the counts ---------------------------------------------------------------

_VOID_CALL = re.compile(
    r"\(\s*void\s*\)\s*((?:[A-Za-z_]\w*\s*(?:::|\.|->)\s*)*[A-Za-z_]\w*)\s*(?:<[^<>;]*>\s*)?\("
)


_STATIC_TASK = re.compile(r"\bxTaskCreateStaticPinnedToCore\s*\(")


def component_sources(root: Path = SOURCES) -> list[str]:
    """The ``SRCS`` of the firmware's component (``firmware/src/CMakeLists.txt``)."""
    component = root / "CMakeLists.txt"
    if not component.is_file():
        return []
    block = re.search(
        r"SRCS\s+(.*?)\n\s*INCLUDE_DIRS", component.read_text(encoding="utf-8"), re.DOTALL
    )
    return sorted(re.findall(r'"([^"]+\.cpp)"', block.group(1))) if block else []


def counts(root: Path = SOURCES) -> dict[str, int]:
    """The textual counts docs/misra.md states, by key.

    ``d5.void_casts`` (D5), ``src.sources``, the translation units every
    compile-flag and clang-tidy leg reads, and ``src.static_tasks``, the
    tasks the firmware creates over its own static memory (21.6.1).
    """
    found: Counter[str] = Counter()
    for path in own_sources(root):
        code = code_of(path.read_text(encoding="utf-8"))
        found["d5.void_casts"] += len(_VOID_CALL.findall(code))
        found["src.static_tasks"] += len(_STATIC_TASK.findall(code))
    found["src.sources"] = len(component_sources(root))
    return dict(sorted(found.items()))


def callee_of(chain: str) -> str:
    """How D5 names the function a cast call calls.

    A qualified name keeps its qualifier (``std::copy_n``,
    ``watchdog::begin``); a member called on an object is written with a
    leading dot whatever the object (``.fetch_add``); a plain call by its name.
    """
    chain = re.sub(r"\s+", "", chain)
    member = re.split(r"\.|->", chain)
    if len(member) > 1:
        return "." + member[-1].rsplit("::", 1)[-1]
    return chain


def void_casts(root: Path = SOURCES) -> dict[str, int]:
    """Every call whose result is cast to ``void``, counted by the function called."""
    found: Counter[str] = Counter()
    for path in own_sources(root):
        for chain in _VOID_CALL.findall(code_of(path.read_text(encoding="utf-8"))):
            found[callee_of(chain)] += 1
    return dict(sorted(found.items()))


# --- 6.9.2: the standard integer type names ------------------------------------------

#: A spelling of a standard signed or unsigned integer type (6.9.2): one or
#: more of the keywords, ``unsigned char`` and ``signed char`` included, plain
#: ``char`` (a character type, 7.0.3) not.
_INTEGER_NAME = re.compile(
    r"\b(?:(?:unsigned|signed|short|long|int)\b\s*)+(?:\bchar\b)?|\b(?:unsigned|signed)\s+char\b"
)


@dataclass(frozen=True, order=True)
class IntegerName:
    """One spelling of a standard integer type's name."""

    file: str
    line: int
    spelled: str


def integer_names_in(name: str, text: str) -> list[IntegerName]:
    """Every standard integer type name of one source (6.9.2).

    An alias declaration (``using``, ``typedef``), which the rule exempts, is
    not counted.
    """
    code = code_of(text)
    found = []
    for match in _INTEGER_NAME.finditer(code):
        start = match.start()
        statement = code.rfind(";", 0, start) + 1
        head = code[max(statement, code.rfind("{", 0, start) + 1) : start]
        if re.search(r"\busing\s+\w+\s*=|\btypedef\b", head):
            continue
        found.append(IntegerName(name, line_of(code, start), " ".join(match.group(0).split())))
    return found


def all_integer_names(root: Path = SOURCES) -> list[IntegerName]:
    """Every standard integer type name of ``firmware/src``, by file and line."""
    found: list[IntegerName] = []
    for path in own_sources(root):
        found.extend(integer_names_in(path.name, path.read_text(encoding="utf-8")))
    return sorted(found)


# --- The register's tables ----------------------------------------------------------


def table_rows(text: str, heading: str) -> list[list[str]]:
    """The body rows of the first table after the heading line *heading* in *text*."""
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        raise KeyError(f"docs/misra.md has no heading {heading!r}") from None
    rows: list[list[str]] = []
    in_table = False
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        if line.startswith("|"):
            in_table = True
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
                continue
            rows.append(cells)
        elif in_table:
            break
    return rows[1:]  # the header row


def unquote(cell: str) -> str:
    """A table cell without its backticks."""
    return cell.replace("`", "").strip()


#: The headings of the register's generated tables.
HEADINGS = {
    "static-locals": "#### The static locals",
    "language": "#### What C++17 does not have",
    "suppressions": "## Tool suppressions",
    "counts": "## The counts",
    "void-casts": "#### The discarded results, by the function called",
}


def register_static_locals(text: str) -> list[tuple[str, str, str]]:
    """D9's rows: (file, function, object)."""
    return sorted(
        (unquote(r[0]), unquote(r[1]), unquote(r[2]))
        for r in table_rows(text, HEADINGS["static-locals"])
    )


def register_language(text: str) -> dict[str, list[str]]:
    """D10's rows: each construct and its files."""
    return {
        unquote(r[0]): sorted(unquote(f) for f in r[2].split(","))
        for r in table_rows(text, HEADINGS["language"])
    }


def register_suppressions(text: str) -> dict[Suppression, tuple[int, str]]:
    """The suppressions table: each suppression, its count and its reason."""
    return {
        Suppression(unquote(r[0]), unquote(r[1]), unquote(r[2])): (int(r[3]), r[4])
        for r in table_rows(text, HEADINGS["suppressions"])
    }


def register_counts(text: str) -> dict[str, int]:
    """The counts table: key to count."""
    return {unquote(r[0]): int(r[2].replace(",", "")) for r in table_rows(text, HEADINGS["counts"])}


def register_void_casts(text: str) -> dict[str, tuple[int, str]]:
    """D5's table: each function called, its count and the reason."""
    return {
        unquote(r[0]).removesuffix("()"): (int(r[1]), r[2])
        for r in table_rows(text, HEADINGS["void-casts"])
    }


#: A function definition whose name the linker's ``--wrap`` gives a
#: replacement: the name, its parameter list, an optional ``noexcept``, a body.
WRAP_DEFINITION = re.compile(r"\b(__wrap_\w+)\s*\([^;{}]*\)\s*(?:noexcept\s*)?\{")


def wraps_in(text: str) -> set[str]:
    """The ``__wrap_*`` functions *text* defines (a declaration or comment is none)."""
    return set(WRAP_DEFINITION.findall(code_of(text)))


def wrap_definitions(root: Path = SOURCES) -> dict[str, str]:
    """Every ``__wrap_*`` function the sources define, and its file."""
    found: dict[str, str] = {}
    for path in own_sources(root):
        for name in wraps_in(path.read_text(encoding="utf-8")):
            found[name] = path.name
    return found


def register_wraps(text: str) -> set[str]:
    """The ``__wrap_*`` functions D7's section names."""
    start = text.index("### D7.")
    end = text.index("\n### ", start + 1)
    return set(re.findall(r"`(__wrap_\w+)\(\)`", text[start:end]))


def differences(register: str = "", root: Path = ROOT) -> list[str]:
    """What the register says that the sources do not, and the reverse."""
    text = register or REGISTER.read_text(encoding="utf-8")
    sources = root / "firmware" / "src"
    out: list[str] = []

    found_locals = sorted({(s.file, s.function, s.name) for s in all_static_locals(sources)})
    listed_locals = register_static_locals(text)
    out.extend(
        f"static local {item} is not in D9"
        for item in sorted(set(found_locals) - set(listed_locals))
    )
    out.extend(
        f"D9 lists static local {item}, which the sources no longer declare"
        for item in sorted(set(listed_locals) - set(found_locals))
    )

    found_language = all_language_features(sources)
    listed_language = register_language(text)
    for feature in sorted(set(found_language) | set(listed_language)):
        if feature not in listed_language:
            out.append(f"{feature} ({', '.join(found_language[feature])}) is not in D10")
        elif feature not in found_language:
            out.append(f"D10 lists {feature}, which no source uses any more")
        elif found_language[feature] != listed_language[feature]:
            out.append(
                f"D10 gives {feature} in {', '.join(listed_language[feature])}; "
                f"the sources: {', '.join(found_language[feature])}"
            )

    found_suppressions = all_suppressions(root)
    listed_suppressions = register_suppressions(text)
    for item in sorted(set(found_suppressions) | set(listed_suppressions)):
        if item not in listed_suppressions:
            out.append(f"suppression {item} has no row in 'Tool suppressions'")
        elif item not in found_suppressions:
            out.append(f"'Tool suppressions' lists {item}, which is no longer there")
        elif listed_suppressions[item][0] != found_suppressions[item]:
            out.append(
                f"'Tool suppressions' counts {item} {listed_suppressions[item][0]} times; "
                f"there are {found_suppressions[item]}"
            )
        elif not listed_suppressions[item][1].strip():
            out.append(f"suppression {item} has a row with no reason")

    found_counts = counts(sources)
    listed_counts = register_counts(text)
    for key, value in found_counts.items():
        if listed_counts.get(key) != value:
            out.append(
                f"count {key}: the register says {listed_counts.get(key)}, the sources {value}"
            )

    out.extend(
        f"{name.file}:{name.line}: {name.spelled}, a standard integer type's name"
        " (6.9.2): use a fixed-width type (std::int32_t, std::uint32_t, std::size_t)"
        for name in all_integer_names(sources)
    )

    found_wraps = wrap_definitions(sources)
    listed_wraps = register_wraps(text)
    out.extend(
        f"{name}() ({found_wraps[name]}) is a linker wrap D7 does not name"
        for name in sorted(set(found_wraps) - listed_wraps)
    )
    out.extend(
        f"D7 names {name}(), which no source defines"
        for name in sorted(listed_wraps - set(found_wraps))
    )

    found_casts = void_casts(sources)
    listed_casts = register_void_casts(text)
    for callee in sorted(set(found_casts) | set(listed_casts)):
        if callee not in listed_casts:
            out.append(f"(void) {callee}() ({found_casts[callee]}) has no row in D5")
        elif callee not in found_casts:
            out.append(f"D5 lists (void) {callee}(), which no source casts any more")
        elif listed_casts[callee][0] != found_casts[callee]:
            out.append(
                f"D5 counts (void) {callee}() {listed_casts[callee][0]} times; "
                f"the sources {found_casts[callee]}"
            )
        elif not listed_casts[callee][1].strip():
            out.append(f"(void) {callee}() has a row in D5 with no reason")
    return out


# --- Printing ------------------------------------------------------------------------


def print_tables() -> None:
    """Print every inventory as the register's tables (the reasons left empty)."""
    print(HEADINGS["static-locals"])
    print("| File | Function | Object | Why |\n|---|---|---|---|")
    for s in all_static_locals():
        print(f"| `{s.file}` | `{s.function}` | `{s.name}` |  |")
    print("\n" + HEADINGS["language"])
    print("| Construct | Since | Files |\n|---|---|---|")
    for feature, files in all_language_features().items():
        print(f"| `{feature}` |  | {', '.join(f'`{f}`' for f in files)} |")
    print("\n" + HEADINGS["suppressions"])
    print("| File | Marker | Checks | Count | Why |\n|---|---|---|---:|---|")
    for item, count in sorted(all_suppressions().items()):
        print(f"| `{item.file}` | `{item.marker}` | `{item.checks}` | {count} |  |")
    print("\n" + HEADINGS["counts"])
    print("| Key | What | Count |\n|---|---|---:|")
    for key, value in counts().items():
        print(f"| `{key}` |  | {value} |")
    print("\n" + HEADINGS["void-casts"])
    print("| Function | Count | Why |\n|---|---:|---|")
    for callee, count in void_casts().items():
        print(f"| `{callee}()` | {count} |  |")


def main(argv: list[str] | None = None) -> int:
    """Print the inventories, or compare them with the register."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare with docs/misra.md")
    args = parser.parse_args(argv)
    if not args.check:
        print_tables()
        return 0
    problems = differences()
    for problem in problems:
        print(f"FAIL: {problem}")
    print(f"docs/misra.md against the sources: {len(problems)} difference(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
