#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""clang-tidy over the firmware's sources, guarded against analyzing nothing.

Two legs, each over every translation unit of ``firmware/src/CMakeLists.txt``:

``device``
    The sources as the image compiles them: each entry of
    ``firmware/compile_commands.json`` (``pio run --project-dir firmware -e
    meter -t compiledb``) with its response files expanded, reduced to its
    include paths and defines, for the real target (``--target=xtensa-esp-elf``)
    in the device's language mode (its ``-std``, ``-fno-exceptions``,
    ``-fno-rtti``), with the host's own headers dropped (``-nostdlibinc``) and
    the toolchain's libstdc++ and picolibc roots, as its GCC reports them,
    after every include path of the build.
``harness``
    The sources as the host tests compile them: against the fake ESP-IDF of
    ``firmware/test/native/harness``, at the device's standard, with
    ``-fno-exceptions -fno-rtti``.

The guards are the firmware repository's (``scripts/run_clang_tidy.sh``
there), each a failure of its own: the clang-tidy binary is of the LLVM major
``scripts/analysis_toolchain.sh`` pins; ``--verify-config`` accepts
``firmware/.clang-tidy``; the translation units analyzed are exactly the CMake source
list, each with an output; and a translation unit that emits any parse
diagnostic (a ``clang-diagnostic-*`` line) fails the leg, because clang-tidy
reports findings off a half-parsed AST and they are fiction.

Exit codes: 0, every translation unit analyzed and no finding; 1, at least
one finding; 2, a guard failed (wrong binary, rejected configuration, a
missing or short compile database, a translation unit not analyzed, a parse
diagnostic). ``--include-cleaner`` runs ``misc-include-cleaner`` alone over
the device leg's flags and reports, advisory: it exits 0 whatever it finds,
and 2 only on a guard. ``--selftest`` proves the guards: it requires exit 2
from the device leg with the toolchain's include roots removed and from a
clang-tidy that reports another major, and exit 1 from a planted finding.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = ROOT / "firmware"
SOURCES_DIR = FIRMWARE / "src"
COMPONENT = SOURCES_DIR / "CMakeLists.txt"
DATABASE = FIRMWARE / "compile_commands.json"
CONFIG = FIRMWARE / ".clang-tidy"
TOOLCHAIN_PINS = ROOT / "scripts" / "analysis_toolchain.sh"
HARNESS = FIRMWARE / "test" / "native" / "harness"

#: The exit code of a guard failure: the gate refuses to report on an
#: analysis it cannot trust.
GUARD = 2

#: The exit code of a run with at least one finding.
FINDINGS = 1

#: The target the image runs on; clang has known it since LLVM 19.
DEVICE_TARGET = "--target=xtensa-esp-elf"

#: The language mode the device builds in, restated: the flag filter keeps
#: only include paths and defines, and several checks key off these.
LANGUAGE_MODE = ("-fno-exceptions", "-fno-rtti")

#: The standard the host tests compile at (firmware/test/native/Makefile).
HARNESS_STD = "-std=gnu++2c"

#: Options that take the next token as their argument and are kept.
KEPT_WITH_ARGUMENT = ("-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros")

#: Include options GCC also accepts with the path joined to them.
JOINED_INCLUDE_OPTIONS = ("-isystem", "-iquote", "-idirafter", "-I")

#: Defines GCC's builtin dump would carry that clang must not see, kept for
#: the Xtensa architecture macros the ESP-IDF headers branch on.
ARCH_DEFINES = ("-D__XTENSA", "-D__xtensa")

#: One clang-tidy diagnostic line: file, line, column, severity, message, checks.
DIAGNOSTIC = re.compile(
    r"^(?P<file>\S[^:]*):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<sev>error|warning|note): (?P<msg>.*) \[(?P<checks>[^\]]+)\]$"
)


class GuardFailure(Exception):
    """A guard refused the run: the analysis would not be trustworthy."""


# --- Pins ----------------------------------------------------------------------


def toolchain_pins(path: Path = TOOLCHAIN_PINS) -> dict[str, str]:
    """Return the ``METER_*`` assignments of *path*, the single definition site."""
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(METER_[A-Z0-9_]+)=(\S+)$", line.strip())
        if match:
            pins[match.group(1)] = match.group(2)
    for key in ("METER_CLANG_TIDY_MAJOR", "METER_CLANG_TIDY_VERSION", "METER_CPPCHECK_VERSION"):
        if key not in pins:
            raise GuardFailure(f"{path.relative_to(ROOT)} does not define {key}")
    return pins


def version_of(output: str) -> str | None:
    """Return the first ``major.minor.patch`` in a ``--version`` *output*."""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    return match.group(0) if match else None


def default_binary(major: str) -> str:
    """Return the first clang-tidy of LLVM *major* among the usual places, else ``clang-tidy``.

    The order is the firmware repository's: the PATH's ``clang-tidy``, the
    versioned name apt.llvm.org installs, then Homebrew's LLVM.
    """
    candidates = [
        "clang-tidy",
        f"clang-tidy-{major}",
        "/opt/homebrew/opt/llvm/bin/clang-tidy",
        "/usr/local/opt/llvm/bin/clang-tidy",
    ]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved is None:
            continue
        out = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, check=False
        ).stdout
        found = version_of(out)
        if found is not None and found.split(".")[0] == major:
            return resolved
    return "clang-tidy"


def check_binary(binary: str, major: str, version: str) -> tuple[str, str]:
    """Return *binary*'s absolute path and LLVM version.

    Raises GuardFailure if the binary is missing or of another major.
    """
    resolved = shutil.which(binary)
    if resolved is None:
        raise GuardFailure(f"clang-tidy '{binary}' not found (set CLANG_TIDY, pin LLVM {version})")
    resolved = os.path.abspath(resolved)
    try:
        out = subprocess.run(
            [resolved, "--version"], capture_output=True, text=True, check=False
        ).stdout
    except OSError as error:
        raise GuardFailure(f"clang-tidy '{binary}' does not run: {error}") from error
    found = version_of(out)
    if found is None or found.split(".")[0] != major:
        raise GuardFailure(
            f"clang-tidy '{binary}' is LLVM {found or 'unknown'}; "
            f"the gate is pinned to LLVM {major} "
            f"(scripts/analysis_toolchain.sh): another major analyses another check set"
        )
    if found != version:
        print(f"warning: clang-tidy is {found}, the pin is {version} (same major)", file=sys.stderr)
    return resolved, found


def verify_config(binary: str) -> None:
    """Raise unless *binary* accepts ``firmware/.clang-tidy`` as it is."""
    result = subprocess.run(
        [binary, "--verify-config", f"--config-file={CONFIG}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GuardFailure(
            "clang-tidy rejected firmware/.clang-tidy (--verify-config):\n"
            f"{result.stdout}{result.stderr}"
        )


# --- The translation units ------------------------------------------------------


def cmake_sources(component: Path = COMPONENT) -> list[str]:
    """Return the ``SRCS`` of the component's ``idf_component_register``, in order."""
    text = component.read_text(encoding="utf-8")
    block = re.search(r"\bSRCS\b(.*?)(?:\n\s*[A-Z_]{4,}\b)", text, re.S)
    if block is None:
        raise GuardFailure(f"no SRCS list in {component.relative_to(ROOT)}")
    names = re.findall(r'"([^"]+\.cpp)"', block.group(1))
    if not names:
        raise GuardFailure(f"an empty SRCS list in {component.relative_to(ROOT)}")
    return names


# --- Flags -----------------------------------------------------------------------


def expand_response_files(tokens: Sequence[str], directory: Path) -> list[str]:
    """Return *tokens* with every ``@file`` replaced by the file's own tokens."""
    out: list[str] = []
    for token in tokens:
        if token.startswith("@") and len(token) > 1:
            path = Path(token[1:])
            if not path.is_absolute():
                path = directory / path
            if not path.is_file():
                raise GuardFailure(f"response file {path} named by the compile database is missing")
            out.extend(
                expand_response_files(shlex.split(path.read_text(encoding="utf-8")), directory)
            )
        else:
            out.append(token)
    return out


def entry_tokens(entry: dict[str, Any]) -> list[str]:
    """Return one compile database *entry*'s command, response files expanded."""
    directory = Path(entry["directory"])
    raw = entry["arguments"] if "arguments" in entry else shlex.split(entry["command"])
    return expand_response_files(raw, directory)


def foreign_include(path: str) -> bool:
    """Return whether *path* is an include root of a build this is not.

    PlatformIO knows the RISC-V toolchain (the ULP's) too, and the Xtensa
    toolchain's own ``xtensa-esp-elf/include`` is newlib's header set, which
    ESP-IDF 6 replaced with picolibc: either one shadows the real headers.
    """
    return "/toolchain-riscv32-esp/" in path or path.rstrip("/").endswith(
        "/toolchain-xtensa-esp-elf/xtensa-esp-elf/include"
    )


def include_and_define_flags(tokens: Sequence[str], directory: Path) -> list[str]:
    """Return the include paths and defines of a GCC command line, clang-safe.

    Relative paths are made absolute against the entry's *directory*, and the
    roots of a foreign build are dropped (foreign_include()). GCC's builtin
    ``-D__...`` macros are dropped but for the Xtensa architecture's.
    """

    def absolute(value: str) -> str:
        return value if Path(value).is_absolute() else str(directory / value)

    out: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        joined = next(
            (o for o in JOINED_INCLUDE_OPTIONS if token.startswith(o) and token != o), None
        )
        if token in KEPT_WITH_ARGUMENT and i + 1 < len(tokens):
            value = tokens[i + 1]
            if not foreign_include(value):
                out.extend([token, absolute(value)])
            i += 2
            continue
        if joined is not None:
            value = token[len(joined) :]
            if not foreign_include(value):
                out.append(joined + absolute(value))
        elif token.startswith("-D") and (
            not token.startswith("-D__") or token.startswith(ARCH_DEFINES)
        ):
            out.append(token)
        i += 1
    return out


def standard_of(tokens: Sequence[str]) -> str:
    """Return the last ``-std=`` of a command line, which is the one GCC obeys."""
    found = [t for t in tokens if t.startswith("-std=")]
    if not found:
        raise GuardFailure(
            "a compile database entry has no -std= (the database is not the device's)"
        )
    return found[-1]


def toolchain_roots(compiler: str, tokens: Sequence[str]) -> list[str]:
    """Return the toolchain's libstdc++ and picolibc include roots, as its GCC reports them.

    The compiler is asked with the entry's own machine and specs options, so it
    picks the same multilib (esp32s3, no-rtti) the build does. libstdc++ comes
    first, so its ``#include_next`` of a C header reaches picolibc's; GCC's
    private ``include`` and ``include-fixed`` are left to clang's own
    resource headers, and newlib's set is dropped.
    """
    machine = [
        t
        for t in tokens
        if t.startswith(("-m", "-specs=", "-f")) and not t.startswith("-fdiagnostics")
    ]
    try:
        result = subprocess.run(
            [compiler, *machine, "-E", "-x", "c++", "-v", os.devnull, "-o", os.devnull],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise GuardFailure(f"the device compiler '{compiler}' does not run: {error}") from error
    listing = result.stderr.split("#include <...> search starts here:", 1)
    if len(listing) != 2:
        raise GuardFailure(f"'{compiler}' printed no include search list")
    roots = [
        os.path.normpath(line.strip())
        for line in listing[1].split("End of search list.", 1)[0].splitlines()
        if line.strip()
    ]
    cxx = [r for r in roots if "/include/c++/" in r + "/"]
    libc = [r for r in roots if r.rstrip("/").endswith("/picolibc/include")]
    if not cxx or not libc:
        raise GuardFailure(f"'{compiler}' reported no libstdc++ or picolibc root: {roots}")
    return [flag for root in [*cxx, *libc] for flag in ("-isystem", root)]


def resolve_compiler(name: str) -> str:
    """Return the path of the device compiler *name* of a database entry."""
    if os.sep in name:
        return name
    found = shutil.which(name)
    if found:
        return found
    toolchain = Path(
        os.environ.get(
            "METER_XTENSA_TOOLCHAIN",
            Path.home() / ".platformio" / "packages" / "toolchain-xtensa-esp-elf",
        )
    )
    candidate = toolchain / "bin" / name
    if candidate.is_file():
        return str(candidate)
    raise GuardFailure(
        f"the device compiler '{name}' is neither on PATH nor in {toolchain}/bin "
        "(set METER_XTENSA_TOOLCHAIN)"
    )


def device_flags(
    sources: Sequence[str], database: Path = DATABASE, drop_toolchain_roots: bool = False
) -> dict[str, list[str]]:
    """Return the clang flags of each of *sources*, from the compile database."""
    if not database.is_file():
        raise GuardFailure(
            f"{database} is missing: run `pio run --project-dir firmware -e meter -t compiledb` "
            "(through scripts/pio-job) first"
        )
    entries = json.loads(database.read_text(encoding="utf-8"))
    by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        path = Path(entry["file"])
        if not path.is_absolute():
            path = Path(entry["directory"]) / path
        try:
            relative = path.resolve().relative_to(SOURCES_DIR.resolve())
        except ValueError:
            continue
        by_name[str(relative)] = entry
    missing = [s for s in sources if s not in by_name]
    if missing:
        raise GuardFailure(
            f"the compile database has no entry for {', '.join(missing)}: "
            "it is stale, regenerate it"
        )
    flags: dict[str, list[str]] = {}
    roots_cache: dict[tuple[str, ...], list[str]] = {}
    for source in sources:
        entry = by_name[source]
        tokens = entry_tokens(entry)
        compiler = resolve_compiler(tokens[0])
        key = (compiler, *[t for t in tokens if t.startswith(("-m", "-specs="))])
        if key not in roots_cache:
            roots_cache[key] = toolchain_roots(compiler, tokens)
        roots = [] if drop_toolchain_roots else roots_cache[key]
        directory = Path(entry["directory"])
        flags[source] = [
            DEVICE_TARGET,
            "-nostdlibinc",
            standard_of(tokens),
            *LANGUAGE_MODE,
            *include_and_define_flags(tokens[1:], directory),
            *roots,
        ]
    return flags


def host_sysroot() -> list[str]:
    """Return ``-isysroot`` for the macOS SDK, which a clang-tidy built outside Xcode misses.

    Homebrew's LLVM finds the SDK by itself; the PyPI wheel CI pins does not, and
    on macOS it would miss every standard header. Elsewhere, nothing.
    """
    if sys.platform != "darwin" or shutil.which("xcrun") is None:
        return []
    sdk = subprocess.run(
        ["xcrun", "--show-sdk-path"], capture_output=True, text=True, check=False
    ).stdout.strip()
    return ["-isysroot", sdk] if sdk else []


def harness_flags(sources: Sequence[str]) -> dict[str, list[str]]:
    """Return the clang flags of each of *sources* as the host tests compile them."""
    common = [
        "-x",
        "c++",
        HARNESS_STD,
        *LANGUAGE_MODE,
        *host_sysroot(),
        f"-I{HARNESS / 'idf'}",
        f"-I{HARNESS}",
        f"-I{SOURCES_DIR}",
    ]
    return {source: list(common) for source in sources}


# --- Running and reading ------------------------------------------------------------


@dataclass
class Outcome:
    """What one leg found."""

    analyzed: list[str] = field(default_factory=list)
    findings: dict[tuple[str, int, int, str], str] = field(default_factory=dict)
    parse_noise: dict[str, list[str]] = field(default_factory=dict)


def read_output(source: str, text: str, outcome: Outcome) -> int:
    """Fold one translation unit's clang-tidy *text* into *outcome*; return the diagnostics read."""
    read = 0
    for line in text.splitlines():
        match = DIAGNOSTIC.match(line.strip())
        if match is None:
            continue
        checks = [
            c.strip()
            for c in match.group("checks").split(",")
            if c.strip() and c.strip() != "-warnings-as-errors"
        ]
        primary = checks[0] if checks else match.group("checks")
        read += 1
        if primary.startswith("clang-diagnostic"):
            outcome.parse_noise.setdefault(source, []).append(line.strip())
            continue
        if match.group("sev") == "note":
            continue
        try:
            where = str(Path(match.group("file")).resolve().relative_to(ROOT))
        except ValueError:
            where = match.group("file")
        key = (where, int(match.group("line")), int(match.group("col")), primary)
        outcome.findings[key] = match.group("msg")
    return read


def run_leg(binary: str, flags: dict[str, list[str]], jobs: int, config: list[str]) -> Outcome:
    """Run *binary* over every source of *flags* and return what it found."""
    outcome = Outcome()

    def one(source: str) -> tuple[str, subprocess.CompletedProcess[str]]:
        command = [binary, "--quiet", *config, str(SOURCES_DIR / source), "--", *flags[source]]
        return source, subprocess.run(
            command, cwd=FIRMWARE, capture_output=True, text=True, check=False
        )

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for source, result in pool.map(one, flags):
            read = read_output(source, result.stdout, outcome)
            if "fatal error:" in result.stderr or "Too many errors emitted" in result.stderr:
                outcome.parse_noise.setdefault(source, []).append("fatal parse error")
            if result.returncode != 0 and read == 0:
                # A clang-tidy that failed and said nothing did not analyze the
                # unit: it crashed, or refused its command line.
                last = (result.stderr.strip().splitlines() or ["no output"])[-1]
                outcome.parse_noise.setdefault(source, []).append(
                    f"exit {result.returncode} with no diagnostic: {last}"
                )
                continue
            outcome.analyzed.append(source)
    return outcome


def verdict(leg: str, sources: Sequence[str], outcome: Outcome, advisory: bool = False) -> int:
    """Print the leg's summary and return its exit code."""
    findings = sorted(outcome.findings.items())
    noisy = sum(len(v) for v in outcome.parse_noise.values())
    print(
        f"clang-tidy {leg}: {len(outcome.analyzed)} of {len(sources)} translation units analyzed, "
        f"{len(findings)} findings, {noisy} parse diagnostics"
    )
    for (where, line, col, check), message in findings:
        print(f"  {where}:{line}:{col}: {message} [{check}]")
    if sorted(outcome.analyzed) != sorted(sources):
        print(
            f"GATE FAILURE (guard): analyzed {sorted(outcome.analyzed)}, "
            f"expected {sorted(sources)}",
            file=sys.stderr,
        )
        return GUARD
    if outcome.parse_noise:
        print(
            "GATE FAILURE (guard): translation units with parse diagnostics; "
            "their findings cannot be "
            "trusted, fix the flag set, not the findings:",
            file=sys.stderr,
        )
        for source, lines in sorted(outcome.parse_noise.items()):
            print(f"  {source}: {len(lines)}: {lines[0]}", file=sys.stderr)
        return GUARD
    if findings and not advisory:
        return FINDINGS
    return 0


# --- Self-test ---------------------------------------------------------------------


def selftest(binary: str, jobs: int) -> int:
    """Prove the guards: each planted fault gives the exit code the contract says."""
    script = [sys.executable, str(Path(__file__).resolve())]
    environment = dict(os.environ)
    failures = 0

    def expect(
        label: str, command: list[str], wanted: int, env: dict[str, str] | None = None
    ) -> None:
        nonlocal failures
        result = subprocess.run(
            command, capture_output=True, text=True, check=False, env=env or environment
        )
        ok = result.returncode == wanted
        print(
            f"selftest: {label}: exit {result.returncode} (wanted {wanted}) "
            f"{'ok' if ok else 'FAILED'}"
        )
        if not ok:
            failures += 1
            print(result.stdout + result.stderr, file=sys.stderr)

    one = ["--only", "main.cpp"]
    expect(
        "device leg without the toolchain's include roots",
        [
            *script,
            "--leg",
            "device",
            "--clang-tidy",
            binary,
            "--jobs",
            str(jobs),
            "--drop-toolchain-roots",
            *one,
        ],
        GUARD,
    )
    with tempfile.TemporaryDirectory() as scratch:
        fake = Path(scratch) / "clang-tidy"
        fake.write_text(
            "#!/bin/sh\necho 'LLVM (http://llvm.org/):'\necho '  LLVM version 15.0.7'\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        expect(
            "a clang-tidy of another major",
            [*script, "--leg", "harness", "--clang-tidy", str(fake), *one],
            GUARD,
        )
        expect(
            "a planted finding",
            [*script, "--leg", "harness", "--clang-tidy", binary, "--plant-finding", *one],
            FINDINGS,
        )
    return 0 if failures == 0 else GUARD


# --- Command line ----------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Return the parsed command line."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--leg", choices=("device", "harness"), help="the leg to run")
    parser.add_argument(
        "--clang-tidy",
        default=os.environ.get("CLANG_TIDY"),
        help="the clang-tidy binary (default: $CLANG_TIDY, else the first of the pinned major)",
    )
    parser.add_argument(
        "--jobs", type=int, default=int(os.environ.get("KLEIDOS_PIO_JOBS") or os.cpu_count() or 4)
    )
    parser.add_argument(
        "--include-cleaner",
        action="store_true",
        help="advisory misc-include-cleaner report over the device leg",
    )
    parser.add_argument("--selftest", action="store_true", help="prove the guards")
    parser.add_argument(
        "--print-sources", action="store_true", help="print the CMake source list and exit"
    )
    parser.add_argument("--only", action="append", help=argparse.SUPPRESS)
    parser.add_argument("--drop-toolchain-roots", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--database", type=Path, default=DATABASE, help=argparse.SUPPRESS)
    parser.add_argument("--plant-finding", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested leg, report or self-test and return its exit code."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        sources = cmake_sources()
        if args.print_sources:
            print("\n".join(f"firmware/src/{s}" for s in sources))
            return 0
        pins = toolchain_pins()
        requested = args.clang_tidy or default_binary(pins["METER_CLANG_TIDY_MAJOR"])
        binary, found = check_binary(
            requested, pins["METER_CLANG_TIDY_MAJOR"], pins["METER_CLANG_TIDY_VERSION"]
        )
        verify_config(binary)
        if args.selftest:
            return selftest(binary, args.jobs)
        wanted = list(args.only) if args.only else sources
        leg = "device" if args.include_cleaner else args.leg
        if leg is None:
            raise GuardFailure("name a --leg, --include-cleaner or --selftest")
        print(f"== clang-tidy {leg} leg (LLVM {found}, {len(wanted)} translation units)")
        flags = (
            device_flags(wanted, args.database, args.drop_toolchain_roots)
            if leg == "device"
            else harness_flags(wanted)
        )
        config = [f"--config-file={CONFIG}"]
        if args.include_cleaner:
            config = [
                "--config={Checks: '-*,misc-include-cleaner', "
                "HeaderFilterRegex: 'firmware/src/.*\\.h$'}"
            ]
        if args.plant_finding:
            config = [*config, "--checks=readability-identifier-length"]
        outcome = run_leg(binary, flags, args.jobs, config)
        return verdict(
            f"{leg} (include-cleaner, advisory)" if args.include_cleaner else leg,
            wanted,
            outcome,
            args.include_cleaner,
        )
    except (GuardFailure, OSError) as failure:
        print(f"GATE FAILURE (guard): {failure}", file=sys.stderr)
        return GUARD


if __name__ == "__main__":
    sys.exit(main())
