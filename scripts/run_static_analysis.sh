#!/bin/sh
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The firmware's formatter and static analyzers, in one command:
#
#   scripts/run_static_analysis.sh               # format, both clang-tidy legs, cppcheck
#   scripts/run_static_analysis.sh format        # only the format check
#   scripts/run_static_analysis.sh tidy          # both clang-tidy legs
#   scripts/run_static_analysis.sh tidy-device   # clang-tidy as the device compiles
#   scripts/run_static_analysis.sh tidy-harness  # clang-tidy as the host tests compile
#   scripts/run_static_analysis.sh cppcheck      # only cppcheck
#   scripts/run_static_analysis.sh includes      # include-cleaner report (advisory)
#   scripts/run_static_analysis.sh selftest      # prove the clang-tidy guards
#
# 1. clang-format --dry-run --Werror over firmware/src and firmware/test/native
#    (.clang-format; clang-format 23, as pre-commit and CI pin it);
# 2. clang-tidy, every finding an error (.clang-tidy), twice, through
#    scripts/clang_tidy_gate.py: the device leg reads each source's compile
#    command from firmware/compile_commands.json (`pio run --project-dir
#    firmware -e meter -t compiledb`, through the firmware repository's
#    scripts/pio-job) and analyses it for the Xtensa target against the real
#    ESP-IDF, libstdc++ and picolibc headers; the harness leg analyses it as
#    the host tests compile it, against the fake ESP-IDF of
#    firmware/test/native/harness/idf. Both run with -fno-exceptions -fno-rtti,
#    as the image is built, and with no C++ modules (-fno-cxx-modules), which
#    neither build compiles;
# 3. cppcheck over the same sources with the firmware repository's flags, every
#    finding an error (.cppcheck-suppress names the few left out, each with
#    its reason), against the fake ESP-IDF.
#
# The guards are the firmware repository's: a clang-tidy of the LLVM major
# scripts/analysis_toolchain.sh pins, a configuration --verify-config accepts,
# exactly the translation units of firmware/src/CMakeLists.txt analyzed, and
# no parse diagnostic in any of them; the cppcheck release that file pins.
# Exit status: 0 clean, 1 a finding, 2 a guard failed (the analysis did not
# run as it claims). The steps run in order and the worst status is returned.
#
# CLANG_FORMAT, CLANG_TIDY and CPPCHECK name the binaries when the ones found
# are not the pinned ones; METER_XTENSA_TOOLCHAIN names the Xtensa toolchain's
# directory when it is not PlatformIO's.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
what=${1:-all}

# shellcheck source=scripts/analysis_toolchain.sh
. "$root/scripts/analysis_toolchain.sh"

clang_format=${CLANG_FORMAT:-clang-format}
cppcheck=${CPPCHECK:-cppcheck}
gate="python3 scripts/clang_tidy_gate.py"

harness=firmware/test/native/harness
formatted=$(git ls-files 'firmware/src/*.cpp' 'firmware/src/*.h' 'firmware/test/native/*.cpp' \
    'firmware/test/native/*.h' | grep -E '\.(cpp|h)$')

status=0

# Keeps the worst exit status: a guard failure (2) outranks a finding (1).
note() {
    if [ "$1" -gt "$status" ]; then
        status=$1
    fi
}

case "$what" in
    all | format | tidy | tidy-device | tidy-harness | cppcheck | includes | selftest) ;;
    *)
        echo "unknown step: $what" >&2
        exit 2
        ;;
esac

if [ "$what" = all ] || [ "$what" = format ]; then
    echo "== clang-format ($("$clang_format" --version))"
    rc=0
    # shellcheck disable=SC2086 # word splitting of the file list is intended
    "$clang_format" --dry-run --Werror $formatted || rc=1
    note "$rc"
fi

if [ "$what" = all ] || [ "$what" = tidy ] || [ "$what" = tidy-device ]; then
    rc=0
    $gate --leg device || rc=$?
    note "$rc"
fi

if [ "$what" = all ] || [ "$what" = tidy ] || [ "$what" = tidy-harness ]; then
    rc=0
    $gate --leg harness || rc=$?
    note "$rc"
fi

if [ "$what" = includes ]; then
    rc=0
    $gate --include-cleaner || rc=$?
    note "$rc"
fi

if [ "$what" = selftest ]; then
    rc=0
    $gate --selftest || rc=$?
    note "$rc"
fi

if [ "$what" = all ] || [ "$what" = cppcheck ]; then
    found=$("$cppcheck" --version 2>/dev/null || true)
    echo "== cppcheck ($found)"
    if [ "$found" != "Cppcheck $METER_CPPCHECK_VERSION" ]; then
        echo "GATE FAILURE (guard): '$cppcheck' is '${found:-missing}', the pin is Cppcheck" \
            "$METER_CPPCHECK_VERSION (scripts/analysis_toolchain.sh): another release runs other checkers" >&2
        note 2
    else
        sources=$($gate --print-sources) || { note 2; sources=; }
        rc=0
        # shellcheck disable=SC2086
        "$cppcheck" --enable=warning,style,performance,portability --check-level=exhaustive \
            --inline-suppr --std=c++20 --language=c++ --suppressions-list=.cppcheck-suppress \
            --error-exitcode=1 --quiet -I"$harness/idf" -I"$harness" -Ifirmware/src $sources \
            || rc=1
        note "$rc"
    fi
fi

exit "$status"
