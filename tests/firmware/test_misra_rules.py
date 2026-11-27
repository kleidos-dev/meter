# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The MISRA C++:2023 rules the firmware holds by a gate of its own (FW-21).

``scripts/misra_rules.py`` reads the image's translation units from clang's
AST for 5.13.4 (an unsigned literal's ``U``), 7.0.3 (no conversion between
a character type and another type, FW-22), 7.0.5 (no operand whose
signedness promotion or the usual arithmetic conversions change, FW-23), 8.0.1 (the conditional
operator's operands parenthesized), 8.1.2 (no default capture), 8.14.1 (no
side effect in the right operand of ``&&`` and ``||``), 8.19.1 (no comma
operator), 9.4.1 (every ``if ... else if`` chain ends in an ``else``) and
9.5.1 (every legacy ``for`` simple), and the sources' text for 19.6.1 (no
pragma; every header guarded by ``KLEIDOS_METER_<FILE>_H``), 8.2.2's
FreeRTOS tick macros, 21.2.2 (no string handling function of the C
library and no ``<cstring>``, FW-22), 21.10.1 (no ``<cstdarg>`` and no C
variadic function) and 30.0.1 (no formatted output of the C library and no
``<cstdio>``, FW-26). docs/misra.md lists each rule as held with this gate.

The image must hold no finding, every rule must fail on a planted case and
pass its compliant twin, and the lists the script keeps (the side-effect-free
calls, the dump filters) must each still be needed. They need a clang++, as
``tests/firmware/test_ast_census.py`` does; the device leg also needs the
device's compile database, and skips without it (``conftest.py``).
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "misra_rules.py"
SOURCES = ROOT / "firmware" / "src"
REGISTER = ROOT / "docs" / "misra.md"
sys.path.insert(0, str(ROOT / "scripts"))


def load_rules() -> ModuleType:
    """Import scripts/misra_rules.py as a module."""
    spec = importlib.util.spec_from_file_location("misra_rules", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["misra_rules"] = module
    spec.loader.exec_module(module)
    return module


rules = load_rules()


@pytest.fixture(scope="module")
def clang() -> str:
    """The clang++ the gate runs; its absence is a failure, not a skip."""
    found = rules.ast_census.find_clang()
    if found is None:
        pytest.fail(
            "no clang++ (METER_CLANG, LLVM_BIN, Homebrew's LLVM, the PATH): the gate cannot run"
        )
    clang_path: str = found
    return clang_path


@pytest.fixture(scope="module")
def image(clang: str) -> Any:
    """The rules over the image's translation units, harness leg."""
    return rules.run_units(clang, rules.ast_census.image_units("harness"))


# --- The image -----------------------------------------------------------------


def test_every_unit_parses_and_is_read(image: Any) -> None:
    assert image.failures == []
    assert image.nodes > 50000, "the gate read almost nothing: the dump filters are wrong"


def test_the_image_holds_no_finding(image: Any) -> None:
    assert [str(f) for f in image.sorted()] == []


def test_the_device_leg_holds_no_finding(clang: str, built: Callable[[Path, str], Path]) -> None:
    """The sources as the image compiles them: the code under ``ESP_PLATFORM`` too.

    It reads the device's compile database (``pio run --project-dir firmware
    -e meter -t compiledb``), and skips on a checkout that was never built.
    """
    built(ROOT / "firmware" / "compile_commands.json", "pio run ... -t compiledb")
    reading = rules.run_units(clang, rules.ast_census.image_units("device"))
    assert reading.failures == []
    assert [str(f) for f in reading.sorted()] == []


def test_the_text_rules_find_nothing_in_the_sources() -> None:
    assert [str(f) for f in rules.text_reading()] == []


def test_every_c_linkage_definition_is_read() -> None:
    assert rules.c_linkage_definitions(), "the sources define app_main(), at least"
    assert rules.unfiltered() == []


def test_every_side_effect_free_call_is_still_needed(image: Any) -> None:
    """A name the image no longer calls in a right operand leaves the list."""
    assert sorted(image.pure_used) == sorted(rules.PURE_CALLS)


def test_no_recursion_is_suppressed() -> None:
    """8.2.10 is clang-tidy's misc-no-recursion, with no NOLINT left (FW-21)."""
    config = (ROOT / ".clang-tidy").read_text(encoding="utf-8")
    assert re.search(r"^\s*misc-no-recursion,", config, re.M)
    for path in sorted((ROOT / "firmware").rglob("*")):
        if path.suffix in (".cpp", ".h") and ".pio" not in path.parts:
            assert "misc-no-recursion" not in path.read_text(encoding="utf-8"), path


def test_the_register_lists_each_rule_with_this_gate() -> None:
    text = REGISTER.read_text(encoding="utf-8")
    held = text[text.index("## Held, by the gates and by reading") : text.index("## Deviations")]
    for rule in rules.RULES:
        assert re.search(rf"\*\*{re.escape(rule)}\b", held), f"docs/misra.md holds no {rule}"
    assert "scripts/misra_rules.py" in held
    assert "### D4." not in text, "D4 went with the recursion (FW-21)"


# --- Planted cases ---------------------------------------------------------------

#: Each line marked ``// FINDS <rule>`` must be found for that rule, and no
#: other line for any rule: the compliant twins sit beside the planted cases.
PLANTED = """\
#include <array>
#include <atomic>
#include <compare>
#include <cstdint>
#include <string>
#include <string_view>

namespace kleidos::meter {

int counter = 0;

bool bump() noexcept {
    ++counter;
    return true;
}

constexpr bool positive( int x ) noexcept {
    return x > 0;
}

struct Box {
    int  value = 0;
    bool full() const noexcept { return value > 3; }
    bool take() noexcept { return ++value > 3; }
};

std::uint32_t literal() noexcept {
    return 0x80000000;  // FINDS 5.13.4
}

std::uint32_t literalSuffixed() noexcept {
    return 0x80000000U;
}

int conditional( int a, int b ) noexcept {
    return a == b ? a : b;  // FINDS 8.0.1
}

int conditionalArm( bool pick, int a, int b ) noexcept {
    return pick ? a : a + b;  // FINDS 8.0.1
}

int conditionalParenthesized( int a, int b ) noexcept {
    return ( a == b ) ? a : ( a + b );
}

struct Cursor {
    int  at = 0;
    bool operator==( const Cursor& ) const noexcept = default;
    auto operator<=>( const Cursor& ) const noexcept = default;
    Cursor operator+( int step ) const noexcept { return Cursor{ at + step }; }
};

int conditionalOverloaded( Cursor a, Cursor b ) noexcept {
    return a == b ? 1 : 2;  // FINDS 8.0.1
}

int conditionalRewritten( Cursor a, Cursor b ) noexcept {
    return a != b ? 1 : 2;  // FINDS 8.0.1
}

int conditionalSpaceship( Cursor a, Cursor b ) noexcept {
    return a < b ? 1 : 2;  // FINDS 8.0.1
}

Cursor conditionalOverloadedArm( bool pick, Cursor a ) noexcept {
    return pick ? a : a + 1;  // FINDS 8.0.1
}

Cursor conditionalOverloadedParenthesized( Cursor a, Cursor b ) noexcept {
    return ( a == b ) ? ( a + 1 ) : ( ( a < b ) ? a : b );
}

struct Context {
    int value = 0;
};

int fromVoidPointer( void* context ) noexcept {
    return static_cast<Context*>( context )->value;
}

int* fromInteger( unsigned long address ) noexcept {
    return reinterpret_cast<int*>( address );  // FINDS 8.2.6
}

int* fromNull() noexcept {
    return static_cast<int*>( nullptr );
}

using Callback = void ( * )();

Callback toFunctionPointer( void* entry ) noexcept {
    return reinterpret_cast<Callback>( entry );
}

int lambdaByDefault( int a ) noexcept {
    const auto f = [&]() noexcept { return a; };  // FINDS 8.1.2
    return f();
}

int lambdaExplicit( int a ) noexcept {
    const auto f = [a]() noexcept { return a; };
    return f();
}

bool sideEffect( bool a ) noexcept {
    return a && bump();  // FINDS 8.14.1
}

bool sideEffectMember( bool a, Box& box ) noexcept {
    return a || box.take();  // FINDS 8.14.1
}

bool sideEffectAssigns( bool a, int& b ) noexcept {
    return a && ( b = 1 ) > 0;  // FINDS 8.14.1
}

bool noSideEffect( bool a, int x, const Box& box ) noexcept {
    return a && positive( x ) && box.full();
}

struct Store {
    int  loaded = 0;
    bool load() noexcept {
        loaded = loaded + 1;
        return loaded > 0;
    }
    bool empty() noexcept {
        loaded = 0;
        return true;
    }
};

bool ownLoad( bool a, Store& store ) noexcept {
    return a && store.load();  // FINDS 8.14.1
}

bool ownEmpty( bool a, Store& store ) noexcept {
    return a || store.empty();  // FINDS 8.14.1
}

bool atomicLoad( bool a, const std::atomic<bool>& flag ) noexcept {
    return a && flag.load();
}

bool viewEmpty( bool a, std::string_view text, const std::array<int, 2>& pair ) noexcept {
    return a || text.empty() || ( pair.size() > 1U );
}

int comma( int a ) noexcept {
    int b = 0;
    return ( b = a, b );  // FINDS 8.19.1
}

int chain( int a ) noexcept {
    if ( a == 1 ) {  // FINDS 9.4.1
        return 1;
    } else if ( a == 2 ) {
        return 2;
    }
    return 0;
}

int chainEnded( int a ) noexcept {
    if ( a == 1 ) {
        return 1;
    } else if ( a == 2 ) {
        return 2;
    } else {
        return 0;
    }
}

int plainIf( int a ) noexcept {
    if ( a == 1 ) {
        return 1;
    } else {
        return 0;
    }
}

int loopSimple( int n ) noexcept {
    int sum = 0;
    for ( int i = 0; i < n; ++i ) {
        sum += i;
    }
    for ( unsigned j = 10U; j > 0U; j -= 2U ) {
        sum += 1;
    }
    return sum;
}

int loopCounterModified( int n ) noexcept {
    int sum = 0;
    for ( int i = 0; i < n; ++i ) {  // FINDS 9.5.1
        ++i;
        sum += i;
    }
    return sum;
}

int loopBoundModified( int n ) noexcept {
    int sum = 0;
    for ( int i = 0; i < n; ++i ) {  // FINDS 9.5.1
        --n;
        sum += i;
    }
    return sum;
}

int loopTwoConditions( int n, int cap ) noexcept {
    int sum = 0;
    for ( int i = 0; i < n && sum < cap; ++i ) {  // FINDS 9.5.1
        sum += i;
    }
    return sum;
}

int loopForever() noexcept {
    for ( ;; ) {  // FINDS 9.5.1
        return 1;
    }
}

int loopNoInit( int n ) noexcept {
    int i = 0;
    for ( ; i < n; ++i ) {  // FINDS 9.5.1
    }
    return i;
}

int digitArithmetic( char c ) noexcept {
    return c - '0';  // FINDS 7.0.3
}

bool characterOrder( char c ) noexcept {
    return c >= 'a';  // FINDS 7.0.3
}

char characterFromInt( int code ) noexcept {
    return static_cast<char>( code );  // FINDS 7.0.3
}

unsigned char byteFromCharacter( char c ) noexcept {
    return static_cast<unsigned char>( c );  // FINDS 7.0.3
}

bool characterAsCondition( char c ) noexcept {
    return !c;  // FINDS 7.0.3
}

bool sameCharacters( char a, char b ) noexcept {
    return ( a == b ) && ( a != 'x' );
}

int characterThroughTraits( char c ) noexcept {
    return std::char_traits<char>::to_int_type( c );
}

char characterCopied( const char* text ) noexcept {
    const char c = text[0];
    return c;
}

bool narrowFlag( std::uint8_t flags, std::uint8_t mask ) noexcept {
    return ( flags & mask ) != 0U;  // FINDS 7.0.5
}

bool wideMask( std::uint8_t flags, std::uint32_t mask ) noexcept {
    return ( flags & mask ) != 0U;
}

bool unsuffixedLiteral( std::uint32_t value ) noexcept {
    return value > 3;  // FINDS 7.0.5
}

bool suffixedLiteral( std::uint32_t value ) noexcept {
    return value > 3U;
}

std::uint32_t narrowShift( std::uint16_t word ) noexcept {
    return static_cast<std::uint32_t>( word >> 4U );  // FINDS 7.0.5
}

std::uint32_t widenedShift( std::uint16_t word ) noexcept {
    return static_cast<std::uint32_t>( word ) >> 4U;
}

std::uint8_t narrowComplement( std::uint8_t value ) noexcept {
    return static_cast<std::uint8_t>( ~value );  // FINDS 7.0.5
}

bool signedMeetsUnsigned( std::int64_t a, std::uint32_t b ) noexcept {
    return a < b;  // FINDS 7.0.5
}

bool bothSigned( std::int64_t a, std::uint32_t b ) noexcept {
    return a < static_cast<std::int64_t>( b );
}

void narrowCompound( std::uint8_t& flags, std::uint8_t bit ) noexcept {
    flags |= bit;  // FINDS 7.0.5
}

void wideCompound( std::uint32_t& flags, std::uint8_t bit ) noexcept {
    flags |= bit;
}

}  // namespace kleidos::meter
"""


def expected_marks(text: str) -> set[tuple[str, int]]:
    """The (rule, line) pairs *text*'s ``// FINDS`` marks ask for."""
    marks = set()
    for number, line in enumerate(text.splitlines(), 1):
        match = re.search(r"// FINDS (\S+)", line)
        if match:
            marks.add((match.group(1), number))
    return marks


def test_each_rule_fails_its_planted_case_and_passes_its_twin(clang: str, tmp_path: Path) -> None:
    source = tmp_path / "planted.cpp"
    source.write_text(PLANTED, encoding="utf-8")
    flags = rules.ast_census.clang_tidy_gate.harness_flags(["x.cpp"])["x.cpp"]
    reading = rules.run_units(clang, {source: [*flags, f"-I{tmp_path}"]}, own=tmp_path)
    assert reading.failures == []
    found = {(f.rule, f.line) for f in reading.findings.values()}
    assert found == expected_marks(PLANTED)
    assert {rule for rule, _ in found} == set(rules.RULES) - {
        "8.2.2",
        "19.6.1",
        "21.2.2",
        "21.10.1",
        "30.0.1",
    }
    # wideMask()'s flags and wideCompound()'s bit: narrow unsigned operands
    # converted straight to a wider unsigned type, which D12 counts.
    assert len(reading.widened) == 2
    # fromVoidPointer()'s cast: D13's kind, counted rather than found.
    assert len(reading.void_pointer_casts) == 1


def test_the_widened_narrow_operands_are_d12s_count(image: Any) -> None:
    """D12 states how many narrow unsigned operands the image widens in one step."""
    counts = rules.misra_register.register_counts(REGISTER.read_text(encoding="utf-8"))
    assert len(image.widened) == counts["d12.widened_operands"]


def _matched(found: re.Match[str] | None) -> re.Match[str]:
    """*found*, which the table's own rows guarantee."""
    assert found is not None
    return found


def d13_sites(text: str) -> list[str]:
    """The file of each row of D13's table, once per cast."""
    section = text[text.index("### D13.") :]
    section = re.split(r"\n##+ ", section[1:], maxsplit=1)[0]
    return sorted(
        _matched(re.match(r"\| `([^`]+)` \|", line)).group(1)
        for line in section.splitlines()
        if re.match(r"\| `[^`]+\.cpp` \|", line)
    )


def test_the_void_pointer_casts_are_d13s_rows(image: Any) -> None:
    """D13 lists every cast of a callback's void* context to an object pointer
    (8.2.6), one row each, and the count under "The counts" is theirs."""
    text = REGISTER.read_text(encoding="utf-8")
    counts = rules.misra_register.register_counts(text)
    found = sorted(Path(file).name for file, _ in image.void_pointer_casts)
    assert found == d13_sites(text)
    assert len(image.void_pointer_casts) == counts["d13.void_pointer_casts"]


def test_a_call_through_a_pointer_is_a_side_effect(clang: str, tmp_path: Path) -> None:
    source = tmp_path / "pointer.cpp"
    source.write_text(
        "namespace kleidos::meter {\n"
        "bool viaPointer( bool a, bool ( *check )() ) noexcept {\n"
        "    return a && check();\n"
        "}\n"
        "}  // namespace kleidos::meter\n",
        encoding="utf-8",
    )
    flags = rules.ast_census.clang_tidy_gate.harness_flags(["x.cpp"])["x.cpp"]
    reading = rules.run_units(clang, {source: flags}, own=tmp_path)
    assert [(f.rule, f.line) for f in reading.findings.values()] == [("8.14.1", 3)]


# --- The text rules --------------------------------------------------------------

GUARDED = """\
// SPDX-License-Identifier: GPL-3.0-or-later
/// @file box_size.h
#ifndef KLEIDOS_METER_BOX_SIZE_H
#define KLEIDOS_METER_BOX_SIZE_H

constexpr int kBox = 1;

#endif  // KLEIDOS_METER_BOX_SIZE_H
"""


def text_rules(name: str, text: str) -> list[tuple[str, int]]:
    """The textual findings for *text* as the file *name*, as (rule, line)."""
    return [(f.rule, f.line) for f in rules.text_findings(Path(name), text, name)]


def test_the_guard_name_is_made_from_the_file_name() -> None:
    assert rules.guard_name(Path("ina228_regs.h")) == "KLEIDOS_METER_INA228_REGS_H"
    assert rules.guard_name(Path("scale.h")) == "KLEIDOS_METER_SCALE_H"


def test_a_guarded_header_passes() -> None:
    assert text_rules("box_size.h", GUARDED) == []


def test_pragma_once_fails() -> None:
    planted = GUARDED.replace(
        "#ifndef KLEIDOS_METER_BOX_SIZE_H\n#define KLEIDOS_METER_BOX_SIZE_H\n", "#pragma once\n"
    )
    planted = planted.replace("#endif  // KLEIDOS_METER_BOX_SIZE_H\n", "")
    assert ("19.6.1", 3) in text_rules("box_size.h", planted)
    assert {rule for rule, _ in text_rules("box_size.h", planted)} == {"19.6.1"}


def test_a_pragma_in_a_source_fails() -> None:
    assert text_rules("unit.cpp", "int a;\n#pragma GCC diagnostic push\n") == [("19.6.1", 2)]
    assert text_rules("unit.cpp", 'int a;\n_Pragma( "once" )\n') == [("19.6.1", 2)]


def test_a_pragma_in_a_comment_or_a_string_passes() -> None:
    assert text_rules("unit.cpp", '// #pragma once\nconst char* k = "#pragma";\n') == []


def test_a_guard_of_another_name_or_no_closing_comment_fails() -> None:
    wrong = GUARDED.replace("KLEIDOS_METER_BOX_SIZE_H", "BOX_SIZE_H")
    assert text_rules("box_size.h", wrong) == [("19.6.1", 3)]
    unclosed = GUARDED.replace("#endif  // KLEIDOS_METER_BOX_SIZE_H", "#endif")
    assert text_rules("box_size.h", unclosed) == [("19.6.1", 3)]
    late = GUARDED.replace("constexpr int kBox = 1;", "constexpr int kBox = 1;\n#include <cstdint>")
    late = late.replace(
        "#ifndef KLEIDOS_METER_BOX_SIZE_H\n",
        "#include <cstddef>\n#ifndef KLEIDOS_METER_BOX_SIZE_H\n",
    )
    assert text_rules("box_size.h", late) == [("19.6.1", 3)]


@pytest.mark.parametrize("macro", sorted(rules.BANNED_MACROS))
def test_a_banned_tick_macro_fails(macro: str) -> None:
    planted = f"void wait() {{\n    vTaskDelay( {macro}( 1 ) );\n}}\n"
    assert text_rules("unit.cpp", planted) == [("8.2.2", 2)]
    assert text_rules("unit.cpp", f"// {macro} is banned\n") == []


def test_the_tick_helpers_replace_the_macros() -> None:
    header = (SOURCES / "rtos_ticks.h").read_text(encoding="utf-8")
    assert "constexpr TickType_t ticksFromMs(" in header
    assert "constexpr TickType_t kWaitForever" in header
    code = "\n".join(
        rules.misra_register.code_of(p.read_text(encoding="utf-8"))
        for p in rules.misra_register.own_sources()
    )
    assert re.search(r"\brtos::ticksFromMs\(", code)
    assert re.search(r"\brtos::kWaitForever\b", code)


# --- 21.2.2: the C library's string handling (FW-22) -------------------------------


@pytest.mark.parametrize("name", ["strlen", "strcmp", "strncmp", "memcmp", "strtol", "atoi"])
def test_a_c_string_function_fails(name: str) -> None:
    planted = (
        f"bool f( const char* a, const char* b ) {{\n    return std::{name}( a, b ) == 0;\n}}\n"
    )
    assert text_rules("unit.cpp", planted) == [("21.2.2", 2)]


def test_every_banned_function_is_one_of_the_rules_headers() -> None:
    assert {"strlen", "strcmp", "strncmp", "memcpy", "memmove", "memset"} <= (
        rules.BANNED_STRING_FUNCTIONS
    )


@pytest.mark.parametrize("header", ["cstring", "string.h"])
def test_the_c_string_header_fails(header: str) -> None:
    assert text_rules("unit.cpp", f"#include <cstdint>\n#include <{header}>\n") == [("21.2.2", 2)]


def test_a_string_function_named_in_a_comment_or_a_string_passes() -> None:
    planted = '// std::strlen( s ) is banned\nconst char* k = "strcmp( a, b )";\n'
    assert text_rules("unit.cpp", planted) == []
    assert text_rules("unit.cpp", "#include <string>\n#include <string_view>\n") == []
    assert text_rules("unit.cpp", "auto n = view.size();\nauto m = lengthOf( s );\n") == []


def test_the_sources_name_no_c_string_function() -> None:
    """The planted cases above prove the scan; the image's sources hold none."""
    found = [f for f in rules.text_reading() if f.rule == "21.2.2"]
    assert [str(f) for f in found] == []


# --- 21.10.1 and 30.0.1: varargs and formatted output (FW-26) -------------------------


@pytest.mark.parametrize(
    "call", ["std::snprintf( b, 4, x )", "vsnprintf( b, 4, f, a )", "esp_rom_printf( x )"]
)
def test_a_formatted_output_call_fails(call: str) -> None:
    planted = f"void f( char* b ) {{\n    (void){call};\n}}\n"
    assert text_rules("unit.cpp", planted) == [("30.0.1", 2)]


@pytest.mark.parametrize("header", ["cstdio", "stdio.h"])
def test_the_c_stdio_header_fails(header: str) -> None:
    assert text_rules("unit.cpp", f"#include <cstdint>\n#include <{header}>\n") == [("30.0.1", 2)]


@pytest.mark.parametrize("header", ["cstdarg", "stdarg.h"])
def test_the_c_varargs_header_fails(header: str) -> None:
    assert text_rules("unit.cpp", f"#include <array>\n#include <{header}>\n") == [("21.10.1", 2)]


def test_a_variadic_function_and_its_macros_fail() -> None:
    planted = (
        "void f( const char* format, ... ) noexcept {\n"
        "    std::va_list arguments;\n"
        "    va_start( arguments, format );\n"
        "    va_end( arguments );\n"
        "}\n"
    )
    assert sorted(text_rules("unit.cpp", planted)) == [
        ("21.10.1", 1),
        ("21.10.1", 2),
        ("21.10.1", 3),
        ("21.10.1", 4),
    ]


def test_formatted_output_named_in_a_comment_or_a_string_passes() -> None:
    planted = (
        '// std::snprintf( b, 4, "%d", 1 ) is gone\n'
        'const char* k = "printf( x, ... )";\n'
        "w.unsignedDecimal( 4U );\nrespondText( w.view() );\n"
    )
    assert text_rules("unit.cpp", planted) == []
    assert text_rules("unit.cpp", "#include <charconv>\n#include <span>\n") == []


def test_the_sources_hold_no_varargs_and_no_formatted_output() -> None:
    """The planted cases above prove the scan; the image's sources hold none."""
    found = [f for f in rules.text_reading() if f.rule in ("21.10.1", "30.0.1")]
    assert [str(f) for f in found] == []
