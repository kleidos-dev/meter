# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The static analysis cannot pass by analyzing nothing, or by analyzing the wrong thing.

``scripts/run_static_analysis.sh`` runs clang-tidy twice through
``scripts/clang_tidy_gate.py`` (the device's compile commands, and the host
harness) and cppcheck at a pinned release. These tests hold the guards that
make a green run mean something, the firmware repository's: a clang-tidy of
the pinned LLVM major, a configuration ``--verify-config`` accepts, exactly the
CMake source list analyzed, no parse diagnostic, the pinned cppcheck. Each
guard is proved by a planted fault, with a fake binary where the real one is
not needed, so the tests run on any checkout. The run over the real device
build (``--selftest``) needs clang-tidy and the compile database and skips
without them.

They also hold the configuration and the sources to what FW-02 (2026-11-22)
made them: the checks adopted beyond the firmware repository's set, every
discarded result used or cast to void (MISRA C++:2023 0.1.2), and no
``memcpy`` or ``memmove`` in the firmware (21.2.2).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "clang_tidy_gate.py"
SCRIPT = ROOT / "scripts" / "run_static_analysis.sh"
PINS = ROOT / "scripts" / "analysis_toolchain.sh"
CONFIG = ROOT / "firmware" / ".clang-tidy"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"
HOOKS = ROOT / ".pre-commit-config.yaml"
SOURCES = ROOT / "firmware" / "src"
DATABASE = ROOT / "firmware" / "compile_commands.json"

#: The checks FW-02 adopted beyond the firmware repository's set, each with
#: the MISRA C++:2023 rule docs/misra.md maps it to.
ADOPTED = {
    "misc-unused-parameters": "0.2.2",
    "readability-uppercase-literal-suffix": "5.13.5",
    "readability-implicit-bool-conversion": "7.0.1",
    "cppcoreguidelines-pro-type-reinterpret-cast": "8.2.5",
    "readability-isolate-declaration": "10.0.1",
    "readability-inconsistent-declaration-parameter-name": "13.3.3",
    "google-explicit-constructor": "15.1.3",
    "cppcoreguidelines-pro-type-member-init": "15.1.4",
    "cppcoreguidelines-macro-usage": "19.0.2",
    "cppcoreguidelines-special-member-functions": "15.0.1",
}


def load_gate() -> ModuleType:
    """Import scripts/clang_tidy_gate.py as a module."""
    spec = importlib.util.spec_from_file_location("clang_tidy_gate", GATE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["clang_tidy_gate"] = module
    spec.loader.exec_module(module)
    return module


gate = load_gate()


def fake_binary(directory: Path, name: str, script: str) -> Path:
    """Write an executable shell script *name* in *directory* and return its path."""
    path = directory / name
    path.write_text("#!/bin/sh\n" + script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def fake_clang_tidy(directory: Path, version: str = "23.1.4", analysis: str = "") -> Path:
    """A clang-tidy reporting *version*, taking any configuration, printing *analysis* per unit."""
    return fake_binary(
        directory,
        "clang-tidy",
        f"""case "$1" in
  --version) echo 'LLVM (http://llvm.org/):'; echo '  LLVM version {version}'; exit 0 ;;
  --verify-config) echo 'No config errors detected.'; exit 0 ;;
esac
{analysis}
""",
    )


def run_gate(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the gate with *args* and return what it did."""
    return subprocess.run(
        [sys.executable, str(GATE), *args], capture_output=True, text=True, check=False, env=env
    )


# --- The pins ------------------------------------------------------------------


def test_the_pins_are_defined_once_and_read_from_there() -> None:
    pins = gate.toolchain_pins()
    assert pins["METER_CLANG_TIDY_MAJOR"] == pins["METER_CLANG_TIDY_VERSION"].split(".")[0]
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins["METER_CPPCHECK_VERSION"])
    assert re.fullmatch(r"[0-9a-f]{64}", pins["METER_CPPCHECK_SHA256"])
    # No second copy of a version in the script or the workflow: both read the file.
    for path in (SCRIPT, WORKFLOW):
        text = path.read_text(encoding="utf-8")
        assert pins["METER_CPPCHECK_VERSION"] not in text, f"{path.name} restates the cppcheck pin"
        assert pins["METER_CLANG_TIDY_VERSION"] not in text, (
            f"{path.name} restates the clang-tidy pin"
        )
    assert "scripts/analysis_toolchain.sh" in WORKFLOW.read_text(encoding="utf-8")


def test_a_clang_tidy_of_another_major_is_refused(tmp_path: Path) -> None:
    fake = fake_clang_tidy(tmp_path, version="19.1.0")
    result = run_gate("--leg", "harness", "--clang-tidy", str(fake))
    assert result.returncode == gate.GUARD, result.stdout + result.stderr
    assert "LLVM 19.1.0" in result.stderr


def test_a_missing_clang_tidy_is_refused(tmp_path: Path) -> None:
    result = run_gate("--leg", "harness", "--clang-tidy", str(tmp_path / "nothing"))
    assert result.returncode == gate.GUARD


def test_a_rejected_configuration_is_refused(tmp_path: Path) -> None:
    fake = fake_binary(
        tmp_path,
        "clang-tidy",
        """case "$1" in
  --version) echo '  LLVM version 23.1.4'; exit 0 ;;
  --verify-config) echo "unknown check 'hicpp-multiway-paths-covered'"; exit 1 ;;
esac
""",
    )
    result = run_gate("--leg", "harness", "--clang-tidy", str(fake))
    assert result.returncode == gate.GUARD
    assert "--verify-config" in result.stderr


def test_another_cppcheck_is_refused(tmp_path: Path) -> None:
    fake = fake_binary(tmp_path, "cppcheck", "echo 'Cppcheck 2.13.0'\n")
    env = dict(os.environ, CPPCHECK=str(fake))
    result = subprocess.run(
        ["sh", str(SCRIPT), "cppcheck"], capture_output=True, text=True, check=False, env=env
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Cppcheck 2.13.0" in result.stderr


def test_an_unknown_step_is_refused() -> None:
    result = subprocess.run(
        ["sh", str(SCRIPT), "tidyy"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2


# --- What is analyzed ---------------------------------------------------------


def test_the_units_are_the_cmake_source_list() -> None:
    listed = gate.cmake_sources()
    tracked = subprocess.run(
        ["git", "ls-files", "firmware/src/*.cpp"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(f"firmware/src/{name}" for name in listed) == sorted(tracked)
    printed = run_gate("--print-sources").stdout.split()
    assert printed == [f"firmware/src/{name}" for name in listed]


def test_the_cmake_parser_stops_at_the_next_keyword(tmp_path: Path) -> None:
    component = tmp_path / "CMakeLists.txt"
    component.write_text(
        'idf_component_register(\n    SRCS\n        "a.cpp"\n        "b.cpp"\n'
        '    INCLUDE_DIRS "."\n'
        '    PRIV_REQUIRES\n        "c.cpp"\n)\n',
        encoding="utf-8",
    )
    assert gate.cmake_sources(component) == ["a.cpp", "b.cpp"]


def test_a_unit_whose_clang_tidy_said_nothing_and_failed_is_not_analyzed(tmp_path: Path) -> None:
    fake = fake_clang_tidy(tmp_path, analysis='echo "Segmentation fault" >&2\nexit 139\n')
    result = run_gate("--leg", "harness", "--clang-tidy", str(fake), "--only", "main.cpp")
    assert result.returncode == gate.GUARD, result.stdout + result.stderr
    assert "0 of 1 translation units analyzed" in result.stdout


def test_a_parse_diagnostic_fails_the_leg(tmp_path: Path) -> None:
    line = f"{SOURCES}/main.cpp:1:10: error: 'cstddef' file not found [clang-diagnostic-error]"
    fake = fake_clang_tidy(tmp_path, analysis=f'echo "{line}"\nexit 1\n')
    result = run_gate("--leg", "harness", "--clang-tidy", str(fake), "--only", "main.cpp")
    assert result.returncode == gate.GUARD
    assert "1 parse diagnostics" in result.stdout


def test_a_finding_fails_the_leg_and_a_clean_unit_passes(tmp_path: Path) -> None:
    line = (
        f"{SOURCES}/main.cpp:3:5: error: parameter 'x' is unused "
        "[misc-unused-parameters,-warnings-as-errors]"
    )
    dirty = fake_clang_tidy(tmp_path, analysis=f'echo "{line}"\nexit 1\n')
    result = run_gate("--leg", "harness", "--clang-tidy", str(dirty), "--only", "main.cpp")
    assert result.returncode == gate.FINDINGS
    assert "[misc-unused-parameters]" in result.stdout
    clean_dir = tmp_path / "clean"
    clean_dir.mkdir()
    clean = fake_clang_tidy(clean_dir)
    assert (
        run_gate("--leg", "harness", "--clang-tidy", str(clean), "--only", "main.cpp").returncode
        == 0
    )


def test_the_include_report_is_advisory(tmp_path: Path) -> None:
    database = tmp_path / "compile_commands.json"
    write_database(tmp_path, database)
    line = (
        f"{SOURCES}/main.cpp:51:1: warning: included header gpio.h is not used directly "
        "[misc-include-cleaner]"
    )
    fake = fake_clang_tidy(tmp_path, analysis=f'echo "{line}"\nexit 0\n')
    env = dict(
        os.environ,
        METER_XTENSA_TOOLCHAIN=str(tmp_path / "toolchain-xtensa-esp-elf"),
        PATH="/usr/bin:/bin",
    )
    result = run_gate(
        "--include-cleaner",
        "--clang-tidy",
        str(fake),
        "--only",
        "main.cpp",
        "--database",
        str(database),
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 findings" in result.stdout


# --- The device leg's flags -----------------------------------------------------


def write_database(tmp_path: Path, database: Path) -> None:
    """A one-entry compile database for main.cpp, with a response file and a fake GCC."""
    toolchain = tmp_path / "toolchain-xtensa-esp-elf"
    (toolchain / "bin").mkdir(parents=True)
    cxx = toolchain / "xtensa-esp-elf" / "include" / "c++" / "15.2.0"
    libc = toolchain / "picolibc" / "include"
    fake_binary(
        toolchain / "bin",
        "xtensa-esp32s3-elf-g++",
        f"""cat >&2 <<'EOF'
#include "..." search starts here:
#include <...> search starts here:
 {libc}
 {cxx}
 {cxx}/xtensa-esp-elf/esp32s3/no-rtti
 {toolchain}/lib/gcc/xtensa-esp-elf/15.2.0/include
 {toolchain}/xtensa-esp-elf/include
End of search list.
EOF
""",
    )
    response = tmp_path / "cxxflags"
    response.write_text("-mlongcalls\n-fno-rtti\n", encoding="utf-8")
    entry = {
        "directory": str(tmp_path),
        "file": str(SOURCES / "main.cpp"),
        "command": f"xtensa-esp32s3-elf-g++ -o main.o -c @{response} -std=gnu++26 -DESP_PLATFORM "
        f"-D__GNUC_DUMP=1 -isystem/abs/esp -Irel/inc -I {toolchain}/xtensa-esp-elf/include "
        f"-isystem /p/toolchain-riscv32-esp/include {SOURCES / 'main.cpp'}",
    }
    database.write_text(json.dumps([entry]), encoding="utf-8")


def test_the_device_flags_are_the_build_s_includes_and_defines_for_xtensa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "compile_commands.json"
    write_database(tmp_path, database)
    monkeypatch.setenv("METER_XTENSA_TOOLCHAIN", str(tmp_path / "toolchain-xtensa-esp-elf"))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    flags = gate.device_flags(["main.cpp"], database)["main.cpp"]
    assert flags[:6] == [
        "--target=xtensa-esp-elf",
        "-nostdlibinc",
        "-std=gnu++26",
        "-fno-exceptions",
        "-fno-rtti",
        "-DESP_PLATFORM",
    ]
    assert "-isystem/abs/esp" in flags
    assert (
        f"-I{tmp_path / 'rel/inc'}" in flags
    )  # relative paths resolved against the entry's directory
    assert not any(
        "riscv32" in f or f.endswith("/xtensa-esp-elf/include") for f in flags
    )  # foreign roots
    assert not any(f.startswith("-D__") for f in flags)  # GCC's builtin dump
    cxx = str(
        tmp_path / "toolchain-xtensa-esp-elf" / "xtensa-esp-elf" / "include" / "c++" / "15.2.0"
    )
    libc = str(tmp_path / "toolchain-xtensa-esp-elf" / "picolibc" / "include")
    # The toolchain's roots come last, libstdc++ before picolibc, GCC's own headers left to clang.
    assert flags[-6:] == [
        "-isystem",
        cxx,
        "-isystem",
        f"{cxx}/xtensa-esp-elf/esp32s3/no-rtti",
        "-isystem",
        libc,
    ]
    without = gate.device_flags(["main.cpp"], database, drop_toolchain_roots=True)["main.cpp"]
    assert cxx not in without and libc not in without


def test_a_missing_or_short_database_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(gate.GuardFailure, match="missing"):
        gate.device_flags(["main.cpp"], tmp_path / "none.json")
    database = tmp_path / "compile_commands.json"
    write_database(tmp_path, database)
    monkeypatch.setenv("METER_XTENSA_TOOLCHAIN", str(tmp_path / "toolchain-xtensa-esp-elf"))
    with pytest.raises(gate.GuardFailure, match="no entry for sampler.cpp"):
        gate.device_flags(["main.cpp", "sampler.cpp"], database)


def test_the_arch_defines_survive_the_builtin_filter() -> None:
    kept = gate.include_and_define_flags(
        ["-D__XTENSA__", "-D__xtensa__", "-D__GNUC__=15", "-DX=1"], ROOT
    )
    assert kept == ["-D__XTENSA__", "-D__xtensa__", "-DX=1"]


def test_response_files_are_expanded(tmp_path: Path) -> None:
    (tmp_path / "inner").write_text("-DINNER\n", encoding="utf-8")
    (tmp_path / "outer").write_text('-DOUTER "@inner"\n', encoding="utf-8")
    assert gate.expand_response_files(["g++", "@outer", "-c"], tmp_path) == [
        "g++",
        "-DOUTER",
        "-DINNER",
        "-c",
    ]
    with pytest.raises(gate.GuardFailure):
        gate.expand_response_files(["@absent"], tmp_path)


def test_both_legs_analyze_without_exceptions_or_rtti() -> None:
    harness = gate.harness_flags(["main.cpp"])["main.cpp"]
    assert "-fno-exceptions" in harness and "-fno-rtti" in harness


# --- The real run ----------------------------------------------------------------


def pinned_clang_tidy() -> str | None:
    """The clang-tidy of the pinned major the gate would pick, if there is one."""
    major = gate.toolchain_pins()["METER_CLANG_TIDY_MAJOR"]
    binary = gate.default_binary(major)
    if shutil.which(binary) is None:
        return None
    out = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False).stdout
    version = gate.version_of(out)
    return binary if version is not None and version.split(".")[0] == major else None


def test_the_guards_prove_themselves_on_the_device_build(
    built: Callable[[Path, str], Path],
) -> None:
    built(DATABASE, "`pio run --project-dir firmware -e meter -t compiledb`")
    binary = pinned_clang_tidy()
    if binary is None:
        message = "no clang-tidy of the pinned LLVM major (scripts/analysis_toolchain.sh)"
        if os.environ.get("METER_BUILD_REQUIRED") == "1":
            pytest.fail(f"{message} (METER_BUILD_REQUIRED=1, so a skip is a failure)")
        pytest.skip(message)
    result = run_gate("--selftest", "--clang-tidy", binary)
    assert result.returncode == 0, result.stdout + result.stderr
    for label in ("without the toolchain's include roots", "of another major", "a planted finding"):
        assert re.search(rf"{label}.*ok$", result.stdout, re.M), result.stdout


# --- The configuration and the sources ------------------------------------------


def test_the_adopted_checks_are_on_and_registered() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    checks = config.split("Checks: >", 1)[1].split("WarningsAsErrors", 1)[0]
    misra = (ROOT / "docs" / "misra.md").read_text(encoding="utf-8")
    for check, rule in ADOPTED.items():
        assert re.search(rf"^\s+{re.escape(check)},$", checks, re.M), f"{check} is not enabled"
        assert re.search(rf"`{re.escape(check)}`[^\n|]*\({re.escape(rule)}", misra), (
            f"docs/misra.md does not map {check} to {rule}"
        )


def test_every_result_is_used_or_cast_to_void() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    assert re.search(r"bugprone-unused-return-value\.CheckedFunctions\n\s+value: '\.\*'", config)
    assert re.search(r"bugprone-unused-return-value\.AllowCastToVoid\n\s+value: true", config)


def test_the_global_naming_comment_says_the_rule_it_sits_on() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    block = config.split("GlobalVariableCase", 1)[0].rsplit("# ---", 1)[1]
    assert "s_" not in block, "the comment above GlobalVariable* still describes an s_ prefix"
    assert "g prefix" in block


def strip_comments_and_strings(text: str) -> str:
    """Return C++ *text* without its comments and string or character literals."""
    pattern = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'', re.S)
    return pattern.sub(" ", text)


def test_no_memcpy_or_memmove_in_the_firmware() -> None:
    offenders: list[Any] = []
    for path in sorted(SOURCES.glob("*.[ch]*")):
        code = strip_comments_and_strings(path.read_text(encoding="utf-8"))
        offenders.extend(
            f"{path.name}: {match.group(0)}" for match in re.finditer(r"\bmem(cpy|move)\b", code)
        )
    assert not offenders, (
        f"MISRA C++:2023 21.2.2 (std::copy_n, std::copy, std::bit_cast instead): {offenders}"
    )


def test_the_scan_sees_a_planted_memcpy() -> None:
    assert re.search(
        r"\bmem(cpy|move)\b", strip_comments_and_strings("void f() { std::memcpy( a, b, 1 ); }")
    )
    assert not re.search(
        r"\bmem(cpy|move)\b", strip_comments_and_strings('// memcpy\nconst char* s = "memmove";')
    )


def test_the_hook_runs_both_legs_and_the_pinned_cppcheck() -> None:
    hooks = HOOKS.read_text(encoding="utf-8")
    assert "scripts/run_static_analysis.sh tidy" in hooks
    assert "scripts/run_static_analysis.sh cppcheck" in hooks
    assert (
        "clang_tidy_gate" in hooks and "analysis_toolchain" in hooks
    )  # in the hook's files pattern


def test_ci_analyses_after_the_device_build_s_compile_database() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    job = workflow.split("\n  static-analysis:\n", 1)[1].split("\n  # ", 1)[0]
    assert re.search(r"needs: \[[^\]]*\bfirmware\b", job), (
        "static-analysis does not wait for the device build"
    )
    compiledb = job.index("-t compiledb")
    for step in (
        "run_static_analysis.sh tidy",
        "run_static_analysis.sh selftest",
        "run_static_analysis.sh cppcheck",
    ):
        assert job.index(step) > compiledb, f"{step} runs before the compile database exists"
    assert "METER_CPPCHECK_SHA256" in job and "sha256sum -c" in job
    assert "apt-get install -y -qq cppcheck" not in job
