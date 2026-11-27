# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
# shellcheck shell=sh
#
# The single definition site of the analyzers' versions, as the firmware
# repository's scripts/clang_tidy_toolchain.sh is for its clang-tidy. Source
# it; do not execute it:
#
#   . scripts/analysis_toolchain.sh
#
# Read by scripts/run_static_analysis.sh, scripts/clang_tidy_gate.py (which
# parses the three assignments below) and CI's static-analysis job, which
# installs clang-tidy from its PyPI wheel at METER_CLANG_TIDY_VERSION and
# builds cppcheck METER_CPPCHECK_VERSION from its source archive, checked
# against METER_CPPCHECK_SHA256.
#
# The clang-tidy major is enforced (another major is another check set: the
# gate exits 2) and a different patch level of it only warns, because
# Homebrew, apt.llvm.org and PyPI publish patch releases at different times.
# cppcheck is enforced exactly: its checkers change in minor releases.

# LLVM major release the clang-tidy legs require.
METER_CLANG_TIDY_MAJOR=23

# Exact LLVM release CI installs (the clang-tidy wheel on PyPI).
METER_CLANG_TIDY_VERSION=23.1.4

# Exact cppcheck release the cppcheck leg requires, and the SHA-256 of its
# source archive (https://github.com/danmar/cppcheck/archive/refs/tags/2.21.0.tar.gz).
METER_CPPCHECK_VERSION=2.21.0
METER_CPPCHECK_SHA256=f028ff75ca5372738f3737c8b3e8611426a6526b6aea2ef01301ab0f5902f044

export METER_CLANG_TIDY_MAJOR METER_CLANG_TIDY_VERSION METER_CPPCHECK_VERSION METER_CPPCHECK_SHA256
