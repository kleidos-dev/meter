# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
"""The host tests are compiled as the device is: the same standard, the same warnings.

``docs/testing.md`` says the firmware is tested on the host "as it is built".
That holds only while ``firmware/test/native/Makefile`` keeps the device's C++
standard (``firmware/CMakeLists.txt``, gnu++26, which compilers spell
gnu++2c until it is published) and its strict warning set
(``firmware/platformio.ini`` ``build_src_flags``), flag for flag. This guard
holds them together, and keeps the 32-bit leg and leak detection in place.

Host-only: the build files read as text, and one object made with a fake
compiler. No build.
"""

from __future__ import annotations

import configparser
import re
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "firmware" / "test" / "native" / "Makefile"
METER_INI = ROOT / "firmware" / "platformio.ini"
CMAKELISTS = ROOT / "firmware" / "CMakeLists.txt"


def make_variable(name: str) -> str:
    text = MAKEFILE.read_text(encoding="utf-8")
    found = re.search(rf"^{name}\s*:=\s*((?:.*\\\n)*.*)$", text, re.MULTILINE)
    assert found, f"Makefile defines no {name}"
    return found.group(1).replace("\\\n", " ")


#: Flags the device builds with and the host does not, each with its reason.
#: ``-Wstack-usage`` bounds a function's frame on the device's compiler, at the
#: device's optimization; the host builds at -O1 under AddressSanitizer, whose
#: redzones grow every frame, so the same bound there would measure the
#: sanitizer rather than the firmware (0.14.0, docs/misra.md).
DEVICE_ONLY = {"-Wstack-usage=1024"}


def test_the_host_warnings_are_the_devices() -> None:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(METER_INI, encoding="utf-8")
    device = set(ini["env:meter"]["build_src_flags"].split()) - DEVICE_ONLY
    host = set(make_variable("WARN").split())
    assert host == device, (
        f"only on the device: {sorted(device - host)}; only on the host: {sorted(host - device)}"
    )


def test_the_host_standard_is_the_devices() -> None:
    device = re.search(r"-std=gnu\+\+(\d+)", CMAKELISTS.read_text(encoding="utf-8"))
    assert device and device.group(1) == "26", "CMakeLists.txt pins gnu++26"
    assert make_variable("STD").split() == ["-std=gnu++2c"]


def test_the_32_bit_leg_and_leak_detection_stay() -> None:
    assert "-m32" in make_variable("M32FLAGS").split()
    assert "-malign-double" in make_variable("M32FLAGS").split()
    assert "detect_leaks=$(LEAKS)" in make_variable("SANENV")
    workflow = (ROOT / ".github" / "workflows" / "check.yml").read_text(encoding="utf-8")
    assert "test32" in workflow, "check.yml must run the 32-bit leg"
    recipe = re.search(r"^test32:\n\t(.*)$", MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert recipe, "the Makefile has no test32 recipe"
    wanted = {"STACK_KB=512", "PER_SUITE=1", "test"}
    missing = wanted - set(recipe.group(1).split())
    assert not missing, f"one 32-bit process does not hold every suite's boards: {missing}"


def test_the_host_switches_no_warning_off() -> None:
    """The device's set has no ``-Wno-`` (FW-01), and neither has the host's.

    ``COMPAT``'s ``-Wno-unknown-warning-option`` is not a warning of the
    firmware's: it lets clang accept the three flags only GCC knows.
    """
    assert [flag for flag in make_variable("WARN").split() if flag.startswith("-Wno-")] == []
    assert make_variable("COMPAT").endswith("-Wno-unknown-warning-option)")


def test_the_host_links_with_fatal_warnings() -> None:
    """Both test binaries and the bridge link with the linker's warnings fatal, as the image does.

    The device's option is ``-Wl,--fatal-warnings`` (``firmware/CMakeLists.txt``);
    Apple's ld spells it ``-fatal_warnings``. The sanitizer, 32-bit and coverage
    builds all link through ``LDFLAGS``, the coverage one adding the 16 KiB
    segment alignment that keeps an x86_64 Mac's link free of the warning about
    the profile counters' sections.
    """
    ldfatal = make_variable("LDFATAL")
    assert "-Wl$(comma)-fatal_warnings" in ldfatal and "-Wl$(comma)--fatal-warnings" in ldfatal
    text = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^LDFLAGS\s*\?=\s*\$\(LDFATAL\)$", text, re.MULTILINE)
    links = re.findall(r"^\t@\$\(CXX\) .* -o \$@ \$\(LDLIBS\)$", text, re.MULTILINE)
    assert len(links) == 3 and all("$(LDFLAGS)" in line for line in links), links
    assert 'LDFLAGS="$(LDFLAGS)"' in text, "the 32-bit leg links with the same flags"
    assert 'LDFLAGS="$(COVLDFLAGS)"' in text, "the coverage build links with its own"
    coverage = make_variable("COVLDFLAGS")
    assert coverage.startswith("$(LDFLAGS)") and "-segalign$(comma)0x4000" in coverage


def test_an_object_is_rebuilt_when_its_flags_change() -> None:
    """A flag changed in the Makefile reaches every object, not only those a source edit touches."""
    rule = r"^\$\(BUILD\)/obj/std/%\.o: %\.cpp \$\(HEADERS\) Makefile \$\(BUILD\)/compiler$"
    assert re.search(rule, MAKEFILE.read_text(encoding="utf-8"), re.MULTILINE)


#: A compiler that names itself to ``--version``, and otherwise counts a run,
#: writes its output (the argument after ``-o``, the last), prints what it was
#: built to print and ends with the status it was built to end with.
FAKE_CXX = """#!/bin/sh
if [ "$1" = --version ]; then echo '{version}'; exit 0; fi
for last; do :; done
echo object > "$last"
echo run >> "$0.runs"
printf '%s\\n' 'a.cpp:1:2: {diagnostic}' >&2
exit {status}
"""


def make_one_object(
    tmp_path: Path, diagnostic: str, status: int = 0, version: str = "fake 1"
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """``scpi.o`` made under *tmp_path* by the compiler described, and its path."""
    compiler = tmp_path / "cxx"
    text = FAKE_CXX.format(diagnostic=diagnostic, status=status, version=version)
    compiler.write_text(text, encoding="utf-8")
    compiler.chmod(0o755)
    target = tmp_path / "build" / "obj" / "std" / "scpi.o"
    made = subprocess.run(
        ["make", "-C", MAKEFILE.parent, f"BUILD={tmp_path / 'build'}", f"CXX={compiler}", target],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return made, target


@pytest.mark.parametrize(
    ("diagnostic", "status", "kept"),
    [
        ("note: nothing to see", 0, True),
        ("warning: unsupported MC/DC boolean expression", 0, False),
        ("error: refused", 1, False),
    ],
)
def test_a_warning_no_flag_makes_an_error_fails_the_object(
    tmp_path: Path, diagnostic: str, status: int, kept: bool
) -> None:
    """And leaves no object behind, or the next make would take it as built."""
    made, target = make_one_object(tmp_path, diagnostic, status)
    assert diagnostic in made.stderr, made.stdout + made.stderr
    assert (made.returncode == 0) is kept, made.stdout + made.stderr
    assert target.exists() is kept


def test_another_compiler_makes_the_objects_again(tmp_path: Path) -> None:
    """And the same one does not: two compilers' AddressSanitizer objects do not link."""
    runs = tmp_path / "cxx.runs"
    for version, count in (("fake 1", 1), ("fake 1", 1), ("fake 2", 2), ("fake 2", 2)):
        made, _ = make_one_object(tmp_path, "note: nothing", version=version)
        assert made.returncode == 0, made.stdout + made.stderr
        assert runs.read_text(encoding="utf-8").count("run") == count, version
        time.sleep(1.1)  # macOS's make 3.81 compares times of whole seconds


def test_the_device_only_flags_are_on_the_device() -> None:
    ini = configparser.ConfigParser(interpolation=None)
    ini.read(METER_INI, encoding="utf-8")
    device = set(ini["env:meter"]["build_src_flags"].split())
    assert device >= DEVICE_ONLY, f"no longer on the device: {sorted(DEVICE_ONLY - device)}"


def dry_run(tmp_path: Path, environment: dict[str, str], *goals: str) -> str:
    """What ``make -n`` would run for *goals*, with *environment* exported."""
    import os

    env = {**os.environ, **environment}
    build = tmp_path / "build"
    return subprocess.run(
        ["make", "-n", "-C", str(MAKEFILE.parent), f"BUILD={build}", *goals],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.mark.parametrize(
    "environment",
    [{}, {"CXXFLAGS": "-O1"}, {"CXXFLAGS": "-O2 -Wno-error", "LDFLAGS": "-L/opt/lib"}],
    ids=["clean", "cxxflags", "both"],
)
def test_the_shell_cannot_take_the_warnings_off(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    """A CXXFLAGS or LDFLAGS exported by the shell (Homebrew, conda, Nix set
    them) keeps the standard, the strict warnings and their -Werror on every
    compile, last so that a -Wno-error before them is undone, and the fatal
    linker warnings on every link."""
    build = tmp_path / "build"
    lines = dry_run(
        tmp_path, environment, f"{build}/obj/std/scpi.o", f"{build}/test_scpi"
    ).splitlines()
    compiles = [line for line in lines if " -c " in line and "scpi.cpp" in line]
    links = [line for line in lines if " -o " in line and line.rstrip().endswith("-lpthread")]
    assert compiles and links, lines
    for line in compiles:
        flags = line.split()
        for flag in ("-std=gnu++2c", "-Werror", "-Wconversion", "-Wsign-conversion"):
            assert flag in flags, (flag, line)
        if "-Wno-error" in flags:
            assert flags.index("-Wno-error") < len(flags) - flags[::-1].index("-Werror") - 1
    fatal = (
        "-Wl,-fatal_warnings"
        if "Darwin" in subprocess.check_output(["uname", "-s"], text=True)
        else "-Wl,--fatal-warnings"
    )
    for line in links:
        assert fatal in line.split(), line
