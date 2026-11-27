# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The image's translation units read together: nothing unreferenced, the decays counted.

``scripts/ast_census.py`` parses every unit of ``firmware/src/CMakeLists.txt``
with clang and reads its AST: a function of the firmware's own that no unit
references fails (cppcheck's ``unusedFunction``, suppressed until FW-20, read
one file at a time and could not follow the console's member-function
pointers), but the test seams the script names; and the array decays of
MISRA C++:2023 7.11.2 and the C arrays of 11.3.1 are counted and held to
docs/misra.md's "The counts", the decays to a number that only falls over the
page's history.

These tests run the census once over the harness leg (the device leg needs
the device's compile database and gives the same counts; ``--leg device``),
and prove the reader on planted units it compiles itself. They need a clang++
(``$METER_CLANG``, ``$LLVM_BIN``, Homebrew's LLVM or the PATH's), which
``scripts/run_tests.sh`` already requires for the MC/DC coverage and exports.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ast_census.py"
REGISTER = ROOT / "docs" / "misra.md"
sys.path.insert(0, str(ROOT / "scripts"))


def load_census() -> ModuleType:
    """Import scripts/ast_census.py as a module."""
    spec = importlib.util.spec_from_file_location("ast_census", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ast_census"] = module
    spec.loader.exec_module(module)
    return module


census_module = load_census()
import misra_register  # noqa: E402  (the register's table reader)


@pytest.fixture(scope="module")
def clang() -> str:
    """The clang++ the census runs; its absence is a failure, not a skip."""
    found = census_module.find_clang()
    if found is None:
        pytest.fail(
            "no clang++ (METER_CLANG, LLVM_BIN, Homebrew's LLVM, the PATH): the census cannot run"
        )
    clang_path: str = found
    return clang_path


@pytest.fixture(scope="module")
def image(clang: str) -> Any:
    """The census of the image's translation units, harness leg."""
    return census_module.run_units(clang, census_module.image_units("harness"))


def flags() -> list[str]:
    """The flags a planted unit is compiled with: the harness leg's language."""
    harness: list[str] = census_module.clang_tidy_gate.harness_flags(["x.cpp"])["x.cpp"]
    return harness


def planted(clang: str, tmp_path: Path, units: dict[str, str]) -> Any:
    """The census of *units* (file name to text), all in *tmp_path*, which is the own code."""
    paths = {}
    for name, text in units.items():
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        if name.endswith(".cpp"):
            paths[path] = [*flags(), f"-I{tmp_path}"]
    return census_module.run_units(clang, paths, own=tmp_path)


# --- The image ---------------------------------------------------------------------


def test_every_unit_of_the_image_parses(image: Any) -> None:
    assert image.failures == []
    assert len(image.functions) > 500, "the census read almost nothing: the dump filter is wrong"


def test_no_own_function_is_unreferenced(image: Any) -> None:
    assert [f.name for f in image.unreferenced()] == []


def test_every_test_seam_is_one(image: Any) -> None:
    assert census_module.seam_problems(image) == []


def test_the_counts_are_the_registers(image: Any) -> None:
    listed = misra_register.register_counts(REGISTER.read_text(encoding="utf-8"))
    found = census_module.as_json(image)
    expected = {
        key: listed.get(key)
        for key in ("d3.arrays", "d3.decays", "d3.literal_decays", "src.lambdas")
    }
    assert expected == {
        "d3.arrays": found["arrays"],
        "d3.decays": found["decays"],
        "d3.literal_decays": found["literal_decays"],
        "src.lambdas": found["lambdas"],
    }


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
    versions.append(("working tree", REGISTER.read_text(encoding="utf-8")))
    return versions


def decay_history(versions: list[tuple[str, str]], key: str = "d3.decays") -> list[tuple[str, int]]:
    """A ratchet's count (*key*) at each version that states it."""
    history = []
    for where, text in versions:
        try:
            counts = misra_register.register_counts(text)
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


def test_the_decay_count_only_falls_over_the_registers_history() -> None:
    history = decay_history(register_versions())
    assert history, "the working tree's register states no d3.decays"
    assert rises(history) == []


def test_the_c_array_count_only_falls_over_the_registers_history() -> None:
    """D3's C arrays (11.3.1) are a ratchet too since the review of 2026-11-24:
    a std::array or a span replaces one, nothing adds one."""
    history = decay_history(register_versions(), "d3.arrays")
    assert history, "the working tree's register states no d3.arrays"
    assert rises(history) == []


def test_a_rise_in_the_decay_count_is_seen() -> None:
    assert rises([("a", 182), ("b", 180), ("c", 181)]) == ["c: 180 -> 181"]


# --- Planted units -------------------------------------------------------------------


def test_a_planted_unreferenced_function_is_reported(clang: str, tmp_path: Path) -> None:
    result = planted(
        clang,
        tmp_path,
        {
            "a.cpp": (
                "namespace kleidos::meter {\n"
                "int used() noexcept { return 1; }\n"
                "int forgotten() noexcept { return 2; }\n"
                "namespace { int hidden() noexcept { return 3; } }\n"
                "}\n"
                "int entry() noexcept { return kleidos::meter::used(); }\n"
            )
        },
    )
    assert result.failures == []
    names = sorted(f.name for f in result.unreferenced())
    assert names == [
        "kleidos::meter::(anonymous namespace)::hidden",
        "kleidos::meter::forgotten",
    ]


def test_what_another_unit_a_table_or_a_constant_uses_is_referenced(
    clang: str, tmp_path: Path
) -> None:
    result = planted(
        clang,
        tmp_path,
        {
            "m.h": (
                "namespace kleidos::meter {\n"
                "struct Console { void cmd() noexcept; void other() noexcept; };\n"
                "using Handler = void ( Console::* )() noexcept;\n"
                "Handler pick( int i ) noexcept;\n"
                "int remote() noexcept;\n"
                "constexpr int folded( int x ) noexcept { return x + 1; }\n"
                "template <int N> constexpr int each() noexcept { return N; }\n"
                "}\n"
            ),
            "a.cpp": (
                '#include "m.h"\n'
                "namespace kleidos::meter {\n"
                "void Console::cmd() noexcept {}\n"
                "void Console::other() noexcept {}\n"
                "constexpr Handler kTable[] = { &Console::cmd, &Console::other };\n"
                'static_assert( folded( 1 ) == 2, "" );\n'
                "int remote() noexcept { return each<3>(); }\n"
                "Handler pick( int i ) noexcept { return kTable[i]; }\n"
                "}\n"
            ),
            "b.cpp": (
                '#include "m.h"\n'
                "int entry() noexcept {\n"
                "    (void)kleidos::meter::pick( 0 );\n"
                "    return kleidos::meter::remote();\n"
                "}\n"
            ),
        },
    )
    assert result.failures == []
    assert [f.name for f in result.unreferenced()] == []


def test_decays_are_counted_once_per_place_and_literals_apart(clang: str, tmp_path: Path) -> None:
    header = (
        "namespace kleidos::meter {\n"
        "void take( const char* text ) noexcept;\n"
        "void sum( const int* values, int count ) noexcept;\n"
        "inline void both() noexcept { int v[2] = { 1, 2 }; sum( v, 2 ); }\n"
        "}\n"
    )
    result = planted(
        clang,
        tmp_path,
        {
            "d.h": header,
            "a.cpp": (
                '#include "d.h"\n'
                "namespace kleidos::meter {\n"
                "void take( const char* ) noexcept {}\n"
                "void sum( const int*, int ) noexcept {}\n"
                "void f() noexcept {\n"
                "    char buffer[4] = {};\n"
                "    take( buffer );\n"
                '    take( "literal" );\n'
                "    both();\n"
                "}\n"
                "}\n"
            ),
            "b.cpp": '#include "d.h"\nnamespace kleidos::meter { void g() noexcept { both(); } }\n',
        },
    )
    assert result.failures == []
    # buffer in f(), v in both() (one place, though both units include it).
    assert len(result.decays) == 2
    assert len(result.literal_decays) == 1
    # buffer and v, and nothing else (the parameters are pointers).
    assert len(result.arrays) == 2


def test_a_unit_that_does_not_parse_is_a_failure(clang: str, tmp_path: Path) -> None:
    result = planted(
        clang, tmp_path, {"bad.cpp": "namespace kleidos::meter { int f() { return }\n"}
    )
    assert result.failures and "bad.cpp" in result.failures[0]


def test_a_stale_test_seam_is_reported(image: Any) -> None:
    seams = dict(census_module.TEST_SEAMS)
    try:
        census_module.TEST_SEAMS["kleidos::meter::Console::begin"] = "planted"
        census_module.TEST_SEAMS["kleidos::meter::nothing::here"] = "planted"
        problems = census_module.seam_problems(image)
    finally:
        census_module.TEST_SEAMS.clear()
        census_module.TEST_SEAMS.update(seams)
    assert any("Console::begin" in p and "the image references it" in p for p in problems), problems
    assert any("nothing::here" in p and "declare no such function" in p for p in problems), problems


def test_the_gate_reports_none_from_the_command_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert census_module.main([]) == 0
    out = capsys.readouterr().out
    assert re.search(r"0 unreferenced, 5 test seams", out), out


def test_a_seam_can_name_one_overload(image: Any) -> None:
    """A seam with parameter types names that overload alone (FW-22)."""
    seams = dict(census_module.TEST_SEAMS)
    try:
        # The span overload of choice() is the one the image calls.
        census_module.TEST_SEAMS[
            "kleidos::meter::scpi::Parameters::choice"
            "(std::size_t, std::span<const std::string_view>, std::size_t &)"
        ] = "planted"
        problems = census_module.seam_problems(image)
    finally:
        census_module.TEST_SEAMS.clear()
        census_module.TEST_SEAMS.update(seams)
    assert any("image references it" in p and "std::span" in p for p in problems), problems
    assert census_module.parameters_of("bool (int, char (*)[2]) noexcept") == "(int, char (*)[2])"
    assert census_module.parameters_of("int") == ""
