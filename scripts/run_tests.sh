#!/bin/sh
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Every test of the repository and the report, in one command (docs/testing.md):
#
#   scripts/run_tests.sh            # writes build/test-report/report.{md,html}
#
# 1. the firmware's host tests under AddressSanitizer and UBSan (and
#    LeakSanitizer on Linux), and the native bridge, the firmware on standard
#    input and output, which steps 3 and 4 then talk to;
# 2. the same tests under LLVM's MC/DC instrumentation, for the coverage;
# 3. the host package (the client, its simulator and the panel), with branch
#    coverage; its conformance suite runs on the simulator and on the bridge of
#    step 1, and a conformance case without the bridge fails. The page's browser
#    tests run when the [browser] extra is installed (CI's panel job);
# 4. the guards over the firmware sources (the AST census among them, with
#    the clang of step 2), and the calibration registry; both pytest runs
#    turn every warning into an error (-W error); and the board's pin table,
#    whose two generated copies must be what firmware/board/pins.toml makes;
# 5. the host package's mutants (scripts/run_mutants.sh), every one killed: all
#    of them where the [browser] extra and its Chromium are installed, those no
#    browser judges where they are not (CI's panel job runs the others). The
#    firmware's, half an hour and more, are run by hand and before a release;
# 6. scripts/test_report.py --gate: fails on a failed test, an MC/DC gap with
#    no written reason, more untaken branch sides without one than the
#    baseline of firmware/test/native/branch_exclusions.toml, any line
#    llvm-profdata or llvm-cov printed while exporting the coverage, a host
#    module under its branch floor (host/branch_floors.toml) and, in CI, a test
#    skipped for a reason that names no place where it runs instead.
#
# The 32-bit leg is `make -C firmware/test/native test32 CXX=g++` (Linux,
# g++-multilib; CI's native32 job).
#
# LLVM 19 or later is needed for step 2. LLVM_BIN names its bin directory when
# clang++ on PATH is older or is Apple's (Homebrew: /opt/homebrew/opt/llvm/bin
# or /usr/local/opt/llvm/bin).
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
out="$root/build/test-report"
native="$root/firmware/test/native"

if [ -z "${LLVM_BIN:-}" ]; then
    for candidate in /opt/homebrew/opt/llvm/bin /usr/local/opt/llvm/bin; do
        if [ -x "$candidate/clang++" ]; then LLVM_BIN=$candidate; break; fi
    done
fi
if [ -n "${LLVM_BIN:-}" ]; then
    cxx="$LLVM_BIN/clang++"; profdata="$LLVM_BIN/llvm-profdata"; cov="$LLVM_BIN/llvm-cov"
else
    cxx=${CXX:-clang++}; profdata=llvm-profdata; cov=llvm-cov
fi

if [ -x "$root/host/.venv/bin/python" ]; then python="$root/host/.venv/bin/python"; else python=python3; fi
jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)

rm -rf "$out"
mkdir -p "$out/native"

echo "== firmware host tests, sanitizers on"
make -C "$native" -j"$jobs" CXX="$cxx" test

echo "== firmware host tests, MC/DC instrumentation"
make -C "$native" -j"$jobs" coverage CXX="$cxx" LLVM_PROFDATA="$profdata" LLVM_COV="$cov"
cp "$native"/build/coverage/junit/*.xml "$out/native/"
cp "$native/build/coverage/coverage.json" "$out/coverage.json"
cp "$native/build/coverage/summary.txt" "$out/native-summary.txt"
cp "$native/build/coverage/llvm-cov.log" "$out/native-llvm-cov.log"
cp -R "$native/build/coverage/html" "$out/native-html"

# Step 1 built the bridge and the test binaries. The host's conformance suite
# asks the bridge, and it and the guards that need either
# (tests/firmware/test_native_bridge.py and test_native_runner.py) fail rather
# than skip without them.
KLEIDOS_METER_NATIVE_BRIDGE="$native/build/bridge"
METER_BRIDGE_REQUIRED=1
export KLEIDOS_METER_NATIVE_BRIDGE METER_BRIDGE_REQUIRED

echo "== host package: client, simulator, panel and conformance, branch coverage"
(cd "$root/host" && "$python" -m pytest -q -W error -p no:cacheprovider --junitxml="$out/host.xml" \
    --cov --cov-report=term --cov-report=json:"$out/host-coverage.json")

echo "== guards over the firmware sources"
# The AST census (tests/firmware/test_ast_census.py) parses the sources with
# the clang this script found for the coverage.
METER_CLANG=$(command -v "$cxx" || echo "$cxx")
export METER_CLANG
(cd "$root" && "$python" -m pytest -q -W error -p no:cacheprovider tests --junitxml="$out/guards.xml")

echo "== the board's pin table and its two generated copies"
"$python" "$root/scripts/gen_pin_table.py" --check

echo "== the calibration registry and its index"
"$python" "$root/scripts/check_calibration_registry.py"

echo "== the host package's mutants"
if "$python" - <<'EOF'
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit(1)
with sync_playwright() as playwright:
    sys.exit(not Path(playwright.chromium.executable_path).is_file())
EOF
then
    mutants=--host
else
    mutants=--package
    echo "   no Chromium here: the page's mutants run in CI's panel job"
fi
PYTHON="$python" "$root/scripts/run_mutants.sh" "$mutants"

echo "== report"
"$python" "$root/scripts/test_report.py" --gate
