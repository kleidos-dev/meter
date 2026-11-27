// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The backtrace walk of ESP-IDF 6.0.3's esp_debug_helpers.h, over a stack the
// test scripts (sim::World::backtrace): a host thread's own frames say
// nothing about the device's, so the census's walk climbs the frames a test
// gives it instead.
#include <cstdint>

/// One frame of the walk, as ESP-IDF's Xtensa port defines it.
typedef struct {
    std::uint32_t pc;         ///< The frame's PC.
    std::uint32_t sp;         ///< The frame's stack pointer.
    std::uint32_t next_pc;    ///< The PC of the frame's caller, 0 past the last.
    const void*   exc_frame;  ///< The exception frame, when there is one.
} esp_backtrace_frame_t;

/// The walk's first frame: the caller of the function that calls this, its
/// stack pointer, and its caller's PC (the script's first, 0 when it has none).
void esp_backtrace_get_start( std::uint32_t* pc, std::uint32_t* sp, std::uint32_t* next_pc );

/// Climbs one frame: @p frame becomes its caller. False when the script marks
/// the frame reached as not sane (sim::World::backtraceInvalidAt).
bool esp_backtrace_get_next_frame( esp_backtrace_frame_t* frame );
