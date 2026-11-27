#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The MISRA C++:2023 rules the firmware holds by a gate of its own (FW-21).

    python3 scripts/misra_rules.py               # every rule, harness leg
    python3 scripts/misra_rules.py --leg device  # as the image compiles them
    python3 scripts/misra_rules.py --json        # the findings, for the tests

Two readings, each over ``firmware/src`` (docs/misra.md, "Held"):

**From clang's AST**, every translation unit of ``firmware/src/CMakeLists.txt``
dumped as JSON (``scripts/ast_census.py``'s clang and flags), limited to the
declarations of ``kleidos::meter`` and to the C-linkage functions the
firmware defines (:data:`DUMP_FILTERS`; a C-linkage definition no filter
names fails the run), each place counted once however many units include it:

``5.13.4``
    An integer literal of an unsigned type whose spelling has no ``U``
    suffix (``0x80000000`` is ``unsigned int`` by its value alone).
``8.0.1``
    An operand of the conditional operator whose own operator ranks above
    it in the rule's table (``==``, ``+``, ``&&`` and the rest) and is not
    parenthesized: ``(a == b) ? x : y``, never ``a == b ? x : y``. The rule's
    other half, a binary operator's operands, is not read here.
``8.1.2``
    A lambda that captures by default (``[&]``, ``[=]``) instead of naming
    what it captures.
``8.14.1``
    The right-hand operand of ``&&`` or ``||`` that assigns, increments or
    decrements, or calls a function that is neither ``constexpr`` nor a
    ``const`` member function nor one of :data:`PURE_CALLS`, each named with
    why it changes nothing (a member function or an operator with its class,
    so an own ``load()`` is not ``std::atomic``'s; a free function by its
    name, never an own one). A function called through a pointer is a call
    like any other: it is reported unless it is ``const`` or listed.
``8.19.1``
    The comma operator.
``9.4.1``
    An ``if ... else if`` chain that does not end in an ``else``.
``9.5.1``
    A legacy ``for`` statement that is not simple: its init-statement
    declares one loop counter of integer type; its condition compares the
    counter, alone, with a bound by ``<``, ``<=``, ``>``, ``>=`` or ``!=``;
    its increment steps the counter alone, by ``++``, ``--``, ``+=`` or
    ``-=`` of a constant; and the body neither assigns, steps nor takes the
    address of the counter, nor assigns or steps what the bound reads. A
    ``for ( ;; )`` is not simple, and is written ``while ( true )``.

**From the text** (comments removed and literals emptied):

``19.6.1``
    No ``#pragma`` and no ``_Pragma`` in any source, and every header guarded
    by ``#ifndef KLEIDOS_METER_<FILE>_H``, ``#define`` of the same name as its
    first directives and ``#endif  // KLEIDOS_METER_<FILE>_H`` as its last
    line, the name made from the file's (``ina228_regs.h``:
    ``KLEIDOS_METER_INA228_REGS_H``).
``8.2.2``
    None of FreeRTOS's tick macros (:data:`BANNED_MACROS`), which expand to
    C-style casts at their call site: ``rtos_ticks.h`` gives
    ``ticksFromMs()`` and ``kWaitForever`` instead.
``21.2.2``
    No string handling function of the C library
    (:data:`BANNED_STRING_FUNCTIONS`) and no ``#include`` of ``<cstring>``,
    ``<cwchar>`` or their C names (FW-22).
``21.10.1``
    No ``<cstdarg>``: no ``#include`` of it or of ``<stdarg.h>``, no
    ``va_list`` and none of its macros, and no function declared with a C
    ellipsis parameter (FW-26: the console's four printf-style members were
    the last).
``30.0.1``
    No formatted input or output of the C library
    (:data:`BANNED_FORMATTED_IO`), the ROM's ``esp_rom_printf()`` and
    ``ets_printf()`` included, and no ``#include`` of ``<cstdio>`` or
    ``<stdio.h>`` (FW-26: every response is composed with ``text.h``'s
    writer).

Blind spots: a call's side effects are judged by the callee's declaration,
not its body (a ``const`` member function that writes through a pointer
member passes); a counter passed to a function by non-const reference is not
seen as modified; and a construct inside a macro's expansion is read at the
macro's definition, which for ESP-IDF's and FreeRTOS's macros is adopted
code. Exit status: 0 nothing found, 1 a finding, 2 the analysis could not run.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "firmware" / "src"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ast_census  # noqa: E402  (clang, the units and their flags)
import clang_tidy_gate  # noqa: E402  (the guard failure the units can raise)
import misra_register  # noqa: E402  (the sources with comments removed)

#: The exit code of a run that could not be trusted.
GUARD = 2

#: The exit code of a run with a finding.
FINDINGS = 1

#: What each dump is limited to: the instrument's namespace and the C-linkage
#: functions the firmware defines (ESP-IDF's entry point, the heap hooks and
#: the link-time wraps), which are outside it.
DUMP_FILTERS = ("kleidos::meter::", "app_main", "esp_heap_trace_", "__wrap_")

#: Functions a right-hand operand of ``&&`` or ``||`` may call although their
#: declaration is neither ``constexpr`` nor ``const``, each with why the call
#: changes no state. A member function or an overloaded operator is named with
#: its class (``std::atomic::load``: the template of the object's type, its
#: arguments dropped, or the alias the type is spelled with, as
#: ``std::string_view``), so an own ``load()`` or a member of another class by the
#: same name is not one of them; a free function by its name, and only when no
#: own declaration is the callee (an own function is judged by its declaration
#: alone, whatever its name).
PURE_CALLS: dict[str, str] = {
    "std::atomic::load": "a read of the atomic, which writes nothing",
    "std::array::size": "the array's length",
    "std::span::size": "the span's length",
    "std::array::cbegin": "an iterator to the array's first element",
    "std::array::operator[]": "an element's reference, read where it is used",
    "std::string_view::size": "the view's length",
    "std::string_view::operator[]": "a character of the view, read",
    "std::string_view::starts_with": "compares the view's first characters",
    "std::string_view::empty": "whether the view holds no character",
    "std::string_view::front": "the view's first character, read",
    "std::string_view::back": "the view's last character, read",
    "std::string_view::operator==": "compares two views and writes neither",
    "equal": "std::equal(): compares two ranges and writes neither",
    "esp_timer_get_time": "the microseconds since boot, read from the system timer",
    "xPortGetCoreID": "the core the caller runs on, read from the CPU's PRID register",
}


#: A class template by the alias :data:`PURE_CALLS` names it with: clang
#: desugars ``std::string_view`` on one leg and not on the other.
OWNER_ALIASES = {"std::basic_string_view": "std::string_view"}


def owner_of(expression: dict[str, Any]) -> str:
    """The class template of *expression*'s type, its arguments and qualifiers
    dropped: ``const std::array<int, 2> &`` gives ``std::array``."""
    shown = expression.get("type", {})
    text = shown.get("desugaredQualType") or shown.get("qualType", "")
    text = re.sub(r"\b(?:const|volatile|struct|class)\s+", "", text).strip()
    match = re.match(r"[A-Za-z_][\w:]*", text)
    owner = match.group(0) if match else ""
    return OWNER_ALIASES.get(owner, owner)


#: The string handling functions of ``<cstring>``, ``<cstdlib>``,
#: ``<cwchar>`` and ``<cinttypes>`` (21.2.2), by name, and the headers whose
#: inclusion the firmware gives up with them: ``<cstring>`` and its C name.
BANNED_STRING_FUNCTIONS = frozenset(
    {
        # <cstring>
        "strcpy",
        "strncpy",
        "strcat",
        "strncat",
        "strcmp",
        "strncmp",
        "strcoll",
        "strxfrm",
        "strchr",
        "strrchr",
        "strspn",
        "strcspn",
        "strpbrk",
        "strstr",
        "strtok",
        "strerror",
        "strlen",
        "memchr",
        "memcmp",
        "memset",
        "memcpy",
        "memmove",
        # <cstdlib>
        "atof",
        "atoi",
        "atol",
        "atoll",
        "strtod",
        "strtof",
        "strtold",
        "strtol",
        "strtoll",
        "strtoul",
        "strtoull",
        # <cinttypes>
        "strtoimax",
        "strtoumax",
        "wcstoimax",
        "wcstoumax",
        # <cwchar>
        "wcscpy",
        "wcsncpy",
        "wcscat",
        "wcsncat",
        "wcscmp",
        "wcsncmp",
        "wcscoll",
        "wcsxfrm",
        "wcschr",
        "wcsrchr",
        "wcsspn",
        "wcscspn",
        "wcspbrk",
        "wcsstr",
        "wcstok",
        "wcslen",
        "wmemchr",
        "wmemcmp",
        "wmemset",
        "wmemcpy",
        "wmemmove",
        "wcstod",
        "wcstof",
        "wcstold",
        "wcstol",
        "wcstoll",
        "wcstoul",
        "wcstoull",
    }
)

#: The headers 21.2.2 leaves the firmware no reason to include.
BANNED_STRING_HEADERS = ("cstring", "string.h", "cwchar", "wchar.h")

#: ``<cstdarg>`` (21.10.1): its headers and the names it declares.
BANNED_VARARGS_HEADERS = ("cstdarg", "stdarg.h")
BANNED_VARARGS_NAMES = ("va_list", "va_start", "va_arg", "va_copy", "va_end")

#: The C library's formatted input and output (30.0.1), by name, and the
#: ROM's printf under both its names; and the headers that declare them.
BANNED_FORMATTED_IO = frozenset(
    {
        "printf",
        "fprintf",
        "sprintf",
        "snprintf",
        "vprintf",
        "vfprintf",
        "vsprintf",
        "vsnprintf",
        "scanf",
        "fscanf",
        "sscanf",
        "vscanf",
        "vfscanf",
        "vsscanf",
        "asprintf",
        "vasprintf",
        "dprintf",
        "vdprintf",
        "esp_rom_printf",
        "ets_printf",
    }
)
BANNED_FORMATTED_IO_HEADERS = ("cstdio", "stdio.h")

#: The macros the firmware does not use, and what it uses instead.
BANNED_MACROS: dict[str, str] = {
    "pdMS_TO_TICKS": "rtos::ticksFromMs() (rtos_ticks.h)",
    "portMAX_DELAY": "rtos::kWaitForever (rtos_ticks.h)",
}

#: Each rule's ranking in 8.0.1's table, by operator; anything else ranks 14.
RANKS: dict[str, int] = {
    "*": 13,
    "/": 13,
    "%": 13,
    "+": 12,
    "-": 12,
    "<<": 11,
    ">>": 11,
    "<": 10,
    ">": 10,
    "<=": 10,
    ">=": 10,
    "==": 9,
    "!=": 9,
    "&": 8,
    "^": 7,
    "|": 6,
    "&&": 5,
    "||": 4,
    ",": 0,
}

#: The conditional operator's rank in 8.0.1's table.
CONDITIONAL_RANK = 3

#: An assignment's rank in 8.0.1's table.
ASSIGNMENT_RANK = 2

#: The rank of every operator the table does not list.
OTHER_RANK = 14

#: What clang wraps an operand in without the source saying so.
IMPLICIT = frozenset(
    {
        "ImplicitCastExpr",
        "ExprWithCleanups",
        "MaterializeTemporaryExpr",
        "CXXBindTemporaryExpr",
        "ConstantExpr",
    }
)

#: The calls whose callee 8.14.1 weighs.
CALLS = frozenset({"CallExpr", "CXXMemberCallExpr", "CXXOperatorCallExpr"})

#: The declarations a callee is looked up in.
FUNCTION_DECLS = frozenset({"FunctionDecl", "CXXMethodDecl", "CXXConversionDecl"})

#: The operators of 9.5.1's condition.
LOOP_COMPARISONS = frozenset({"<", "<=", ">", ">=", "!="})

#: An integer type as clang spells it, desugared.
INTEGER_TYPE = re.compile(
    r"^(?:(?:un)?signed )?(?:char|short|int|long|long long)(?: int)?$|^(?:un)?signed$"
)

#: The types of MISRA C++:2023's character category, cv-qualifiers removed:
#: ``signed char`` and ``unsigned char`` are integers, not characters.
CHARACTER_TYPES = frozenset({"char", "wchar_t", "char8_t", "char16_t", "char32_t"})

#: The casts 7.0.3 weighs: every conversion the source writes and every one
#: clang inserts.
CASTS = frozenset(
    {
        "ImplicitCastExpr",
        "CStyleCastExpr",
        "CXXStaticCastExpr",
        "CXXFunctionalCastExpr",
        "CXXReinterpretCastExpr",
        "CXXConstCastExpr",
    }
)

#: The casts the source writes, which 8.2.6 weighs.
EXPLICIT_CASTS = frozenset(
    {
        "CStyleCastExpr",
        "CXXStaticCastExpr",
        "CXXFunctionalCastExpr",
        "CXXReinterpretCastExpr",
    }
)

#: What a null pointer constant is written as, which 8.2.6 exempts.
NULL_CONSTANTS = frozenset({"CXXNullPtrLiteralExpr", "GNUNullExpr"})


def pointer_from(node: dict[str, Any]) -> str | None:
    """What 8.2.6 says the explicit cast *node* converts to a pointer from.

    ``"void pointer"``, ``"integer"`` or ``"enumeration"`` for a cast to an
    object pointer from one of those, else None: a cast to a function
    pointer from ``void*`` and a null pointer constant are the rule's
    exceptions, and any other operand is another rule's. An enumeration is
    seen only where clang spells its type ``enum ...``; one named by its tag
    alone reads as neither (the blind spot :func:`is_integer` has too).
    """
    if node.get("kind") not in EXPLICIT_CASTS or not node.get("inner"):
        return None
    target = type_spelled(node.get("type", {}))
    if not target.endswith("*") or "(*)" in target:
        return None
    operand = strip(node["inner"][0])
    if operand.get("kind") in NULL_CONSTANTS or (
        operand.get("kind") == "IntegerLiteral" and operand.get("value") == "0"
    ):
        return None
    source = type_spelled(operand.get("type", {}))
    if source == "void *":
        return "void pointer"
    if source.startswith("enum "):
        return "enumeration"
    if is_integer(source):
        return "integer"
    return None


#: The rules this script reads, in the order it reports them.
RULES = (
    "5.13.4",
    "7.0.3",
    "7.0.5",
    "8.0.1",
    "8.1.2",
    "8.2.2",
    "8.2.6",
    "8.14.1",
    "8.19.1",
    "9.4.1",
    "9.5.1",
    "19.6.1",
    "21.2.2",
    "21.10.1",
    "30.0.1",
)


@dataclass(frozen=True, order=True)
class Finding:
    """One departure: the rule, the file relative to the repository, its line, what."""

    rule: str
    file: str
    line: int
    what: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: MISRA C++:2023 {self.rule}: {self.what}"


@dataclass
class Reading:
    """What every unit says together, each place once."""

    findings: dict[tuple[str, str, int], Finding] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    nodes: int = 0
    pure_used: set[str] = field(default_factory=set)
    widened: set[tuple[str, int]] = field(default_factory=set)
    void_pointer_casts: set[tuple[str, int]] = field(default_factory=set)

    def add(self, finding: Finding, offset: int) -> None:
        """Record *finding*, once per rule and place."""
        self.findings.setdefault((finding.rule, finding.file, offset), finding)

    def sorted(self) -> list[Finding]:
        """The findings, by rule, file and line."""
        return sorted(self.findings.values(), key=lambda f: (RULES.index(f.rule), f))


# --- The AST ---------------------------------------------------------------------


def strip(node: dict[str, Any]) -> dict[str, Any]:
    """*node* without the implicit nodes clang put around it."""
    while node.get("kind") in IMPLICIT and node.get("inner"):
        node = node["inner"][0]
    return node


def overloaded_operator(node: dict[str, Any]) -> str | None:
    """The binary operator a ``CXXOperatorCallExpr`` *node* calls, as written.

    A call of a unary operator, of ``[]``, ``()`` or ``->``, or of anything
    that is not a call of an operator function, gives ``None``.
    """
    inner = node.get("inner", [])
    if node.get("kind") != "CXXOperatorCallExpr" or len(inner) != 3:
        return None
    callee = strip(inner[0])
    name = callee.get("referencedDecl", {}).get("name", "")
    if callee.get("kind") != "DeclRefExpr" or not name.startswith("operator"):
        return None
    return str(name.removeprefix("operator"))


def rewritten_operator(node: dict[str, Any]) -> str | None:
    """The operator a ``CXXRewrittenBinaryOperator`` *node* stands for.

    clang writes ``a != b`` as ``!( a == b )`` and ``a < b`` as
    ``( a <=> b ) < 0``; the operator the source spells is the one outside.
    """
    semantic = strip(node["inner"][0]) if node.get("inner") else {}
    if semantic.get("kind") == "UnaryOperator" and semantic.get("opcode") == "!":
        return "!="
    if semantic.get("kind") == "BinaryOperator":
        return semantic.get("opcode")
    return overloaded_operator(semantic)


def operator_rank(operator: str | None) -> int:
    """The rank of a binary *operator*, as spelled, in 8.0.1's table."""
    if operator is None:
        return OTHER_RANK
    if operator in RANKS:
        return RANKS[operator]
    if operator.endswith("="):
        return ASSIGNMENT_RANK
    return OTHER_RANK


def rank(node: dict[str, Any]) -> int:
    """The rank of *node*'s root operator in 8.0.1's table.

    The rule is about the expression as written, so an overloaded operator
    (an iterator's ``==``, a ``std::string_view``'s ``<``) ranks as the
    built-in one it is spelled as.
    """
    node = strip(node)
    kind = node.get("kind")
    if kind == "BinaryOperator":
        return RANKS.get(node.get("opcode", ""), ASSIGNMENT_RANK)
    if kind == "CompoundAssignOperator":
        return ASSIGNMENT_RANK
    if kind == "ConditionalOperator":
        return CONDITIONAL_RANK
    if kind == "CXXOperatorCallExpr":
        return operator_rank(overloaded_operator(node))
    if kind == "CXXRewrittenBinaryOperator":
        return operator_rank(rewritten_operator(node))
    return OTHER_RANK


def referenced_id(node: dict[str, Any]) -> str | None:
    """The declaration a ``DeclRefExpr`` under *node*'s implicit wrappers names."""
    node = strip(node)
    if node.get("kind") == "DeclRefExpr":
        found: str | None = node.get("referencedDecl", {}).get("id")
        return found
    return None


def subtree(node: object) -> Iterator[dict[str, Any]]:
    """*node* and every node below it, lambdas' bodies left out."""
    if not isinstance(node, dict):
        return
    yield node
    if node.get("kind") == "LambdaExpr":
        return
    for child in node.get("inner", []):
        yield from subtree(child)


def modified_ids(body: dict[str, Any]) -> set[str]:
    """The declarations *body* assigns, steps or takes the address of, by id."""
    found: set[str] = set()
    for node in subtree(body):
        kind = node.get("kind")
        target = None
        assigns = kind == "CompoundAssignOperator" or (
            kind == "BinaryOperator" and node.get("opcode") == "="
        )
        steps = kind == "UnaryOperator" and node.get("opcode") in ("++", "--", "&")
        if assigns or steps:
            target = node["inner"][0]
        if target is not None:
            target = strip(target)
            while target.get("kind") in ("MemberExpr", "ArraySubscriptExpr", "ParenExpr"):
                if target.get("kind") == "MemberExpr" and target.get("referencedMemberDecl"):
                    found.add(target["referencedMemberDecl"])
                target = strip(target["inner"][0])
            if target.get("kind") == "DeclRefExpr":
                found.add(target.get("referencedDecl", {}).get("id", ""))
    return found


def read_ids(node: dict[str, Any]) -> set[str]:
    """The declarations and members *node* reads, by id."""
    found: set[str] = set()
    for inner in subtree(node):
        if inner.get("kind") == "DeclRefExpr":
            found.add(inner.get("referencedDecl", {}).get("id", ""))
        elif inner.get("kind") == "MemberExpr" and inner.get("referencedMemberDecl"):
            found.add(inner["referencedMemberDecl"])
    return found


def is_constant_step(node: dict[str, Any], constants: Mapping[str, bool]) -> bool:
    """Whether *node* is a literal or names a constant: a loop step 9.5.1 accepts."""
    node = strip(node)
    if node.get("kind") == "IntegerLiteral":
        return True
    ident = referenced_id(node)
    return ident is not None and constants.get(ident, False)


def for_problem(node: dict[str, Any], constants: Mapping[str, bool]) -> str | None:
    """Why the ``ForStmt`` *node* is not simple (9.5.1), or None."""
    inner = node.get("inner", [])
    if len(inner) != 5:
        return "a for statement clang did not dump with its five parts"
    init, variable, condition, increment, body = inner
    if variable:
        return "its condition declares a variable"
    declarations = init.get("inner", []) if init.get("kind") == "DeclStmt" else []
    if len(declarations) != 1 or declarations[0].get("kind") != "VarDecl":
        return "its init-statement does not declare exactly one loop counter"
    counter = declarations[0]
    shown = counter.get("type", {})
    spelled = shown.get("desugaredQualType", shown.get("qualType", ""))
    if not INTEGER_TYPE.match(spelled) or not counter.get("init"):
        return "its loop counter is not an initialized variable of integer type"
    ident = counter.get("id")
    condition = strip(condition) if condition else {}
    if condition.get("kind") != "BinaryOperator" or condition.get("opcode") not in (
        LOOP_COMPARISONS
    ):
        return "its condition is not one comparison of the counter with a bound"
    left, right = condition["inner"]
    if referenced_id(left) == ident:
        bound = right
    elif referenced_id(right) == ident:
        bound = left
    else:
        return "its condition does not compare the counter itself"
    bound_reads = read_ids(bound)
    if ident in bound_reads:
        return "its bound reads the counter"
    increment = strip(increment) if increment else {}
    kind = increment.get("kind")
    if kind == "UnaryOperator" and increment.get("opcode") in ("++", "--"):
        stepped = referenced_id(increment["inner"][0])
    elif kind == "CompoundAssignOperator" and increment.get("opcode") in ("+=", "-="):
        stepped = referenced_id(increment["inner"][0])
        if not is_constant_step(increment["inner"][1], constants):
            return "its step is not a constant"
    else:
        return "its increment does not step the counter alone"
    if stepped != ident:
        return "its increment steps something other than the counter"
    changed = modified_ids(body)
    if ident in changed:
        return "its body modifies the counter"
    if bound_reads & changed:
        return "its body modifies what its bound reads"
    return None


def chain_without_else(node: dict[str, Any]) -> bool:
    """Whether the ``IfStmt`` *node* heads an ``if ... else if`` chain with no final ``else``."""
    if not node.get("hasElse"):
        return False
    current = node["inner"][-1]
    if current.get("kind") != "IfStmt":
        return False
    while current.get("kind") == "IfStmt" and current.get("hasElse"):
        current = current["inner"][-1]
    return bool(current.get("kind") == "IfStmt")


def type_name(node: dict[str, Any]) -> str:
    """*node*'s type as clang spells it desugared, without cv-qualifiers."""
    shown = node.get("type", {})
    spelled = shown.get("desugaredQualType", shown.get("qualType", ""))
    words = [w for w in spelled.split() if w not in ("const", "volatile")]
    return " ".join(words)


#: The binary operators whose operands integral promotion and the usual
#: arithmetic conversions reach (7.0.5): arithmetic, bitwise, shift and
#: relational. An assignment's conversion is 7.0.6's, not read here.
CONVERTING_OPERATORS = frozenset(
    {"+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>", "<", ">", "<=", ">=", "==", "!="}
)

#: The unary operators whose operand integral promotion reaches (7.0.5).
CONVERTING_UNARY = frozenset({"-", "+", "~"})


def is_integer(name: str) -> bool:
    """Whether the desugared type *name* is of the integer category (7.0.5).

    The standard integer types, ``signed char`` and ``unsigned char`` among
    them; not the character types (7.0.3), ``bool`` (7.0.1, 7.0.2) nor an
    enumeration (a blind spot: its underlying type is not read).
    """
    return name not in CHARACTER_TYPES and bool(INTEGER_TYPE.match(name))


def is_unsigned(name: str) -> bool:
    """Whether the integer type *name* is unsigned."""
    return name.split()[0] == "unsigned"


def type_spelled(shown: Mapping[str, str]) -> str:
    """A type clang shows, desugared and without cv-qualifiers."""
    spelled = shown.get("desugaredQualType", shown.get("qualType", ""))
    return " ".join(w for w in spelled.split() if w not in ("const", "volatile"))


def signedness_change(operand: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """The conversion clang put around *operand* that changes its signedness.

    Walks the implicit nodes around an operator's operand and returns the
    first integral conversion between two integer types of different
    signedness, as (from, to, the converted expression), or None. Clang
    writes a narrow unsigned operand that meets a wider unsigned one as one
    conversion to the wider type, which changes nothing here; one that meets
    another narrow operand is promoted to ``int``, which does.
    """
    node = operand
    while node.get("kind") in IMPLICIT and node.get("inner"):
        inner = node["inner"][0]
        if node.get("kind") == "ImplicitCastExpr" and node.get("castKind") == "IntegralCast":
            source, target = type_name(inner), type_name(node)
            if (
                is_integer(source)
                and is_integer(target)
                and is_unsigned(source) != is_unsigned(target)
            ):
                return source, target, strip(inner)
        node = inner
    return None


#: The unsigned types narrower than ``int``, which promotion makes ``int``.
NARROW_UNSIGNED = frozenset({"unsigned char", "unsigned short", "unsigned short int"})


def widened_narrow(operand: dict[str, Any]) -> bool:
    """Whether clang converted *operand*, of a narrow unsigned type, straight to a
    wider unsigned one: what 7.0.5's reading here passes and D12 counts."""
    node = operand
    while node.get("kind") in IMPLICIT and node.get("inner"):
        inner = node["inner"][0]
        if node.get("kind") == "ImplicitCastExpr" and node.get("castKind") == "IntegralCast":
            source, target = type_name(inner), type_name(node)
            return source in NARROW_UNSIGNED and is_integer(target) and is_unsigned(target)
        node = inner
    return False


def converted_operands(node: dict[str, Any]) -> list[dict[str, Any]]:
    """The operands of *node* that promotion or the usual arithmetic conversions reach."""
    kind = node.get("kind")
    if kind == "BinaryOperator" and node.get("opcode") in CONVERTING_OPERATORS:
        return list(node["inner"])
    if kind == "CompoundAssignOperator":
        return list(node["inner"][1:])
    if kind == "UnaryOperator" and node.get("opcode") in CONVERTING_UNARY:
        return list(node["inner"])
    if kind == "ConditionalOperator":
        return list(node["inner"][1:])
    return []


def is_character(node: dict[str, Any]) -> bool:
    """Whether *node*'s type is of the character category (7.0.3)."""
    return type_name(node) in CHARACTER_TYPES


def character_comparison(node: dict[str, Any]) -> bool:
    """Whether *node* is ``==`` or ``!=`` between two operands of character type.

    Comparing two characters for identity uses no character's numerical
    value; only the promotion clang writes around each operand converts.
    """
    if node.get("kind") != "BinaryOperator" or node.get("opcode") not in ("==", "!="):
        return False
    return all(is_character(strip(operand)) for operand in node.get("inner", []))


class _Reader(ast_census._Reader):
    """Walks one unit's dump, tracking each node's file as the census does."""

    def __init__(self, own: Path, reading: Reading, base: Path, callees: dict[str, Any]) -> None:
        super().__init__(own, ast_census.Census(), base)
        self.reading = reading
        self.callees = callees
        self.texts: dict[str, bytes] = {}
        self.continued: set[str] = set()
        self.compared: set[str] = set()

    def text_of(self, relative: str) -> bytes:
        """The bytes of the file *relative* names: clang's offsets count bytes."""
        if relative not in self.texts:
            path = Path(relative)
            path = path if path.is_absolute() else ROOT / path
            self.texts[relative] = path.read_bytes()
        return self.texts[relative]

    def source(self, node: dict[str, Any], relative: str, whole: bool = True) -> str | None:
        """The text of *node* in *relative*: its whole range, or its first token.

        None when a macro wrote it (its range names a spelling or expansion).
        """
        span = node.get("range", {})
        begin, end = span.get("begin", {}), span.get("end", {})
        if any(k in point for point in (begin, end) for k in ("spellingLoc", "expansionLoc")):
            return None
        if "offset" not in begin or (whole and "offset" not in end):
            return None
        start = int(begin["offset"])
        if whole:
            stop = int(end["offset"]) + int(end.get("tokLen", 0))
        else:
            stop = start + int(begin.get("tokLen", 0))
        return self.text_of(relative)[start:stop].decode("utf-8", errors="replace")

    def walk_rules(self, node: dict[str, Any]) -> None:
        """Read *node* and its children against every AST rule, in document order."""
        self.location(node.get("loc"))
        begin = None
        if "range" in node:
            self.location(node["range"].get("begin"))
            offset = int(node["range"].get("begin", {}).get("offset", -1))
            begin = (self._is_own(), self._relative() or "?", self.line, offset)
            self.location(node["range"].get("end"))
        for key, value in node.items():
            if key not in ("loc", "range", "inner"):
                self.location(value)
        if begin is not None and begin[0]:
            self.reading.nodes += 1
            self.check(node, begin[1], begin[2], begin[3])
        if character_comparison(node):
            # The promotions of two characters compared for identity.
            self.compared.update(operand.get("id", "") for operand in node["inner"])
        if node.get("kind") == "IfStmt" and node.get("hasElse"):
            # The else branch of a chain is read as part of its head.
            last = node["inner"][-1]
            if last.get("kind") == "IfStmt":
                self.continued.add(last.get("id", ""))
        for child in node.get("inner", []):
            if isinstance(child, dict):
                self.walk_rules(child)

    def check(self, node: dict[str, Any], file: str, line: int, offset: int) -> None:
        """Every AST rule on *node*, which starts at *offset* of *file*, on *line*."""
        kind = node.get("kind")

        def report(rule: str, what: str) -> None:
            self.reading.add(Finding(rule, file, line, what), offset)

        if kind in CASTS and node.get("inner") and node.get("id") not in self.compared:
            source = node["inner"][0]
            if is_character(source) != is_character(node):
                report(
                    "7.0.3",
                    f"a conversion from {type_name(source) or '?'} to {type_name(node) or '?'}"
                    f" ({node.get('castKind', kind)}) uses a character's numerical value",
                )
        converted_from = pointer_from(node)
        if converted_from == "void pointer":
            # D13: a callback's context, which ESP-IDF's and FreeRTOS's
            # signatures carry as void*; counted, and held to the register.
            self.reading.void_pointer_casts.add((file, offset))
        elif converted_from is not None:
            report("8.2.6", f"an {converted_from} is cast to a pointer type")
        if kind == "CompoundAssignOperator":
            lhs = type_name(node["inner"][0])
            computed = type_spelled(node.get("computeLHSType", {}))
            if (
                is_integer(lhs)
                and is_integer(computed)
                and is_unsigned(lhs) != is_unsigned(computed)
            ):
                report("7.0.5", f"{node['opcode']} computes its {lhs} left operand as {computed}")
        for operand in converted_operands(node):
            if widened_narrow(operand):
                where = int(operand.get("range", {}).get("begin", {}).get("offset", offset))
                self.reading.widened.add((file, where))
            changed = signedness_change(operand)
            if changed is not None:
                where = int(operand.get("range", {}).get("begin", {}).get("offset", offset))
                self.reading.add(
                    Finding(
                        "7.0.5",
                        file,
                        line,
                        f"an operand of {node.get('opcode', '?:')} is converted from"
                        f" {changed[0]} to {changed[1]}"
                        + (
                            " (a literal: suffix it U)"
                            if changed[2].get("kind") == "IntegerLiteral"
                            else ""
                        ),
                    ),
                    where,
                )
        if kind == "IntegerLiteral" and "unsigned" in json.dumps(node.get("type", {})):
            spelled = self.source(node, file, whole=False)
            # An implicit literal (an array copy's index) sits on a name.
            if spelled and spelled[0].isdigit() and "u" not in spelled.lower():
                report("5.13.4", f"the unsigned literal {spelled} has no U suffix")
        elif kind == "ConditionalOperator":
            for part, operand in zip(
                ("condition", "operand", "operand"), node["inner"], strict=True
            ):
                inner = strip(operand)
                if inner.get("kind") != "ParenExpr" and CONDITIONAL_RANK < rank(inner) < OTHER_RANK:
                    report("8.0.1", f"a conditional operator's {part} is not parenthesized")
        elif kind == "LambdaExpr":
            text = self.source(node, file)
            if text is not None and re.match(r"\[\s*[&=]\s*[\],]", text):
                report("8.1.2", "a lambda captures by default")
        elif kind == "BinaryOperator" and node.get("opcode") in ("&&", "||"):
            for what in self.side_effects(node["inner"][1]):
                report("8.14.1", f"the right operand of {node['opcode']} {what}")
        elif kind == "BinaryOperator" and node.get("opcode") == ",":
            report("8.19.1", "the comma operator")
        elif kind == "IfStmt" and node.get("id") not in self.continued and chain_without_else(node):
            report("9.4.1", "an if ... else if chain does not end in an else")
        elif kind == "ForStmt":
            problem = for_problem(node, self.callees.get("constants", {}))
            if problem is not None:
                report("9.5.1", f"a for statement that is not simple: {problem}")

    def side_effects(self, operand: dict[str, Any]) -> list[str]:
        """What in *operand* changes state (8.14.1), each as a phrase."""
        found = []
        for node in subtree(operand):
            kind = node.get("kind")
            if kind == "CompoundAssignOperator" or (
                kind == "BinaryOperator" and node.get("opcode") == "="
            ):
                found.append(f"assigns ({node['opcode']})")
            elif kind == "UnaryOperator" and node.get("opcode") in ("++", "--"):
                found.append(f"steps a variable ({node['opcode']})")
            elif kind in CALLS:
                ident, name = callee_of(node)
                declared = self.callees.get("functions", {}).get(ident)
                if declared is not None and (declared["constexpr"] or declared["const"]):
                    continue
                pure = None if declared is not None else pure_name(node, name)
                if pure in PURE_CALLS:
                    self.reading.pure_used.add(pure)
                    continue
                found.append(f"calls {name}(), which is neither constexpr nor const")
        return found


def pure_name(call: dict[str, Any], name: str) -> str:
    """The name :data:`PURE_CALLS` knows *call* by: ``<class>::<name>`` for a
    member function (the object's class) or an overloaded operator (its left
    operand's class), the bare name for a free function."""
    inner = call.get("inner", [])
    if call.get("kind") == "CXXOperatorCallExpr" and len(inner) > 1:
        return f"{owner_of(strip(inner[1]))}::{name}"
    target = strip(inner[0]) if inner else {}
    while target.get("kind") == "ParenExpr":
        target = strip(target["inner"][0])
    if target.get("kind") == "MemberExpr" and target.get("inner"):
        return f"{owner_of(strip(target['inner'][0]))}::{name}"
    return name


def callee_of(call: dict[str, Any]) -> tuple[str | None, str]:
    """The id and the name of the function *call* calls."""
    target = call["inner"][0] if call.get("inner") else {}
    if call.get("kind") == "CXXOperatorCallExpr":
        # The callee of an overloaded operator is its first child, the
        # operator's own reference.
        target = call["inner"][0]
    target = strip(target)
    while target.get("kind") == "ParenExpr":
        target = strip(target["inner"][0])
    if target.get("kind") == "MemberExpr":
        return target.get("referencedMemberDecl"), target.get("name", "?")
    if target.get("kind") == "DeclRefExpr":
        declared = target.get("referencedDecl", {})
        return declared.get("id"), declared.get("name", "?")
    return None, target.get("kind", "?")


def declarations(documents: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """What the dumps declare that the rules look up: functions and constants, by id."""
    functions: dict[str, dict[str, Any]] = {}
    constants: dict[str, bool] = {}
    for document in documents:
        for node in _all_nodes(document):
            kind = node.get("kind")
            if kind in FUNCTION_DECLS and "id" in node:
                shown = node.get("type", {}).get("qualType", "")
                functions[node["id"]] = {
                    "constexpr": bool(node.get("constexpr")),
                    "const": bool(re.search(r"\)\s*const\b", shown)),
                }
            elif kind == "VarDecl" and "id" in node:
                shown = node.get("type", {}).get("qualType", "")
                constants[node["id"]] = bool(node.get("constexpr")) or shown.startswith("const ")
    return {"functions": functions, "constants": constants}


def _all_nodes(node: object) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for child in node.get("inner", []):
            yield from _all_nodes(child)


def read_unit(texts: Sequence[str], reading: Reading, own: Path, base: Path = ROOT) -> None:
    """Merge one unit's filtered dumps (one per :data:`DUMP_FILTERS` entry) into *reading*."""
    documents = [document for text in texts for document in ast_census.json_documents(text)]
    callees = declarations(documents)
    for text in texts:
        reader = _Reader(own, reading, base, callees)
        for document in ast_census.json_documents(text):
            reader.walk_rules(document)


def run_units(
    clang: str, units: Mapping[Path, Sequence[str]], own: Path = SOURCES, jobs: int | None = None
) -> Reading:
    """Dump every unit of *units* (source to flags) once per filter and read the rules."""
    reading = Reading()
    work = [(source, flags, f) for source, flags in units.items() for f in DUMP_FILTERS]

    def one(item: tuple[Path, Sequence[str], str]) -> subprocess.CompletedProcess[str]:
        source, flags, dump_filter = item
        command = ast_census.dump_command(clang, source, flags)
        command[command.index(f"-ast-dump-filter={ast_census.DUMP_FILTER}")] = (
            f"-ast-dump-filter={dump_filter}"
        )
        return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)

    with ThreadPoolExecutor(max_workers=max(1, jobs or 8)) as pool:
        results = list(pool.map(one, work))
    by_unit: dict[Path, list[str]] = {}
    for (source, _flags, _filter), result in zip(work, results, strict=True):
        if result.returncode != 0 or "error:" in result.stderr:
            last = (result.stderr.strip().splitlines() or [f"exit {result.returncode}"])[-1]
            reading.failures.append(f"{source.name}: clang did not parse it: {last}")
            continue
        by_unit.setdefault(source, []).append(result.stdout)
    for texts in by_unit.values():
        read_unit(texts, reading, own)
    return reading


# --- The text --------------------------------------------------------------------

_C_LINKAGE_DEFINITION = re.compile(r'extern\s+"[^"\n]*"\s+[^;{()]*?\b(\w+)\s*\([^;{]*\)[^;{]*\{')


def guard_name(path: Path) -> str:
    """The include guard *path*'s header carries: ``KLEIDOS_METER_<FILE>_H``."""
    return "KLEIDOS_METER_" + re.sub(r"[^A-Za-z0-9]", "_", path.stem).upper() + "_H"


def text_findings(path: Path, text: str, relative: str) -> list[Finding]:
    """The textual rules on one source: 19.6.1 (no pragma, the guard), 8.2.2's
    macros, 21.2.2 (no C string handling function, no ``<cstring>``), 21.10.1
    (no ``<cstdarg>``, no C variadic function) and 30.0.1 (no formatted input
    or output of the C library, no ``<cstdio>``)."""
    code = misra_register.code_of(text)
    found: list[Finding] = []
    found.extend(
        Finding("19.6.1", relative, misra_register.line_of(code, match.start()), "a pragma")
        for match in re.finditer(r"#\s*pragma\b|\b_Pragma\b", code)
    )
    for match in re.finditer(r"#\s*include\s*<([\w./]+)>", code):
        header = match.group(1)
        line = misra_register.line_of(code, match.start())
        if header in BANNED_STRING_HEADERS:
            found.append(Finding("21.2.2", relative, line, f"#include <{header}>"))
        elif header in BANNED_VARARGS_HEADERS:
            found.append(Finding("21.10.1", relative, line, f"#include <{header}>"))
        elif header in BANNED_FORMATTED_IO_HEADERS:
            found.append(Finding("30.0.1", relative, line, f"#include <{header}>"))
        else:
            pass  # any other header is not these rules'
    for match in re.finditer(r"\b(?:std::)?(va_\w+)\b", code):
        if match.group(1) in BANNED_VARARGS_NAMES:
            line = misra_register.line_of(code, match.start())
            found.append(Finding("21.10.1", relative, line, f"{match.group(1)}, of <cstdarg>"))
    for match in re.finditer(r"\.\.\.\s*\)", code):
        line = misra_register.line_of(code, match.start())
        found.append(
            Finding(
                "21.10.1",
                relative,
                line,
                "a C variadic parameter list; use typed functions (text.h's writer)",
            )
        )
    for match in re.finditer(r"\b(\w+)\s*\(", code):
        if match.group(1) in BANNED_FORMATTED_IO:
            line = misra_register.line_of(code, match.start())
            found.append(
                Finding(
                    "30.0.1",
                    relative,
                    line,
                    f"{match.group(1)}(), formatted output of the C library;"
                    " use text.h's bounded writer",
                )
            )
        if match.group(1) in BANNED_STRING_FUNCTIONS:
            line = misra_register.line_of(code, match.start())
            found.append(
                Finding(
                    "21.2.2",
                    relative,
                    line,
                    f"{match.group(1)}(), a string handling function of the C library;"
                    " use std::string_view, std::copy_n or std::from_chars",
                )
            )
    for name, instead in BANNED_MACROS.items():
        for match in re.finditer(rf"\b{name}\b", code):
            line = misra_register.line_of(code, match.start())
            found.append(Finding("8.2.2", relative, line, f"{name}, a C-style cast; use {instead}"))
    if path.suffix == ".h":
        expected = guard_name(path)
        directives = [
            (number, line.strip())
            for number, line in enumerate(code.splitlines(), 1)
            if line.strip().startswith("#")
        ]
        lines = text.rstrip("\n").splitlines()
        opened = [d for _, d in directives[:2]] == [f"#ifndef {expected}", f"#define {expected}"]
        closed = bool(lines) and lines[-1] == f"#endif  // {expected}"
        if not opened or not closed or not directives or directives[-1][1] != "#endif":
            found.append(
                Finding(
                    "19.6.1",
                    relative,
                    directives[0][0] if directives else 1,
                    f"the header is not guarded by {expected}",
                )
            )
    return found


def c_linkage_definitions(root: Path = SOURCES) -> dict[str, str]:
    """Every function the sources define with C linkage, by name, and its file."""
    found = {}
    for path in misra_register.own_sources(root):
        code = misra_register.code_of(path.read_text(encoding="utf-8"))
        for match in _C_LINKAGE_DEFINITION.finditer(code):
            found[match.group(1)] = path.name
    return found


def unfiltered(root: Path = SOURCES) -> list[str]:
    """The C-linkage definitions no :data:`DUMP_FILTERS` entry names, which no rule reads."""
    return sorted(
        f"{name} ({file})"
        for name, file in c_linkage_definitions(root).items()
        if not any(f in name for f in DUMP_FILTERS)
    )


def text_reading(root: Path = SOURCES) -> list[Finding]:
    """The textual rules over every source of *root*."""
    found = []
    for path in misra_register.own_sources(root):
        relative = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        found.extend(text_findings(path, path.read_text(encoding="utf-8"), relative))
    return found


def main(argv: Sequence[str] | None = None) -> int:
    """Read every rule and return the gate's exit code."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--leg", choices=("harness", "device"), default="harness")
    parser.add_argument("--clang", help="the clang++ binary")
    parser.add_argument("--json", action="store_true", help="print the findings as JSON")
    args = parser.parse_args(argv)
    clang = ast_census.find_clang(args.clang)
    if clang is None:
        print("GATE FAILURE (guard): no clang++ found (--clang, METER_CLANG, LLVM_BIN)")
        return GUARD
    missing = unfiltered()
    if missing:
        print(f"GATE FAILURE (guard): C-linkage definitions no dump filter reads: {missing}")
        return GUARD
    try:
        units = ast_census.image_units(args.leg)
    except clang_tidy_gate.GuardFailure as failure:
        print(f"GATE FAILURE (guard): {failure}")
        return GUARD
    reading = run_units(clang, units)
    for problem in reading.failures:
        print(f"GATE FAILURE (guard): {problem}")
    if reading.failures:
        return GUARD
    findings = sorted(set(reading.sorted()) | set(text_reading()))
    findings.sort(key=lambda f: (RULES.index(f.rule), f))
    if args.json:
        print(json.dumps([vars(f) for f in findings], indent=2))
    else:
        for finding in findings:
            print(finding)
        print(
            f"misra rules ({args.leg} leg, {len(units)} units, {reading.nodes} own nodes): "
            f"{len(findings)} finding(s) of {', '.join(RULES)}"
        )
    return FINDINGS if findings else 0


if __name__ == "__main__":
    sys.exit(main())
