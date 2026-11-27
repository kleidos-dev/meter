# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""Every warning is an error where it counts: in the compile command each source of ours gets.

``firmware/platformio.ini`` asks for ``-Werror``, but ESP-IDF's build.cmake
gives every component, ours included and before our flags,
``-Wno-error=unused-function``, ``-Wno-error=unused-variable``,
``-Wno-error=unused-but-set-variable``, ``-Wno-error=deprecated-declarations``,
``-Wno-error=extra``, ``-Wno-unused-parameter``, ``-Wno-sign-compare`` and
``-Wno-enum-conversion``. GCC keeps a per-warning ``-Wno-error=<w>`` whatever
the order of a global ``-Werror``, and an explicit ``-Wno-<w>`` is not undone by
a later ``-Wall`` or ``-Wextra``; so until 2026-11-22 (FW-01) those eight were
warnings, or nothing, on the device while the ini read as if they were errors.

This guard reads what the compiler is actually told: the ``src/*.cpp``
entries of ``firmware/compile_commands.json`` (``pio run --project-dir
firmware -e meter -t compiledb``, never a verbose build, whose printing
PlatformIO's SCons breaks), with their response files expanded. For every one
of the component's translation units (the ``SRCS`` of
``firmware/src/CMakeLists.txt``) it asserts that the last word for each of the
eight is an error and enabled, that no ``-Wno-error=`` or ``-Wno-<w>`` is left
without a later flag that answers it, and that every flag of ``build_src_flags``
reached it. The link is held the same way: ``-Wl,--fatal-warnings`` in
``firmware/CMakeLists.txt`` and in the image's link command, as CMake's code
model gives it to PlatformIO, and so are the wraps and ``-Wl,--emit-relocs``,
which keeps the relocations the reachability gate reads (FW-19).

The build outputs exist only after a build: the guard skips without them, and
fails under ``METER_BUILD_REQUIRED=1`` (CI's firmware job, ``conftest.py``).
The model of GCC's rules is tested here on planted command lines both ways.
"""

from __future__ import annotations

import configparser
import json
import re
import shlex
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "firmware"
DATABASE = FIRMWARE / "compile_commands.json"
COMPONENT = FIRMWARE / "src" / "CMakeLists.txt"
PROJECT = FIRMWARE / "CMakeLists.txt"
METER_INI = FIRMWARE / "platformio.ini"
REPLY = FIRMWARE / ".pio" / "build" / "meter" / ".cmake" / "api" / "v1" / "reply"
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"

#: The warnings ESP-IDF relaxes for every component, each of which must end
#: as an error and enabled for ours.
RELAXED_BY_IDF = (
    "unused-function",
    "unused-variable",
    "unused-but-set-variable",
    "deprecated-declarations",
    "extra",
    "unused-parameter",
    "sign-compare",
    "enum-conversion",
)

#: The link option that makes a linker warning an error.
FATAL_LINK = "-Wl,--fatal-warnings"

#: The link option that keeps the relocations in the image, which the
#: reachability gate builds its call graph from (FW-19,
#: ``scripts/check_heap_reachability.py``).
EMIT_RELOCS = "-Wl,--emit-relocs"

#: The link option that sends ESP-IDF's flash region check to the firmware's
#: static table (FW-16, ``firmware/src/flash_port.h``).
REGION_WRAP = "-Wl,--wrap=esp_partition_is_flash_region_writable"

#: The link options that send two of ESP-IDF's allocating startup calls to
#: the firmware's static ones (FW-18, ``firmware/src/startup_memory.h``).
STARTUP_WRAPS = (
    "-Wl,--wrap=vApplicationGetIdleTaskMemory",
    "-Wl,--wrap=psa_crypto_init",
    "-Wl,--wrap=xTaskCreatePinnedToCore",
    "-Wl,--wrap=xQueueCreateMutex",
    "-Wl,--wrap=esp_intr_alloc",
)

COMPILEDB = "`pio run --project-dir firmware -e meter -t compiledb`"
BUILD = "`pio run --project-dir firmware -e meter`"


# --- GCC's rules, as far as this guard needs them -----------------------------


def effective(flags: Sequence[str], warning: str) -> tuple[bool, bool]:
    """Return whether *warning* is enabled and whether it is an error, as GCC reads *flags*.

    An explicit ``-W<w>``, ``-Wno-<w>`` or ``-Werror=<w>`` decides whether the
    warning is on, the last one winning; ``-Werror=<w>`` and ``-Wno-error=<w>``
    decide whether it is an error, the last one winning, whatever the order of a
    global ``-Werror``, which decides only for a warning neither names. An
    umbrella (``-Wall``, ``-Wextra``) never overrides an explicit setting, so a
    warning with none counts as off: the guard asks for the explicit one.

    :param flags: the compile command's arguments, response files expanded.
    :param warning: the warning's name, without ``-W``.
    :returns: ``(enabled, error)``.
    """
    enabled = False
    error: bool | None = None
    global_error = False
    for flag in flags:
        if flag in (f"-W{warning}", f"-Werror={warning}"):
            enabled = True
        elif flag == f"-Wno-{warning}":
            enabled = False
        if flag == f"-Werror={warning}":
            error = True
        elif flag == f"-Wno-error={warning}":
            error = False
        elif flag == "-Werror":
            global_error = True
        elif flag == "-Wno-error":
            global_error = False
    return enabled, global_error if error is None else error


def surviving_relaxations(flags: Sequence[str]) -> list[str]:
    """Return every ``-Wno-error=<w>``, ``-Wno-error`` or ``-Wno-<w>`` no later flag answers.

    ``-Wno-error=<w>`` is answered by a later ``-Werror=<w>``; ``-Wno-<w>`` by a
    later ``-W<w>`` or ``-Werror=<w>``; the global ``-Wno-error`` by a later
    ``-Werror``.

    :param flags: the compile command's arguments, response files expanded.
    :returns: the flags that still have the last word, in order.
    """
    left: list[str] = []
    for index, flag in enumerate(flags):
        later = flags[index + 1 :]
        if flag == "-Wno-error":
            answered = "-Werror" in later
        elif flag.startswith("-Wno-error="):
            answered = f"-Werror={flag.removeprefix('-Wno-error=')}" in later
        elif flag.startswith("-Wno-"):
            name = flag.removeprefix("-Wno-")
            answered = f"-W{name}" in later or f"-Werror={name}" in later
        else:
            continue
        if not answered:
            left.append(flag)
    return left


def test_the_model_sees_the_hole_idf_leaves() -> None:
    """ESP-IDF's flags followed by a bare -Werror leave every one of the eight relaxed."""
    idf = [
        "-Wall",
        "-Werror",
        "-Wno-error=unused-function",
        "-Wno-error=unused-variable",
        "-Wno-error=unused-but-set-variable",
        "-Wno-error=deprecated-declarations",
        "-Wextra",
        "-Wno-error=extra",
        "-Wno-unused-parameter",
        "-Wno-sign-compare",
        "-Wno-enum-conversion",
    ]
    before = [*idf, "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter"]
    for warning in RELAXED_BY_IDF:
        assert effective(before, warning) != (True, True), warning
    assert len(surviving_relaxations(before)) == 9


def test_the_model_accepts_the_answer_by_name() -> None:
    """The same flags with each of the eight answered by name, after them."""
    after = [
        "-Werror",
        "-Wno-error=extra",
        "-Wno-unused-parameter",
        "-Wno-error",
        "-Werror",
        "-Werror=extra",
        "-Wunused-parameter",
    ]
    assert effective(after, "extra") == (True, True)
    assert effective(after, "unused-parameter") == (True, True)
    assert effective(after, "shadow") == (False, True), "an unnamed warning is off"
    assert surviving_relaxations(after) == []
    assert surviving_relaxations([*after, "-Wno-shadow"]) == ["-Wno-shadow"]
    assert surviving_relaxations([*after, "-Wno-error"]) == ["-Wno-error"]


# --- The project's files ------------------------------------------------------


def component_sources() -> list[str]:
    """Return the ``SRCS`` of the firmware's component, as ``src/<name>.cpp``."""
    text = COMPONENT.read_text(encoding="utf-8")
    block = re.search(r"SRCS\s+(.*?)\n\s*INCLUDE_DIRS", text, re.DOTALL)
    assert block, "firmware/src/CMakeLists.txt has no SRCS list"
    return sorted(f"src/{name}" for name in re.findall(r'"([^"]+\.cpp)"', block.group(1)))


def test_the_component_has_twenty_three_translation_units() -> None:
    """The count the plan names; a new source changes it here on purpose (FW-09:
    interrupt.cpp, FW-11: watchdog.cpp, FW-12: uart0.cpp, FW-13: i2c_bus.cpp,
    FW-15: flash_log.cpp, FW-16: flash_port.cpp, FW-18: startup_memory.cpp and
    brownout.cpp, FW-24: text.cpp, 0.18.0's FW-03: pinmap_store.cpp,
    FW-04: pin_boot.cpp, FW-05: pin_apply.cpp)."""
    assert len(component_sources()) == 23, component_sources()


def requested_flags() -> list[str]:
    """Return ``build_src_flags`` of ``env:meter``, in order."""
    ini = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";",))
    ini.read_string(METER_INI.read_text(encoding="utf-8"))
    return ini["env:meter"]["build_src_flags"].split()


def test_the_ini_answers_each_relaxation_by_name() -> None:
    """The source of the answer, held even where no build exists."""
    flags = requested_flags()
    for warning in RELAXED_BY_IDF:
        assert f"-Werror={warning}" in flags or f"-W{warning}" in flags, warning
    assert not [flag for flag in flags if flag.startswith("-Wno-")], "a -Wno- in build_src_flags"


def test_the_project_asks_for_a_fatal_link() -> None:
    text = PROJECT.read_text(encoding="utf-8")
    option = f'idf_build_set_property(LINK_OPTIONS "{FATAL_LINK}" APPEND)'
    assert option in text, "firmware/CMakeLists.txt must make linker warnings errors"
    assert text.index(option) < text.index("project(kleidos-meter)"), "before project()"


def test_the_project_keeps_the_relocations_in_the_image() -> None:
    """FW-19: the reachability gate reads the image's relocation records."""
    text = PROJECT.read_text(encoding="utf-8")
    option = f'idf_build_set_property(LINK_OPTIONS "{EMIT_RELOCS}" APPEND)'
    assert option in text, "firmware/CMakeLists.txt must keep the image's relocations"
    assert text.index(option) < text.index("project(kleidos-meter)"), "before project()"


def test_the_project_wraps_the_flash_region_check() -> None:
    text = PROJECT.read_text(encoding="utf-8")
    option = f'idf_build_set_property(LINK_OPTIONS\n    "{REGION_WRAP}" APPEND)'
    assert option in text, "firmware/CMakeLists.txt must wrap the region check (FW-16)"
    assert text.index(option) < text.index("project(kleidos-meter)"), "before project()"


def test_the_project_wraps_the_two_allocating_startup_calls() -> None:
    """FW-18: the idle tasks' memory and PSA's init, before ``project()``."""
    text = PROJECT.read_text(encoding="utf-8")
    for wrap in STARTUP_WRAPS:
        assert f'"{wrap}" APPEND)' in text, f"firmware/CMakeLists.txt must link {wrap}"
        assert text.index(wrap) < text.index("project(kleidos-meter)"), "before project()"


def test_ci_runs_the_build_guards_after_the_build() -> None:
    """CI's firmware job writes the compile database and runs these guards with no skip allowed."""
    text = WORKFLOW.read_text(encoding="utf-8")
    job = text[text.index("\n  firmware:\n") :]
    job = job[: job.index("\n  schematic:")]
    assert "-t compiledb" in job
    assert "METER_BUILD_REQUIRED=1" in job
    for guard in (
        "tests/firmware/test_effective_compile_flags.py",
        "tests/firmware/test_meter_sdkconfig_reaches_the_build.py",
    ):
        assert guard in job, guard
    assert job.index("-t compiledb") > job.index("pio run --project-dir firmware -e meter\n")


# --- The build's own record ---------------------------------------------------


def expand(arguments: list[str], directory: Path) -> list[str]:
    """Return *arguments* with every ``@file`` response file replaced by its contents."""
    out: list[str] = []
    for argument in arguments:
        if argument.startswith("@"):
            path = Path(argument[1:])
            path = path if path.is_absolute() else directory / path
            assert path.exists(), f"response file {path} is gone: rebuild ({BUILD})"
            out += expand(shlex.split(path.read_text(encoding="utf-8")), directory)
        else:
            out.append(argument)
    return out


@pytest.fixture
def database(built: Callable[[Path, str], Path]) -> dict[str, list[str]]:
    """The component's entries of the compile database: source to expanded arguments."""
    entries = json.loads(built(DATABASE, COMPILEDB).read_text(encoding="utf-8"))
    out: dict[str, list[str]] = {}
    for entry in entries:
        name = entry["file"]
        if not (name.startswith("src/") and name.endswith(".cpp")):
            continue
        arguments = entry.get("arguments") or shlex.split(entry["command"])
        out[name] = expand(arguments, Path(entry["directory"]))
    return out


def test_every_translation_unit_is_in_the_database(database: dict[str, list[str]]) -> None:
    assert sorted(database) == component_sources()


@pytest.mark.parametrize("source", component_sources())
def test_every_relaxed_warning_ends_as_an_error(
    source: str, database: dict[str, list[str]]
) -> None:
    flags = database[source]
    wrong = {w: effective(flags, w) for w in RELAXED_BY_IDF if effective(flags, w) != (True, True)}
    assert not wrong, f"{source}: (enabled, error) per warning: {wrong}"
    assert surviving_relaxations(flags) == [], f"{source}: these still have the last word"
    missing = [flag for flag in requested_flags() if flag not in flags]
    assert not missing, f"{source}: build_src_flags did not reach it: {missing}"


def test_the_image_links_with_fatal_warnings(built: Callable[[Path, str], Path]) -> None:
    """The link command PlatformIO takes from CMake's code model carries the option."""
    replies = sorted(REPLY.glob("target-kleidos-meter.elf-*.json"))
    reply = built(replies[0] if replies else REPLY / "target-kleidos-meter.elf.json", BUILD)
    model = json.loads(reply.read_text(encoding="utf-8"))
    fragments = [
        argument
        for fragment in model["link"]["commandFragments"]
        for argument in shlex.split(fragment["fragment"])
    ]
    assert FATAL_LINK in fragments
    last = fragments.index(FATAL_LINK)
    assert "-Wl,--no-fatal-warnings" not in fragments[last:]
    # FW-16: the region check ESP-IDF's flash writes ask is the firmware's.
    assert REGION_WRAP in fragments
    # FW-19: the relocations the reachability gate reads stay in the image.
    assert EMIT_RELOCS in fragments
    # FW-18: the idle tasks' memory and PSA's init are the firmware's.
    for wrap in STARTUP_WRAPS:
        assert wrap in fragments, wrap
