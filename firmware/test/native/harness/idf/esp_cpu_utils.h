// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// ESP-IDF 6.0.3's xtensa/include/esp_cpu_utils.h, as it is: the one function
// it holds is arithmetic on a return address, so the harness keeps it whole.
#include <cstdint>

/// The address of the call instruction a return address @p pc follows: the
/// window increment in its top two bits replaced by the code's address space,
/// and the call's three bytes taken off.
static inline std::uint32_t esp_cpu_process_stack_pc( std::uint32_t pc ) {
    if ( ( pc & 0x80000000U ) != 0U ) {
        pc = ( pc & 0x3fffffffU ) | 0x40000000U;
    }
    return pc - 3U;
}
