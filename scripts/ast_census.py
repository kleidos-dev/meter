#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""What the image's translation units say together, read from clang's AST.

    python3 scripts/ast_census.py                 # the unreferenced-function gate
    python3 scripts/ast_census.py --decays        # also print the 7.11.2 decays
    python3 scripts/ast_census.py --json          # everything, for the tests
    python3 scripts/ast_census.py --leg device    # as the image compiles them

Every translation unit of ``firmware/src/CMakeLists.txt`` (the units the link
joins into the image) is parsed by clang (``-fsyntax-only``) and its AST dumped
as JSON, limited to the declarations of ``kleidos::meter``. The units are read
together, as the linker sees them, which a per-file analyzer cannot do: cppcheck's
``unusedFunction`` reads one file at a time and cannot follow the console's
table of member-function pointers, which is why it was suppressed until FW-20.

**Unreferenced functions** (the gate). Clang marks a declaration *used* when a
unit odr-uses it (a call, an address taken, a pointer to member in a table, a
call in a constant expression or a ``static_assert``) and *referenced* when it
names it at all. A function, member function, constructor or conversion
declared in ``firmware/src`` that no unit of the image marks either way is
unreferenced: nothing the image is built from uses it, whatever the host tests
call. The marks are clang's, so inlining hides nothing (a link-map or DWARF
reading of the optimized image loses the small functions GCC inlines without a
trace). Left out: what the compiler declares (implicit members), what is
deleted or defaulted, and the C-linkage entry points, which are outside
``kleidos::meter`` and are named by ESP-IDF or the linker. :data:`TEST_SEAMS`
names the functions only the host tests call, each with its reason; an entry
the image references, or no host test calls, fails the gate as stale.

Blind spots: a function used only by another unreferenced function counts as
used until that one is removed (the gate is run again after each removal), and
the device leg and the harness leg each read one configuration (the census
build's ``METER_HEAP_CENSUS`` is not among them).

**Array-to-pointer decays** (MISRA C++:2023 7.11.2, "An array passed as a
function argument shall not decay to a pointer"): every implicit
array-to-pointer conversion of a call's or a construction's argument whose
source is in ``firmware/src``, counted once per place in the source however
many units include it. String literals are counted apart, because MathWorks'
reading of the rule exempts a string literal passed to a ``char`` pointer
(to confirm against the owner's copy), and a ``std::va_list`` is left out,
because it is an array on the host (x86-64, arm64 macOS) and a struct on the
Xtensa target, so its decay is the host's. Both legs count the same.
docs/misra.md's D3 holds the count,
and ``tests/firmware/test_ast_census.py`` holds the two equal and the count to
a history that only falls.

**C arrays declared** (11.3.1): every variable, member or parameter of array
type declared in ``firmware/src``, once per place; D3 holds that count too.

**Lambdas** (8.1.2): every lambda expression written in ``firmware/src``, once
per place; the register's 8.1.2 entry names each with what it captures, and
its count is held under "The counts".

The clang binary is ``--clang``, else ``$METER_CLANG``, else the first
``clang++`` among ``$LLVM_BIN``, Homebrew's LLVM and the PATH. Exit status:
0 nothing unreferenced, 1 a finding, 2 the analysis could not run (a unit that
did not parse, no clang).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "firmware" / "src"
TESTS = ROOT / "firmware" / "test" / "native"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import clang_tidy_gate  # noqa: E402  (the leg flags and the CMake source list)

#: The exit code of a run that could not be trusted.
GUARD = 2

#: The exit code of a run that found an unreferenced function.
FINDINGS = 1

#: The dump is limited to what is declared inside the instrument's namespace.
DUMP_FILTER = "kleidos::meter::"

#: The kinds of declaration the gate weighs.
FUNCTION_KINDS = frozenset(
    {
        "FunctionDecl",
        "CXXMethodDecl",
        "CXXConstructorDecl",
        "CXXDestructorDecl",
        "CXXConversionDecl",
    }
)

#: The expressions whose arguments a decay is counted in.
CALL_KINDS = frozenset(
    {
        "CallExpr",
        "CXXMemberCallExpr",
        "CXXOperatorCallExpr",
        "CXXConstructExpr",
        "CXXTemporaryObjectExpr",
    }
)

#: Of those, the ones whose first child is the callee, not an argument.
CALLEE_FIRST = frozenset({"CallExpr", "CXXMemberCallExpr", "CXXOperatorCallExpr"})

#: What may stand between an argument and its call: implicit conversions
#: and parentheses.
WRAPPERS = frozenset({"ImplicitCastExpr", "ParenExpr"})

#: The declarations whose array type the 11.3.1 count reads.
ARRAY_DECL_KINDS = frozenset({"VarDecl", "FieldDecl", "ParmVarDecl"})

#: Functions only the host tests call, by qualified name, each with why the
#: image keeps them. An entry that names one overload of several adds its
#: parameter types as clang spells them, ``name(type, type)``. The gate fails
#: an entry the image references, or that no file of ``firmware/test/native``
#: names.
TEST_SEAMS: dict[str, str] = {
    "kleidos::meter::heap::disarm": (
        "returns the heap monitor to its state before arm(), so each host test "
        "starts from zero counts; the image arms it once and never disarms"
    ),
    "kleidos::meter::heap::attachCensus": (
        "gives the census a ring of the host test's own, so the census logic is "
        "tested without the census build; the census build's ring is static"
    ),
    "kleidos::meter::i2c_bus::clockHz": (
        "the clock the bus state holds, which the host tests compare with what the "
        "fake timing registers were given; the console reports the clock it asked "
        "for (Ina228Driver)"
    ),
    "kleidos::meter::i2c_bus::clearBus": (
        "runs the recovery a transfer runs on a time-out or a held bus, so the host "
        "tests reach it, and its refusals, without staging a fault"
    ),
    "kleidos::meter::scpi::Parameters::choice"
    "(std::size_t, const char *const *, std::size_t, std::size_t &)": (
        "the keyword match over an array of C strings, null and all, which the parser's "
        "host tests were written against; the image's handlers keep their keywords as "
        "views and call the std::span overload (FW-22)"
    ),
}


@dataclass(frozen=True, order=True)
class Place:
    """A place in a source file: the file relative to the repository, and its line."""

    file: str
    line: int


@dataclass
class FunctionInfo:
    """What the units say of one function, merged over every declaration of it."""

    name: str
    signature: str = ""
    places: set[Place] = field(default_factory=set)
    referenced: bool = False
    excluded: bool = False


@dataclass
class Census:
    """The merged reading of every unit."""

    functions: dict[str, FunctionInfo] = field(default_factory=dict)
    decays: dict[tuple[str, int, int], Place] = field(default_factory=dict)
    literal_decays: dict[tuple[str, int, int], Place] = field(default_factory=dict)
    arrays: dict[tuple[str, int], Place] = field(default_factory=dict)
    lambdas: dict[tuple[str, int], Place] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    def unreferenced(self) -> list[FunctionInfo]:
        """The functions no unit references, outside :data:`TEST_SEAMS`, by place."""
        return sorted(
            (
                f
                for f in self.functions.values()
                if not f.referenced
                and not f.excluded
                and f.name not in TEST_SEAMS
                and f.signature not in TEST_SEAMS
            ),
            key=lambda f: (min(f.places).file, min(f.places).line, f.name),
        )


# --- Reading one unit's dump ----------------------------------------------------


def json_documents(text: str) -> Iterator[dict[str, Any]]:
    """The JSON objects of a filtered dump, which clang writes one after the other."""
    decoder = json.JSONDecoder()
    index = 0
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            return
        document, index = decoder.raw_decode(text, index)
        yield document


class _Reader:
    """Walks one dump in the order clang wrote it, so each location knows its file.

    Clang's JSON names a location's file only where it differs from the last
    location it wrote, so the file of a node is the last one named before it,
    in document order.
    """

    def __init__(self, own: Path, census: Census, base: Path) -> None:
        self.own = own.resolve()
        self.base = base
        self.census = census
        self.file: str | None = None
        self.line = 0
        self._paths: dict[str, tuple[str, bool]] = {}
        self._stack: list[tuple[dict[str, Any], int]] = []

    def _resolve(self) -> tuple[str, bool] | None:
        """The current file relative to the repository, and whether it is own code."""
        if self.file is None:
            return None
        cached = self._paths.get(self.file)
        if cached is None:
            path = Path(self.file)
            if not path.is_absolute():
                path = self.base / path
            path = path.resolve()
            try:
                shown = str(path.relative_to(ROOT.resolve()))
            except ValueError:
                shown = str(path)
            cached = (shown, path.is_relative_to(self.own))
            self._paths[self.file] = cached
        return cached

    def _relative(self) -> str | None:
        resolved = self._resolve()
        return None if resolved is None else resolved[0]

    def _is_own(self) -> bool:
        resolved = self._resolve()
        return resolved is not None and resolved[1]

    def location(self, value: object) -> None:
        """Track the file and line of every location inside *value*, in order."""
        if isinstance(value, dict):
            if "offset" in value:
                if "file" in value:
                    self.file = value["file"]
                if "line" in value:
                    self.line = value["line"]
            for key, inner in value.items():
                if key not in ("includedFrom", "inner"):
                    self.location(inner)
        elif isinstance(value, list):
            for inner in value:
                self.location(inner)

    def _span(self, node: dict[str, Any]) -> tuple[int, int]:
        def offset(point: dict[str, Any]) -> int:
            if "expansionLoc" in point:
                point = point["expansionLoc"]
            return int(point.get("offset", -1))

        span = node.get("range", {})
        return offset(span.get("begin", {})), offset(span.get("end", {}))

    def walk(
        self, node: dict[str, Any], scope: list[str], parent: dict[str, Any] | None, index: int
    ) -> None:
        """Read *node* and its children; *scope* is the qualified name so far."""
        self.location(node.get("loc"))
        decl_file_own = self._is_own()
        decl_place = Place(self._relative() or "?", self.line)
        begin_own = None
        if "range" in node:
            span = node["range"]
            self.location(span.get("begin"))
            begin_own = (self._is_own(), Place(self._relative() or "?", self.line))
            self.location(span.get("end"))
        for key, value in node.items():
            if key not in ("loc", "range", "inner"):
                self.location(value)

        kind = node.get("kind", "")
        if kind in FUNCTION_KINDS and decl_file_own:
            self._function(node, scope, decl_place, parent)
        elif kind in ARRAY_DECL_KINDS and decl_file_own:
            if "[" in node.get("type", {}).get("qualType", "") and not node.get("isImplicit"):
                self.census.arrays[(decl_place.file, self._span(node)[0])] = decl_place
        elif kind == "LambdaExpr" and begin_own is not None and begin_own[0]:
            self.census.lambdas[(begin_own[1].file, self._span(node)[0])] = begin_own[1]
        elif (
            kind == "ImplicitCastExpr"
            and node.get("castKind") == "ArrayToPointerDecay"
            and self._is_argument(index)
            and begin_own is not None
            and begin_own[0]
        ):
            decay_key = (begin_own[1].file, *self._span(node))
            inner = node.get("inner", [{}])
            source_type = (inner[0] if inner else {}).get("type", {})
            if "va_list" in json.dumps(source_type):
                # A va_list is an array on some hosts (x86-64, arm64 macOS)
                # and a struct on the Xtensa target: its decay is the host's.
                return self._children(node, scope, index)
            target = (
                self.census.literal_decays
                if inner and inner[0].get("kind") == "StringLiteral"
                else self.census.decays
            )
            target[decay_key] = begin_own[1]

        self._children(node, scope, index)
        return None

    def _children(self, node: dict[str, Any], scope: list[str], index: int) -> None:
        kind = node.get("kind", "")
        name = node.get("name")
        inner_scope = scope
        if kind in ("NamespaceDecl", "CXXRecordDecl", "ClassTemplateDecl") and name:
            inner_scope = [*scope, name]
        elif kind == "NamespaceDecl":
            inner_scope = [*scope, "(anonymous namespace)"]
        self._stack.append((node, index))
        for position, child in enumerate(node.get("inner", [])):
            self.walk(child, inner_scope, node, position)
        self._stack.pop()

    def _is_argument(self, index: int) -> bool:
        """Whether the node being read, at *index* in its parent, is a call's argument.

        The implicit conversions and parentheses around it are climbed first:
        a ``char`` buffer passed to a ``const char*`` parameter decays, then
        takes the ``const`` in a conversion of its own.
        """
        position = index
        depth = len(self._stack) - 1
        while depth >= 0 and self._stack[depth][0].get("kind") in WRAPPERS:
            position = self._stack[depth][1]
            depth -= 1
        if depth < 0:
            return False
        call = self._stack[depth][0].get("kind")
        return call in CALL_KINDS and (position >= 1 or call not in CALLEE_FIRST)

    def _function(
        self, node: dict[str, Any], scope: list[str], place: Place, parent: dict[str, Any] | None
    ) -> None:
        name = "::".join([*scope, node.get("name", "")])
        if parent is not None and parent.get("kind") == "FunctionTemplateDecl":
            # A template and its specializations are one function: it is
            # referenced when any specialization is.
            key = f"template {name}"
        else:
            key = node.get("mangledName") or name + node.get("type", {}).get("qualType", "")
        info = self.census.functions.setdefault(
            key, FunctionInfo(name, name + parameters_of(node.get("type", {}).get("qualType", "")))
        )
        info.places.add(place)
        if node.get("isUsed") or node.get("isReferenced"):
            info.referenced = True
        if (
            node.get("isImplicit")
            or node.get("explicitlyDeleted")
            or node.get("explicitlyDefaulted")
        ):
            info.excluded = True


def parameters_of(function_type: str) -> str:
    """The parameter list of a function type as clang spells it, parentheses included.

    ``bool (std::size_t, const char *const *) noexcept`` gives
    ``(std::size_t, const char *const *)``; a type with no list gives ``""``.
    """
    start = function_type.find("(")
    if start < 0:
        return ""
    depth = 0
    for index in range(start, len(function_type)):
        if function_type[index] == "(":
            depth += 1
        elif function_type[index] == ")":
            depth -= 1
            if depth == 0:
                return function_type[start : index + 1]
    return ""


def read_dump(text: str, census: Census, own: Path = SOURCES, base: Path = ROOT) -> None:
    """Merge one unit's filtered JSON dump into *census*.

    :param text: clang's ``-ast-dump=json`` output.
    :param census: the reading so far.
    :param own: the directory whose declarations count.
    :param base: the directory relative file names in the dump are relative to.
    """
    reader = _Reader(own, census, base)
    for document in json_documents(text):
        reader.walk(document, ["kleidos", "meter"], None, 0)


# --- Running clang ---------------------------------------------------------------


def find_clang(requested: str | None = None) -> str | None:
    """The clang++ to run: *requested*, ``$METER_CLANG``, ``$LLVM_BIN``, Homebrew's, the PATH's."""
    candidates = [
        requested,
        os.environ.get("METER_CLANG"),
        str(Path(os.environ["LLVM_BIN"]) / "clang++") if os.environ.get("LLVM_BIN") else None,
        "/opt/homebrew/opt/llvm/bin/clang++",
        "/usr/local/opt/llvm/bin/clang++",
        "clang++",
    ]
    for candidate in candidates:
        if candidate and shutil.which(candidate):
            return shutil.which(candidate)
    return None


def dump_command(clang: str, source: Path, flags: Sequence[str]) -> list[str]:
    """The clang command that dumps *source*'s declarations of :data:`DUMP_FILTER`."""
    return [
        clang,
        *flags,
        "-fsyntax-only",
        "-Wno-everything",
        "-Xclang",
        "-ast-dump=json",
        "-Xclang",
        f"-ast-dump-filter={DUMP_FILTER}",
        str(source),
    ]


def run_units(
    clang: str,
    units: Mapping[Path, Sequence[str]],
    own: Path = SOURCES,
    jobs: int | None = None,
) -> Census:
    """Dump every unit of *units* (source to flags) and merge what they say."""
    census = Census()

    def one(item: tuple[Path, Sequence[str]]) -> tuple[Path, subprocess.CompletedProcess[str]]:
        source, flags = item
        return source, subprocess.run(
            dump_command(clang, source, flags),
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=max(1, jobs or os.cpu_count() or 4)) as pool:
        results = list(pool.map(one, units.items()))
    for source, result in results:
        if result.returncode != 0 or "error:" in result.stderr:
            last = (result.stderr.strip().splitlines() or [f"exit {result.returncode}"])[-1]
            census.failures.append(f"{source.name}: clang did not parse it: {last}")
            continue
        read_dump(result.stdout, census, own)
    return census


def image_units(leg: str) -> dict[Path, list[str]]:
    """The image's translation units and the flags of *leg* (``harness`` or ``device``)."""
    sources = clang_tidy_gate.cmake_sources()
    flags = (
        clang_tidy_gate.device_flags(sources)
        if leg == "device"
        else clang_tidy_gate.harness_flags(sources)
    )
    return {SOURCES / source: flags[source] for source in sources}


def seam_problems(census: Census, tests: Path = TESTS) -> list[str]:
    """The :data:`TEST_SEAMS` entries that are no longer what they claim."""
    problems = []
    names: dict[str, FunctionInfo] = {}
    for f in census.functions.values():
        for key in (f.name, f.signature):
            known = names.get(key)
            if known is None or (f.referenced and not known.referenced):
                names[key] = f
    test_text = "\n".join(
        p.read_text(encoding="utf-8")
        for p in sorted(tests.rglob("*"))
        if p.suffix in (".cpp", ".h") and p.is_file()
    )
    for seam in TEST_SEAMS:
        info = names.get(seam)
        if info is None:
            problems.append(
                f"{seam}: listed as a test seam, but the sources declare no such function"
            )
            continue
        if info.referenced:
            problems.append(f"{seam}: listed as a test seam, but the image references it")
        short = seam.split("(", 1)[0].rsplit("::", 1)[-1]
        if not re.search(rf"\b{re.escape(short)}\s*\(", test_text):
            problems.append(f"{seam}: listed as a test seam, but no host test calls it")
    return problems


def as_json(census: Census) -> dict[str, Any]:
    """The census as plain data (for the tests and ``--json``)."""
    return {
        "unreferenced": [
            {"name": f.name, "places": sorted(f"{p.file}:{p.line}" for p in f.places)}
            for f in census.unreferenced()
        ],
        "decays": len(census.decays),
        "literal_decays": len(census.literal_decays),
        "arrays": len(census.arrays),
        "lambdas": len(census.lambdas),
        "decay_sites": sorted(f"{p.file}:{p.line}" for p in census.decays.values()),
        "functions": len(census.functions),
        "failures": census.failures,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the census and return the gate's exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--leg", choices=("harness", "device"), default="harness")
    parser.add_argument("--clang", help="the clang++ binary")
    parser.add_argument("--decays", action="store_true", help="print the 7.11.2 decay sites")
    parser.add_argument("--json", action="store_true", help="print the census as JSON")
    args = parser.parse_args(argv)
    clang = find_clang(args.clang)
    if clang is None:
        print(
            "GATE FAILURE (guard): no clang++ found (--clang, METER_CLANG, LLVM_BIN)",
            file=sys.stderr,
        )
        return GUARD
    try:
        units = image_units(args.leg)
    except clang_tidy_gate.GuardFailure as failure:
        print(f"GATE FAILURE (guard): {failure}", file=sys.stderr)
        return GUARD
    census = run_units(clang, units)
    if args.json:
        print(json.dumps(as_json(census), indent=2))
    for problem in census.failures:
        print(f"GATE FAILURE (guard): {problem}", file=sys.stderr)
    if census.failures:
        return GUARD
    unreferenced = census.unreferenced()
    problems = seam_problems(census)
    print(
        f"ast census ({args.leg} leg, {len(units)} units): {len(census.functions)} own functions, "
        f"{len(unreferenced)} unreferenced, {len(TEST_SEAMS)} test seams; "
        f"{len(census.decays)} array decays in call arguments (7.11.2) and "
        f"{len(census.literal_decays)} string literals apart; "
        f"{len(census.arrays)} C arrays declared"
    )
    for info in unreferenced:
        where = ", ".join(sorted(f"{p.file}:{p.line}" for p in info.places))
        print(f"  unreferenced: {info.name} ({where})")
    for problem in problems:
        print(f"  stale seam: {problem}")
    if args.decays:
        for site in sorted(census.decays.values(), key=lambda p: (p.file, p.line)):
            print(f"  decay: {site.file}:{site.line}")
    return FINDINGS if unreferenced or problems else 0


if __name__ == "__main__":
    sys.exit(main())
