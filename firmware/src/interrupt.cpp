// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file interrupt.cpp
 * @ingroup interrupt_routing
 * @brief The static interrupt route: a free line found, claimed and enabled.
 */
#include "interrupt.h"

// ESP-IDF 6.1.1's xtensa_api.h declares the port's handler-table calls
// (xt_set_interrupt_handler(), xt_int_has_handler(), xt_ints_on()) with no C
// linkage block, and esp_cpu.h includes it before opening its own: from C++
// its inline wrappers would call C++-mangled names nothing defines. Included
// first, inside one, its guard keeps esp_cpu.h's include from reopening it.
extern "C" {
#include <xtensa_api.h>
}

#include <esp_cpu.h>
#include <esp_rom_sys.h>
#include <freertos/FreeRTOS.h>

namespace kleidos::meter::interrupt {

namespace {

/// @brief The facts of @p line of the calling core, read from the CPU's
///        descriptor table and the port's handler table.
/// @param[in] core The calling core (the handler table read is always its).
/// @param[in] line The CPU line, 0 to kLineCount - 1.
/// @return The line's facts.
LineFacts factsOf( std::int32_t core, std::int32_t line ) noexcept {
    esp_cpu_intr_desc_t descriptor = {};
    esp_cpu_intr_get_desc( core, line, &descriptor );
    const std::uint32_t flags = descriptor.flags;
    LineFacts           facts;
    facts.priority  = descriptor.priority;
    facts.levelType = descriptor.type == ESP_CPU_INTR_TYPE_LEVEL;
    facts.reserved  = ( flags & static_cast<std::uint32_t>( ESP_CPU_INTR_DESC_FLAG_RESVD ) ) != 0U;
    facts.special = ( flags & static_cast<std::uint32_t>( ESP_CPU_INTR_DESC_FLAG_SPECIAL ) ) != 0U;
    facts.hasHandler = esp_cpu_intr_has_handler( line );
    return facts;
}

/// @brief The lowest-numbered free line of the calling core.
/// @param[in] core The calling core.
/// @return The line, or -1 when none is free.
std::int32_t firstFreeLine( std::int32_t core ) noexcept {
    for ( std::int32_t line = 0; line < kLineCount; ++line ) {
        if ( lineIsFree( factsOf( core, line ) ) ) {
            return line;
        }
    }
    return -1;
}

}  // namespace

bool lineIsFree( const LineFacts& facts ) noexcept {
    return facts.priority == kRoutedPriority && facts.levelType && !facts.reserved && !facts.special
           && !facts.hasHandler;
}

esp_err_t routeHere( std::uint32_t source, Handler handler, void* arg, Route& route ) noexcept {
    if ( handler == nullptr ) {
        return ESP_ERR_INVALID_ARG;
    }
    const std::int32_t core = esp_cpu_get_core_id();

    // The search and the claim in one step on this core: with its interrupts
    // masked no other task of it runs, and only one of its tasks can claim a
    // line of it (interrupt.h).
    const UBaseType_t  masked = portSET_INTERRUPT_MASK_FROM_ISR();
    const std::int32_t line   = firstFreeLine( core );
    if ( line >= 0 ) {
        const auto bit = static_cast<std::uint32_t>( line );
        esp_cpu_intr_set_handler( line, handler, arg );
        esp_rom_route_intr_matrix( core, source, bit );
        esp_cpu_intr_enable( 1U << bit );
    }
    portCLEAR_INTERRUPT_MASK_FROM_ISR( masked );

    if ( line < 0 ) {
        return ESP_ERR_NOT_FOUND;
    }
    route.core = core;
    route.line = line;
    return ESP_OK;
}

}  // namespace kleidos::meter::interrupt
