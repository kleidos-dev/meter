# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""docs/misra.md's register is what the sources hold, table by table.

``scripts/misra_register.py`` reads ``firmware/src`` (and the suppressions
under ``firmware/`` and in ``.cppcheck-suppress``) for what the register
states: D9's static locals (6.7.1), D10's constructs that C++17 does not have
(4.1.1), the tool suppressions and D5's discarded results; and it fails any
standard integer type name (6.9.2) outside an alias declaration (FW-23; the
printf formats' few went with D2 in FW-26). These
tests hold the register to the sources, and the reader to planted cases: each
kind of finding the register does not list fails, and what is not a finding
(a comment, a literal, a ``const`` local, a member) does not count.

Host-only: the sources read as text.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "misra_register.py"
REGISTER = ROOT / "docs" / "misra.md"


def load_register() -> ModuleType:
    """Import scripts/misra_register.py as a module."""
    spec = importlib.util.spec_from_file_location("misra_register", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["misra_register"] = module
    spec.loader.exec_module(module)
    return module


reg = load_register()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A copy of the firmware's sources, its host tests' sources and the cppcheck list."""
    shutil.copytree(ROOT / "firmware" / "src", tmp_path / "firmware" / "src")
    shutil.copytree(
        ROOT / "firmware" / "test" / "native",
        tmp_path / "firmware" / "test" / "native",
        ignore=lambda folder, names: [
            n
            for n in names
            if n == "build" or (Path(folder, n).is_file() and Path(n).suffix not in (".cpp", ".h"))
        ],
    )
    shutil.copy2(ROOT / ".cppcheck-suppress", tmp_path / ".cppcheck-suppress")
    return tmp_path


def plant(tree: Path, name: str, text: str) -> None:
    """Add a source *name* holding *text* to the copied tree."""
    (tree / "firmware" / "src" / name).write_text("// A planted source.\n" + text, encoding="utf-8")


def register_text() -> str:
    return REGISTER.read_text(encoding="utf-8")


# --- The register against the sources ---------------------------------------------


def test_the_register_matches_the_sources() -> None:
    assert reg.differences() == []


def test_the_copied_tree_matches_the_register_before_anything_is_planted(tree: Path) -> None:
    assert reg.differences(register_text(), tree) == []


def test_every_table_the_register_is_held_to_has_rows() -> None:
    text = register_text()
    assert reg.register_static_locals(text)
    assert reg.register_language(text)
    assert reg.register_suppressions(text)
    assert reg.register_counts(text)
    assert reg.register_void_casts(text)


def test_every_reason_is_written() -> None:
    text = register_text()
    for row in reg.table_rows(text, reg.HEADINGS["static-locals"]):
        assert row[3].strip(), f"static local {row[:3]} has no reason"
    for row in reg.table_rows(text, reg.HEADINGS["language"]):
        assert row[1].strip(), f"{row[0]} says not where it comes from"
    for item, (_, why) in reg.register_suppressions(text).items():
        assert why.strip(), f"{item} has no reason"
    for callee, (_, why) in reg.register_void_casts(text).items():
        assert why.strip(), f"(void) {callee}() has no reason"


def test_the_void_casts_add_up_to_their_count() -> None:
    text = register_text()
    total = sum(count for count, _ in reg.register_void_casts(text).values())
    assert total == reg.register_counts(text)["d5.void_casts"]


# --- Planted cases: each kind of finding fails ------------------------------------


def test_a_planted_static_local_fails(tree: Path) -> None:
    plant(
        tree,
        "planted.cpp",
        "namespace x {\nint counter() noexcept {\n"
        "    static int calls = 0;\n    return ++calls;\n}\n}\n",
    )
    problems = reg.differences(register_text(), tree)
    assert any("('planted.cpp', 'counter', 'calls')" in p and "not in D9" in p for p in problems), (
        problems
    )


def test_a_static_local_in_a_lambda_or_a_member_body_counts() -> None:
    text = (
        "struct S {\n    int f() const noexcept {\n"
        "        static int a = 0;\n        return a;\n    }\n};\n"
        "void g() {\n    auto h = []() {\n"
        "        static int b = 0;\n        return b;\n    };\n    (void)h();\n}\n"
        "S::S() noexcept : x_( 1 ), y_{ 2 } {\n    static int c;\n}\n"
    )
    found = {(s.function, s.name) for s in reg.static_locals("t.cpp", text)}
    assert ("f", "a") in found
    assert ("g", "b") in found  # a lambda's static is its enclosing function's
    assert ("S::S", "c") in found


def test_const_members_namespace_statics_comments_and_literals_do_not_count() -> None:
    text = (
        "namespace {\nstatic int fileLocal = 0;\n}\n"
        "struct S {\n    static int member;\n    static constexpr int kTable[] = { 1, 2 };\n};\n"
        'extern "C" void entry() {\n'
        "    static constexpr int kA = 1;\n"
        "    static const int kB = 2;\n"
        '    static const char* const kC = "x";\n'
        "    // static int inComment = 0;\n"
        '    const char* text = "static int inLiteral = 0;";\n'
        '    static_assert( kA == 1, "static int x;" );\n'
        "    constexpr int kD = 1'000'000;\n"
        "    (void)text;\n"
        "}\n"
    )
    assert reg.static_locals("t.cpp", text) == []


def test_a_pointer_to_const_is_not_a_const_object() -> None:
    found = reg.static_locals("t.cpp", "void f() {\n    static const char* name = nullptr;\n}\n")
    assert [(s.function, s.name) for s in found] == [("f", "name")]


def test_an_unlisted_attribute_fails(tree: Path) -> None:
    plant(tree, "planted.cpp", "[[gnu::cold]] void rarely() noexcept {}\n")
    problems = reg.differences(register_text(), tree)
    assert any("[[gnu::cold]] (planted.cpp) is not in D10" in p for p in problems), problems


def test_each_kind_of_later_construct_is_seen() -> None:
    text = (
        "#include <span>\n#include <cstdint>\n"
        "consteval int f() { return 1; }\n"
        "[[nodiscard]] [[maybe_unused]] int g();\n"
        "[[gnu::always_inline]] inline int h();\n"
        "int k() __attribute__( ( noinline ) );\n"
        "IRAM_ATTR void isr();\n"
        "auto c = std::bit_cast<int>( 1.0f );\n"
        'bool s = name.starts_with( "x" );\n'
        "int v = __builtin_popcount( 3U );\n"
    )
    found = reg.language_features(text)
    assert found == {
        "<span>",
        "consteval",
        "[[gnu::always_inline]]",
        "__attribute__((noinline))",
        "IRAM_ATTR",
        "std::bit_cast",
        ".starts_with()",
        "__builtin_*",
    }


def test_a_construct_in_a_comment_or_a_literal_is_not_seen() -> None:
    text = '// [[gnu::cold]] consteval std::bit_cast\nconst char* s = "constinit [[gnu::hot]]";\n'
    assert reg.language_features(text) == set()


def test_a_construct_no_source_uses_any_more_fails() -> None:
    row = "| `<bit>` | C++20 | `calibration.cpp`, `heap_monitor.cpp`, `pinmap_store.cpp` |"
    text = register_text()
    assert row in text
    text = text.replace(row, row + "\n| `std::midpoint` | C++20 | `sampler.cpp` |")
    problems = reg.differences(text)
    assert any("D10 lists std::midpoint" in p for p in problems), problems


def test_a_suppression_without_a_row_fails(tree: Path) -> None:
    plant(tree, "planted.cpp", "int f() { return 1; }  // NOLINT(bugprone-planted)\n")
    problems = reg.differences(register_text(), tree)
    assert any(
        "planted.cpp" in p and "bugprone-planted" in p and "no row" in p for p in problems
    ), problems


def test_a_cppcheck_comment_and_a_list_entry_without_a_row_fail(tree: Path) -> None:
    plant(tree, "planted.cpp", "// cppcheck-suppress knownConditionTrueFalse ; planted\nint f();\n")
    with (tree / ".cppcheck-suppress").open("a", encoding="utf-8") as stream:
        stream.write("\n# planted\nduplicateBranch\n")
    problems = reg.differences(register_text(), tree)
    assert any("knownConditionTrueFalse" in p and "no row" in p for p in problems), problems
    assert any("duplicateBranch" in p and "no row" in p for p in problems), problems


def test_a_suppression_in_a_host_test_needs_a_row_too(tree: Path) -> None:
    path = tree / "firmware" / "test" / "native" / "test_planted.cpp"
    path.write_text("int x;  // NOLINTNEXTLINE(misc-planted)\n", encoding="utf-8")
    problems = reg.differences(register_text(), tree)
    assert any("test_planted.cpp" in p and "misc-planted" in p for p in problems), problems


def test_a_stale_suppression_row_and_a_moved_count_fail() -> None:
    text = register_text().replace(
        "| `firmware/src/main.cpp` | `NOLINT` | `readability-identifier-naming` | 1 |",
        "| `firmware/src/main.cpp` | `NOLINT` | `readability-identifier-naming` | 2 |",
    )
    text = text.replace(
        "| `.cppcheck-suppress` | `list` | `useStlAlgorithm` | 1 |",
        "| `.cppcheck-suppress` | `list` | `useStlAlgorithm` | 1 | planted |\n"
        "| `.cppcheck-suppress` | `list` | `unusedFunction` | 1 |",
    )
    problems = reg.differences(text)
    assert any("main.cpp" in p and "counts" in p and "there are 1" in p for p in problems), problems
    assert any("unusedFunction" in p and "no longer there" in p for p in problems), problems


def test_a_stale_d5_count_fails() -> None:
    counts = reg.counts()
    stale = counts["d5.void_casts"] + 1
    row = "| `d5.void_casts` | calls whose result is cast to `void` (D5) |"
    text = register_text().replace(f"{row} {counts['d5.void_casts']} |", f"{row} {stale} |")
    problems = reg.differences(text)
    assert any(f"count d5.void_casts: the register says {stale}" in p for p in problems), problems


def test_a_planted_void_cast_needs_its_row(tree: Path) -> None:
    plant(
        tree,
        "planted.cpp",
        "void f() { (void)planted::discard( 1 ); (void)std::copy_n( a, 1, b ); }\n",
    )
    problems = reg.differences(register_text(), tree)
    assert any("(void) planted::discard() (1) has no row in D5" in p for p in problems), problems
    assert any("D5 counts (void) std::copy_n()" in p for p in problems), problems


# --- 6.9.2: the standard integer type names (FW-23) ------------------------------------

#: Each line marked ``// NAMES <spelling>`` holds one name the reader must
#: find; no other line holds one. A cast in a call's arguments is a name like
#: any other since FW-26 took the printf formats away.
INTEGER_NAMES = """\
int a = 0;  // NAMES int
std::int32_t b = 0;
using Count = unsigned long;
typedef long Wide;
// int in a comment, unsigned too
const char* s = "int unsigned long";
unsigned char byte = 0;  // NAMES unsigned_char
signed char tiny = 0;  // NAMES signed_char
char c = 'a';
long long big = 0;  // NAMES long_long
const int written = count( out );  // NAMES int
w.unsignedDecimal( static_cast<unsigned>( x ) );  // NAMES unsigned
w.unsignedDecimal( static_cast<unsigned long long>( z ) );  // NAMES unsigned_long_long
std::uint64_t q = static_cast<unsigned long>( r );  // NAMES unsigned_long
"""


def test_every_integer_type_name_is_found() -> None:
    expected = set()
    for number, line in enumerate(INTEGER_NAMES.splitlines(), 1):
        match = re.search(r"// NAMES (\S+)", line)
        if match:
            expected.add((number, match.group(1).replace("_", " ")))
    found = {(n.line, n.spelled) for n in reg.integer_names_in("t.cpp", INTEGER_NAMES)}
    assert found == expected


def test_the_sources_name_no_integer_type() -> None:
    names = reg.all_integer_names()
    assert [f"{n.file}:{n.line}: {n.spelled}" for n in names] == []


def test_a_planted_integer_type_name_fails(tree: Path) -> None:
    plant(tree, "planted.cpp", "namespace x {\nunsigned counter = 0U;\n}\n")
    problems = reg.differences(register_text(), tree)
    assert any("planted.cpp:3: unsigned" in p and "(6.9.2)" in p for p in problems), problems


def test_a_planted_cast_in_a_calls_arguments_fails(tree: Path) -> None:
    plant(
        tree,
        "planted.cpp",
        "void f( Writer& w, std::int64_t v ) {\n"
        "    w.signedDecimal( static_cast<long>( v ) );\n}\n",
    )
    problems = reg.differences(register_text(), tree)
    assert any("planted.cpp:3: long" in p and "(6.9.2)" in p for p in problems), problems


def register_versions() -> list[tuple[str, str]]:
    """docs/misra.md at each commit that changed it, oldest first, then the working tree."""
    try:
        listing = subprocess.run(
            ["git", "log", "--format=%H", "--reverse", "--", "docs/misra.md"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        listing = []
    versions = []
    for commit in listing:
        shown = subprocess.run(
            ["git", "show", f"{commit}:docs/misra.md"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if shown.returncode == 0:
            versions.append((commit[:12], shown.stdout))
    versions.append(("working tree", register_text()))
    return versions


def count_history(key: str, versions: list[tuple[str, str]]) -> list[tuple[str, int]]:
    """The count *key* at each version of the register that states it."""
    history = []
    for where, text in versions:
        try:
            counts = reg.register_counts(text)
        except KeyError:
            continue  # before FW-20: no counts table
        if key in counts:
            history.append((where, counts[key]))
    return history


def rises(history: list[tuple[str, int]]) -> list[str]:
    """Each step of *history* where the count rose."""
    return [
        f"{b[0]}: {a[1]} -> {b[1]}"
        for a, b in zip(history, history[1:], strict=False)
        if b[1] > a[1]
    ]


def test_the_printf_integer_names_fell_to_none_and_their_count_went_with_d2() -> None:
    """FW-23 to FW-26: the names the formats needed only fell, and FW-26 took the
    last of them and the count with D2; the working tree states it no more.

    The versions that stated the count are older than the published history,
    which starts at one commit; any version the history holds that states it
    must not show it rising."""
    versions = register_versions()
    working_tree = versions[-1][1]
    assert "d2.integer_names" not in reg.register_counts(working_tree), (
        "D2 is gone: its count is stated no more"
    )
    history = count_history("d2.integer_names", versions)
    assert all(where != "working tree" for where, _ in history)
    assert rises(history) == []


def test_a_rise_in_a_ratcheted_count_is_seen() -> None:
    assert rises([("a", 118), ("b", 90), ("c", 91)]) == ["c: 90 -> 91"]
    assert count_history("d2.integer_names", [("x", "no table here")]) == []


def test_the_callee_is_named_as_d5_names_it() -> None:
    assert reg.callee_of("std :: copy_n") == "std::copy_n"
    assert reg.callee_of("self->alertEdges_.fetch_add") == ".fetch_add"
    assert reg.callee_of("device_->readDiagAlrt") == ".readDiagAlrt"
    assert reg.callee_of("recover") == "recover"


def test_the_code_reader_keeps_lines_and_digit_separators() -> None:
    text = 'int a = 1\'000; // c\n/* x\ny */ char q = \'"\';\nconst char* s = "a\\"b";\n'
    code = reg.code_of(text)
    assert code.count("\n") == text.count("\n")
    assert "1'000" in code
    assert "c\n" not in code.split("\n")[0]
    assert '"' in code.split("\n")[3] and "a" not in code.split("\n")[3].split("=")[1]


def test_the_script_checks_clean_from_the_command_line() -> None:
    assert reg.main(["--check"]) == 0


@pytest.mark.parametrize("key", ["src.sources", "src.static_tasks"])
def test_a_stale_source_or_task_count_fails(key: str) -> None:
    """The register's statements of how many sources and static tasks there are
    (the compile-flag and clang-tidy rows, 21.6.1) are held to the tree."""
    counts = reg.counts()
    stale = counts[key] + 1
    text = register_text()
    row = next(line for line in text.splitlines() if line.startswith(f"| `{key}` |"))
    text = text.replace(row, row.rsplit("|", 2)[0] + f"| {stale} |")
    problems = reg.differences(text)
    assert any(f"count {key}: the register says {stale}" in p for p in problems), problems


def test_the_stated_source_count_is_the_components() -> None:
    """Every sentence of the register that counts the sources says the count."""
    text = register_text()
    sources = reg.counts()["src.sources"]
    stated = {int(n) for n in re.findall(r"[Ee]ach of the (\d+) sources", text)}
    assert stated == {sources}, stated


# --- D7's linker wraps ------------------------------------------------------------------


def test_every_wrap_the_sources_define_is_named_in_d7() -> None:
    """The review of 2026-11-24 added wraps D7 never named (HM2-07)."""
    defined = reg.wrap_definitions()
    assert "__wrap_xQueueCreateMutex" in defined and "__wrap_psa_crypto_init" in defined
    assert reg.register_wraps(register_text()) == set(defined)


def test_a_planted_wrap_without_its_name_in_d7_fails(tree: Path) -> None:
    plant(
        tree,
        "planted.cpp",
        'extern "C" int __wrap_planted_call( int value ) noexcept {\n  return value;\n}\n',
    )
    problems = reg.differences(register_text(), tree)
    assert any("__wrap_planted_call" in p and "D7" in p for p in problems), problems


def test_a_wrap_d7_names_that_no_source_defines_fails() -> None:
    text = register_text()
    marker = "### D7. Names fixed by others (no rule; naming)\n\n"
    assert marker in text
    text = text.replace(marker, marker + "`__wrap_retired_call()` is planted. ")
    problems = reg.differences(text)
    assert any("__wrap_retired_call" in p and "D7" in p for p in problems), problems


def test_a_declaration_or_a_comment_is_not_a_wrap_definition() -> None:
    text = (
        "// __wrap_in_a_comment() is not defined here\n"
        'extern "C" int __wrap_declared( int v ) noexcept;\n'
        'extern "C" int __wrap_defined( int v ) noexcept { return v; }\n'
    )
    assert reg.wraps_in(text) == {"__wrap_defined"}
