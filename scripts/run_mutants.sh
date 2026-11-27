#!/bin/sh
# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
#
# The mutants, each a defect the tests must catch (docs/testing.md,
# "Mutants"):
#
#   scripts/run_mutants.sh                # every patch of both lists
#   scripts/run_mutants.sh F2b FW29-ese   # those named
#   scripts/run_mutants.sh --firmware     # every patch of scripts/mutants/
#   scripts/run_mutants.sh --host         # every patch of host/mutants/
#   scripts/run_mutants.sh --package      # of those, the ones no browser judges
#   scripts/run_mutants.sh --page         # and the ones a browser does
#
# scripts/run_tests.sh runs the host package's in every run, CI's panel job
# the page's, which its browser judges.
#
# Each scripts/mutants/<name>.patch is one small wrong change to
# firmware/src, and each host/mutants/<name>.patch one to the host package,
# with its name and what it breaks on its `# Mutant` line. Both are applied to
# copies in a temporary directory; the checkout is never touched. A mutant is
# killed when the tests fail on it; one they pass on has survived, and one that
# does not apply or cannot be judged is an error. The exit status is 0 only
# when every mutant run was killed.
#
# A firmware mutant is judged by the two host test binaries: they are built in
# the copy once and run, which must pass, then for each mutant the script
# applies its patch, rebuilds what it changed, runs both binaries and takes the
# patch back out. One that does not build is an error.
#
# A host mutant is judged by the command its `# Kill:` line gives, run with
# `sh -c` at the root of a copy of the checkout's files (the tracked ones and
# the new ones, nothing ignored): it must pass there before the patch and fail
# with it, by the exit status 1 of tests that failed; another (2, pytest's for
# tests it could not collect: a mutant that does not even import) is
# an error. A patch with no `# Kill:` line, or more than one, is an error.
# PYTHON names the Python those commands run on, host/.venv's when there is
# one; nothing is compiled to bytecode there, so a patch is never judged on a
# .pyc of the file it changed.
#
# The binaries are built without the sanitizers and with warnings off (-w):
# what is asked here is whether a test fails, not whether the mutant is clean
# code. A mutant takes about half a minute.
#
# LLVM_BIN names a clang to use, as in scripts/run_tests.sh; CXX otherwise.
set -eu

root=$(cd "$(dirname "$0")/.." && pwd)
firmware_mutants="$root/scripts/mutants"
host_mutants="$root/host/mutants"

if [ -z "${LLVM_BIN:-}" ]; then
    for candidate in /opt/homebrew/opt/llvm/bin /usr/local/opt/llvm/bin; do
        if [ -x "$candidate/clang++" ]; then LLVM_BIN=$candidate; break; fi
    done
fi
if [ -n "${LLVM_BIN:-}" ]; then cxx="$LLVM_BIN/clang++"; else cxx=${CXX:-clang++}; fi
jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)

# The patches of one list: $1 is its directory, which may not exist.
every() {
    for patch in "$1"/*.patch; do
        if [ -f "$patch" ]; then printf '%s\n' "$patch"; fi
    done
}

# The host's patches a browser judges (--page: the kill command asks for one)
# or does not (--package).
host_list() {
    every "$host_mutants" | while read -r patch; do
        if grep -q '^# Kill: .*METER_BROWSER_REQUIRED=1' "$patch"; then kind=--page; else kind=--package; fi
        if [ "$1" = "$kind" ]; then printf '%s\n' "$patch"; fi
    done
}

# A list asked for by its name that holds no mutant is an error, not a pass.
some() {
    if [ -z "$2" ]; then
        echo "run_mutants: no mutant in $1" >&2
        exit 2
    fi
}

firmware_patches=""
host_patches=""
if [ "$#" -eq 0 ]; then
    firmware_patches=$(every "$firmware_mutants")
    host_patches=$(every "$host_mutants")
    some "scripts/mutants/ or host/mutants/" "$firmware_patches$host_patches"
fi
for name in "$@"; do
    if [ "$name" = "--firmware" ]; then
        found=$(every "$firmware_mutants")
        some "$name" "$found"
        firmware_patches="$firmware_patches $found"
    elif [ "$name" = "--host" ]; then
        found=$(every "$host_mutants")
        some "$name" "$found"
        host_patches="$host_patches $found"
    elif [ "$name" = "--package" ] || [ "$name" = "--page" ]; then
        found=$(host_list "$name")
        some "$name" "$found"
        host_patches="$host_patches $found"
    elif [ -f "$firmware_mutants/$name.patch" ]; then
        firmware_patches="$firmware_patches $firmware_mutants/$name.patch"
    elif [ -f "$host_mutants/$name.patch" ]; then
        host_patches="$host_patches $host_mutants/$name.patch"
    else
        echo "run_mutants: no mutant $name (scripts/mutants/ or host/mutants/)" >&2
        exit 2
    fi
done

work=$(mktemp -d "${TMPDIR:-/tmp}/meter-mutants.XXXXXX")
trap 'rm -rf "$work"' EXIT INT TERM
log="$work/run.log"

survived=0
errors=0
total=0

# One mutant of the copy at $2: applies the patch $1, runs the function $3 on
# it, which returns 0 for tests that passed, 1 for tests that failed and 2 for
# a mutant they could not be run on, says what came of it and takes the patch
# back out.
judge() {
    patch=$1
    name=$(basename "$patch" .patch)
    what=$(sed -n 's/^# Mutant [^:]*: //p' "$patch")
    total=$((total + 1))
    if ! (cd "$2" && git apply "$patch") 2>"$log"; then
        echo "ERROR    $name: the patch does not apply ($(head -1 "$log"))"
        errors=$((errors + 1))
        return 0
    fi
    set +e
    "$3"
    outcome=$?
    set -e
    case $outcome in
        0)
            echo "SURVIVED $name: $what"
            survived=$((survived + 1))
            ;;
        1)
            first=$(grep -m 1 -E '^\[FAIL\]|^FAILED |^ERROR ' "$log" || true)
            echo "KILLED   $name: ${first:-its tests failed}"
            ;;
        *)
            echo "ERROR    $name: the mutant does not build, or its tests do not run"
            grep -m 3 -i 'error' "$log" | sed 's/^/         /' || true
            errors=$((errors + 1))
            ;;
    esac
    (cd "$2" && git apply -R "$patch")
}

# --- The firmware's ----------------------------------------------------------

native="$work/firmware/test/native"

# Builds both binaries and runs them.
build_and_run() {
    make -C "$native" -j"$jobs" BUILD=build/mut CXX="$cxx" \
        CXXFLAGS="-std=gnu++2c -O1 -g -w" build/mut/test_firmware build/mut/test_scpi \
        >"$log" 2>&1 || return 2
    status=0
    for binary in test_scpi test_firmware; do
        (cd "$native" && "./build/mut/$binary") >>"$log" 2>&1 || status=1
    done
    return "$status"
}

# The lists are walked by word splitting: the checkout's path holds no space.
# shellcheck disable=SC2086
set -- $firmware_patches
if [ "$#" -gt 0 ]; then
    mkdir -p "$work/firmware/test"
    cp -R "$root/firmware/src" "$work/firmware/"
    cp -R "$root/firmware/test/native" "$work/firmware/test/"
    rm -rf "$work/firmware/test/native/build"
    echo "== the unmutated firmware"
    if build_and_run; then
        echo "   passes"
    else
        echo "run_mutants: the unmutated tree does not build or does not pass:" >&2
        tail -20 "$log" >&2
        exit 2
    fi
    for patch in "$@"; do judge "$patch" "$work" build_and_run; done
fi

# --- The host package's ------------------------------------------------------

if [ -x "$root/host/.venv/bin/python" ]; then
    PYTHON=${PYTHON:-"$root/host/.venv/bin/python"}
else
    PYTHON=${PYTHON:-python3}
fi
PYTHONDONTWRITEBYTECODE=1
export PYTHON PYTHONDONTWRITEBYTECODE
tree="$work/tree"

# The kill command of the mutant being judged, at the copy's root.
run_kill() {
    status=0
    (cd "$tree" && sh -c "$kill_command") >"$log" 2>&1 || status=$?
    case $status in
        0 | 1) return "$status" ;;
        *) return 2 ;;
    esac
}

# shellcheck disable=SC2086
set -- $host_patches
if [ "$#" -gt 0 ]; then
    mkdir -p "$tree"
    git -C "$root" ls-files -z --cached --others --exclude-standard \
        | (cd "$root" && tar --null -T - -cf -) | (cd "$tree" && tar -xf -)
    echo "== the unmutated host package, by each kill command"
    passing="$work/passing"
    : >"$passing"
    for patch in "$@"; do
        name=$(basename "$patch" .patch)
        if [ "$(grep -c '^# Kill: ' "$patch")" != 1 ]; then
            echo "ERROR    $name: one '# Kill:' line is asked"
            total=$((total + 1))
            errors=$((errors + 1))
            continue
        fi
        kill_command=$(sed -n 's/^# Kill: //p' "$patch")
        # Each command once on the unmutated copy: one that fails there would
        # kill whatever it is given.
        if ! grep -qxF -- "$kill_command" "$passing"; then
            if run_kill; then
                printf '%s\n' "$kill_command" >>"$passing"
            else
                echo "ERROR    $name: its kill command fails on the unmutated tree: $kill_command"
                tail -5 "$log" | sed 's/^/         /'
                total=$((total + 1))
                errors=$((errors + 1))
                continue
            fi
        fi
        judge "$patch" "$tree" run_kill
    done
fi

echo "== $total mutant(s): $((total - survived - errors)) killed, $survived survived, $errors error(s)"
if [ "$survived" -ne 0 ] || [ "$errors" -ne 0 ]; then
    exit 1
fi
