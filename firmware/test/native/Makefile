# SPDX-FileCopyrightText: 2026 José M. Requena Plens
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Host tests of the firmware. The firmware's own sources are compiled unchanged
# against the fake ESP-IDF in harness/idf/, on the simulated board of
# harness/sim.h, at the device's C++ standard and with its strict warning set.
#
#   make -C firmware/test/native            # build and run every suite (sanitizers on)
#   make -C firmware/test/native test32     # the same at 32 bits (Linux, a multilib compiler)
#   make -C firmware/test/native coverage   # the same under LLVM's MC/DC instrumentation
#   make -C firmware/test/native bridge     # the firmware on stdin and stdout (bridge.cpp)
#   make -C firmware/test/native vectors    # golden/pinmap.vectors written again (test_pinmap.cpp)
#   make -C firmware/test/native clean
#
# `coverage` needs clang and LLVM 19 or later (llvm-profdata, llvm-cov); it
# writes build/coverage/coverage.json and build/coverage/junit/*.xml, which
# scripts/test_report.py turns into the test report (docs/testing.md).

CXX      ?= c++
SRC      := ../../src
BUILD    := build
# The device's standard: firmware/CMakeLists.txt pins gnu++26, which clang and
# GCC both accept as gnu++2c.
STD      := -std=gnu++2c
# The device's warning set, flag for flag: firmware/platformio.ini
# build_src_flags (tests/firmware/test_host_build_matches_device.py holds the
# two equal).
WARN     := -Wall -Wextra -Werror -Wshadow -Wnon-virtual-dtor -Wcast-align -Wunused \
	    -Woverloaded-virtual -Wformat=2 -Wold-style-cast -Wconversion -Wsign-conversion \
	    -Wdouble-promotion -Wcast-qual -Wnull-dereference -Wswitch-enum -Wundef \
	    -Wredundant-decls -Wvla -Walloca -Wduplicated-cond -Wduplicated-branches \
	    -Wlogical-op -Werror=unused-function -Werror=unused-variable \
	    -Werror=unused-but-set-variable -Werror=deprecated-declarations -Werror=extra \
	    -Wunused-parameter -Wsign-compare -Wenum-conversion
# Three of those are GCC's alone (-Wduplicated-cond, -Wduplicated-branches,
# -Wlogical-op): the device's compiler and the 32-bit leg's run them, and clang is told not to refuse what it does not know.
COMPAT   := $(if $(findstring clang,$(shell $(CXX) --version 2>/dev/null)),-Wno-unknown-warning-option)
INCLUDES := -Iharness/idf -Iharness -I$(SRC)
# A linker warning is an error, as on the device (firmware/CMakeLists.txt,
# -Wl,--fatal-warnings). Apple's ld spells the option its own way.
DARWIN   := $(filter Darwin,$(shell uname -s))
comma    := ,
LDFATAL  := $(if $(DARWIN),-Wl$(comma)-fatal_warnings,-Wl$(comma)--fatal-warnings)
LDFLAGS  ?= $(LDFATAL)
SAN      := -fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=undefined
CXXFLAGS ?= $(STD) -O1 -g $(WARN) $(COMPAT) $(SAN)
# The standard, the strict warnings and the fatal linker warnings whatever the
# shell exports: a CXXFLAGS or LDFLAGS in the environment (Homebrew, conda and
# Nix set them) replaces the defaults above, and a leg's own flags on the
# command line replace them too, so these go after either, last, where a
# -Wno-error before them cannot undo -Werror
# (tests/firmware/test_host_build_matches_device.py).
override CXXFLAGS += $(STD) $(WARN) $(COMPAT)
override LDFLAGS  += $(LDFATAL)
LDLIBS   := -lpthread

# LeakSanitizer where the platform has it (Linux); macOS's AddressSanitizer
# refuses detect_leaks=1 outright. lsan.supp names the allocations that live as
# long as the process by design; any other leak fails the run.
LEAKS    := $(if $(filter Linux,$(shell uname -s)),1,0)
SANENV   := ASAN_OPTIONS=detect_leaks=$(LEAKS) LSAN_OPTIONS=suppressions=$(CURDIR)/lsan.supp

# The 32-bit leg: std::size_t, long and pointers at the target's 32 bits, so a
# conversion or a format that only a 64-bit host forgives is caught.
# -malign-double gives a 64-bit integer inside a struct the 8-byte alignment
# Xtensa gives it and i386 does not, so the calibration blob layout calibration.cpp
# static_asserts hold on both.
M32FLAGS := $(STD) -m32 -malign-double -O1 -g $(WARN) $(COMPAT) $(SAN)

# The stack each test thread reserves, in KiB, when set (the 32-bit leg sets
# it): every simulated board's tasks are host threads that stay frozen for the
# life of the process (harness/sim.h), and a few hundred at the default 8 MiB
# do not fit a 32-bit address space beside AddressSanitizer's shadow. 512 KiB
# since 0.14.0: at 1 MiB the 185 cases of the firmware binary left
# AddressSanitizer's own allocator out of memory at exit (CI, 2026-11-20).
STACK_KB ?=

# When set (the 32-bit leg sets it), each suite runs in a process of its own,
# because those boards add up over one process: at 370 cases the address space
# was full before the last suite, and AddressSanitizer ended the run out of
# memory (CI, FW-30).
PER_SUITE ?=

# The firmware, as the device builds it, but for main.cpp, which only
# test_firmware links (the boot suite calls its app_main()).
FIRMWARE := $(SRC)/scpi.cpp $(SRC)/calibration.cpp $(SRC)/relay.cpp $(SRC)/ina228.cpp \
	    $(SRC)/sampler.cpp $(SRC)/console.cpp $(SRC)/console_commands.cpp \
	    $(SRC)/status_led.cpp $(SRC)/identity.cpp $(SRC)/heap_monitor.cpp $(SRC)/interrupt.cpp \
	    $(SRC)/watchdog.cpp $(SRC)/uart0.cpp $(SRC)/i2c_bus.cpp $(SRC)/flash_log.cpp \
	    $(SRC)/flash_port.cpp $(SRC)/pin_boot.cpp $(SRC)/pin_apply.cpp $(SRC)/pinmap_store.cpp \
	    $(SRC)/startup_memory.cpp $(SRC)/brownout.cpp $(SRC)/text.cpp
HARNESS  := harness/unit.cpp harness/sim.cpp harness/flash.cpp
HEADERS  := $(wildcard $(SRC)/*.h) $(wildcard harness/*.h) $(wildcard harness/idf/*.h) \
	    $(wildcard harness/idf/*/*.h)

# Each binary and its test files; the `boot` suite (test_boot.cpp) is
# app_main(). There is one image (0.8.0), so one build of the firmware. The
# boot cases had a binary of their own until FW-01: an inline function only
# the tests call at run time was never emitted there, and its empty coverage
# record, read beside test_firmware's, was llvm-cov's "mismatched data".
FIRMWARE_TESTS := test_bits.cpp test_scale.cpp test_calmath.cpp test_calibration.cpp test_relay.cpp \
	          test_ina228.cpp test_sampler.cpp test_console.cpp test_console_measure.cpp \
	          test_console_route.cpp test_console_cal.cpp test_console_edges.cpp \
	          test_console_sides.cpp test_sampler_sides.cpp test_led.cpp \
	          test_identity.cpp test_heap.cpp test_relay2.cpp test_statistics.cpp test_trigger.cpp \
	          test_health.cpp test_watchdog.cpp test_interrupt.cpp test_uart0.cpp \
	          test_i2c_bus.cpp test_flash_log.cpp test_flash_port.cpp test_startup_memory.cpp \
	          test_golden.cpp test_boot.cpp test_sweep.cpp test_pinmap.cpp test_console_pins.cpp \
	          test_pinmap_store.cpp test_pin_boot.cpp test_averaging.cpp

BINARIES := scpi firmware

# The golden transcript (test_golden.cpp, FW-24) is read and written by its
# absolute path, so the binary finds it from any directory.
GOLDEN   := $(CURDIR)/golden/console.transcript
%/test_golden.o: DEFINES = -DMETER_GOLDEN_FILE='"$(GOLDEN)"'

# The pin map's vectors (test_pinmap.cpp, FW-01): every verdict of
# pinmap::validate() that the simulator's and the panel's ports must give too,
# read and written by the absolute path, as the transcript is. `make vectors`
# writes the file again from the rules.
VECTORS  := $(CURDIR)/golden/pinmap.vectors
%/test_pinmap.o: DEFINES = -DMETER_PINMAP_VECTORS='"$(VECTORS)"'

vpath %.cpp $(SRC) harness .

name = $(notdir $(basename $(1)))
objs = $(addprefix $(BUILD)/obj/$(1)/,$(addsuffix .o,$(foreach f,$(2),$(call name,$(f)))))

STD_OBJS = $(call objs,std,$(FIRMWARE) $(HARNESS))

.PHONY: test test32 bridge coverage vectors clean
test: $(addprefix $(BUILD)/test_,$(BINARIES)) $(BUILD)/bridge
	@set -e; for b in $(BINARIES); do \
	  echo "== $$b"; $(if $(STACK_KB),ulimit -s $(STACK_KB);) \
	  $(if $(PER_SUITE),suites=$$(./$(BUILD)/test_$$b --suites); test -n "$$suites"; for s in $$suites; do \
	    echo "-- $$s"; $(SANENV) ./$(BUILD)/test_$$b --suite $$s; done; \
	  ,$(SANENV) ./$(BUILD)/test_$$b $(if $(JUNIT),--junit $(JUNIT)/$$b.xml);) \
	done
	@echo "== bridge"; $(if $(STACK_KB),ulimit -s $(STACK_KB);) \
	  printf '*IDN?\n' | ./$(BUILD)/bridge | grep -x 'KLEIDOS,KLEIDOS-METER,KM-[0-9A-F]*,[0-9.]*'

test32:
	$(MAKE) --no-print-directory BUILD=$(BUILD)/m32 CXXFLAGS="$(M32FLAGS)" LDFLAGS="$(LDFLAGS)" STACK_KB=512 PER_SUITE=1 test

# A warning -Werror does not reach fails the object as well: clang 21 gives
# "unsupported MC/DC boolean expression" for a decision it then leaves out of
# the coverage, and the diagnostic has no flag -Werror turns into an error.
.DELETE_ON_ERROR:
$(BUILD)/obj/std/%.o: %.cpp $(HEADERS) Makefile $(BUILD)/compiler
	@mkdir -p $(dir $@)
	@echo "  CXX $(notdir $<)"
	@$(CXX) $(CXXFLAGS) $(DEFINES) $(INCLUDES) -c $< -o $@ 2>$@.log; status=$$?; \
	  cat $@.log >&2; test $$status -eq 0 && ! grep -q 'warning:' $@.log

# The compiler the objects of this build directory were made with; another one
# makes them all again. run_tests.sh's and the commit hook's (c++) share the
# directory, and AddressSanitizer's objects of one do not link with the other's.
COMPILER := $(shell $(CXX) --version 2>/dev/null | head -n 1)
.PHONY: FORCE
$(BUILD)/compiler: FORCE
	@mkdir -p $(BUILD); echo '$(COMPILER)' | cmp -s - $@ || echo '$(COMPILER)' > $@

$(BUILD)/test_scpi: $(call objs,std,test_scpi.cpp test_scpi_views.cpp test_chars.cpp test_text.cpp \
	                         scpi.cpp text.cpp unit.cpp)
	@echo "  LD  $(notdir $@)"
	@$(CXX) $(CXXFLAGS) $(LDFLAGS) $^ -o $@ $(LDLIBS)

$(BUILD)/test_firmware: $(call objs,std,$(FIRMWARE_TESTS) main.cpp) $(STD_OBJS)
	@echo "  LD  $(notdir $@)"
	@$(CXX) $(CXXFLAGS) $(LDFLAGS) $^ -o $@ $(LDLIBS)

# The firmware whose UART0 is the process's standard input and output
# (bridge.cpp), for a host tool where there is no meter; `test` asks it its
# identity.
bridge: $(BUILD)/bridge

$(BUILD)/bridge: $(call objs,std,bridge.cpp $(FIRMWARE) harness/sim.cpp harness/flash.cpp)
	@echo "  LD  $(notdir $@)"
	@$(CXX) $(CXXFLAGS) $(LDFLAGS) $^ -o $@ $(LDLIBS)

# The vectors written again from pinmap::validate() (test_pinmap.cpp says when).
vectors: $(BUILD)/test_firmware
	METER_VECTORS_WRITE=1 ./$(BUILD)/test_firmware --suite pinmap-vectors

# --- Coverage: statement, branch and MC/DC, from one instrumented run --------

LLVM_PROFDATA ?= llvm-profdata
LLVM_COV      ?= llvm-cov
COV           := $(BUILD)/coverage
COVFLAGS      := $(STD) -O0 -g $(WARN) $(COMPAT) -fprofile-instr-generate -fcoverage-mapping -fcoverage-mcdc
# On macOS clang's driver asks Apple's ld to align the counter sections
# (__llvm_prf_cnts, _data, _bits) to 16 KiB, the largest Darwin page, so that
# continuous mode can map them; an x86_64 image's segments are aligned to
# 4 KiB, and ld warns that it reduced the alignment. Aligning the segments
# to 16 KiB as well, which arm64 already does, honors the request.
COVLDFLAGS    := $(LDFLAGS) $(if $(DARWIN),-Wl$(comma)-segalign$(comma)0x4000)
# llvm-cov's diagnostics, kept for scripts/test_report.py, which refuses a
# run that printed any (a "mismatched data" warning is coverage it dropped).
COVLOG        := $(COV)/llvm-cov.log

coverage:
	rm -rf $(COV) && mkdir -p $(COV)/junit $(COV)/profiles && : > $(COVLOG)
	$(MAKE) --no-print-directory BUILD=$(COV) CXXFLAGS="$(COVFLAGS)" LDFLAGS="$(COVLDFLAGS)" \
	  $(addprefix $(COV)/test_,$(BINARIES))
	@set -e; for b in $(BINARIES); do \
	  echo "== $$b (instrumented)"; \
	  LLVM_PROFILE_FILE=$(COV)/profiles/$$b.profraw ./$(COV)/test_$$b --junit $(COV)/junit/$$b.xml; \
	done
	$(LLVM_PROFDATA) merge -sparse $(COV)/profiles/*.profraw -o $(COV)/meter.profdata 2>>$(COVLOG)
	$(LLVM_COV) export -format=text -instr-profile=$(COV)/meter.profdata \
	  $(COV)/test_firmware -object $(COV)/test_scpi \
	  -ignore-filename-regex='(test/native/|/usr/|/Library/|/opt/)' > $(COV)/coverage.json 2>>$(COVLOG)
	$(LLVM_COV) report -show-mcdc-summary -show-branch-summary -instr-profile=$(COV)/meter.profdata \
	  $(COV)/test_firmware -object $(COV)/test_scpi \
	  -ignore-filename-regex='(test/native/|/usr/|/Library/|/opt/)' 2>>$(COVLOG) | tee $(COV)/summary.txt
	$(LLVM_COV) show -format=html -show-mcdc -show-branches=count -output-dir=$(COV)/html \
	  -instr-profile=$(COV)/meter.profdata \
	  $(COV)/test_firmware -object $(COV)/test_scpi \
	  -ignore-filename-regex='(test/native/|/usr/|/Library/|/opt/)' >/dev/null 2>>$(COVLOG)
	@if [ -s $(COVLOG) ]; then echo "llvm-cov and llvm-profdata said:" >&2; cat $(COVLOG) >&2; fi

clean:
	rm -rf $(BUILD)
