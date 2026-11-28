// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file interrupt.h
 * @ingroup interrupt_routing
 * @brief A peripheral's interrupt routed to a CPU line, with nothing
 *        allocated.
 *
 * ESP-IDF's interrupt allocator (`esp_intr_alloc()`) keeps a record for each
 * CPU line it hands out and for each handler on it, and allocates both on the
 * heap (docs/misra.md D1). For one interrupt with one handler that is never
 * taken down, none of that bookkeeping is needed: routeHere() does what the
 * allocator does for a non-shared, level-1 interrupt, on the CPU's own
 * tables and no other:
 *
 *  1. it picks the lowest-numbered line of the calling core that is free
 *     (lineIsFree(): priority 1, level-triggered, neither reserved nor
 *     special in the CPU's descriptor table, `esp_cpu_intr_get_desc()`, and
 *     with no handler yet);
 *  2. sets the handler in the Xtensa port's static handler table
 *     (`esp_cpu_intr_set_handler()`, `_xt_interrupt_table`);
 *  3. routes the peripheral's source to that line through the interrupt
 *     matrix (`esp_rom_route_intr_matrix()`, the ROM's);
 *  4. enables the line (`esp_cpu_intr_enable()`).
 *
 * The peripheral's own enable bit still gates the interrupt: a route is made
 * once, at boot, and the owner turns its source on and off at the
 * peripheral (the sampler, at the GPIO's `int_ena`; the console's UART, at
 * UART0's).
 *
 * **Beside ESP-IDF's allocator.** Since FW-14 no driver the firmware calls
 * allocates its interrupt (the I2C master's on Core 1 did until then, UART0's
 * until FW-12), but ESP-IDF's own startup still claims lines through the
 * allocator (`docs/misra.md` D1), and each skips the other's: ESP-IDF
 * 6.1.1's allocator refuses a line whose handler is set
 * (`is_vect_desc_usable()`, `components/esp_hw_support/intr_alloc.c` line
 * 401, `esp_cpu_intr_has_handler()`), and it sets a handler on every line it
 * hands out (the driver's, or its shared dispatcher), which lineIsFree()
 * refuses in turn. Nothing in ESP-IDF 6.1.1 reserves a line without a handler
 * but `esp_intr_reserve()`, which nothing calls.
 *
 * **One core's tables.** The handler table, the matrix route and the line's
 * enable are per core, and the calls of steps 2 and 4 write the calling
 * core's: the interrupt runs on the core that routed it. The search and the
 * claim run with this core's interrupts masked, so no other task of the core
 * can take the same line between them; and only a task of this core can:
 * `esp_intr_alloc()` claims a line of the core it is called on
 * (`intr_alloc.c` line 676), and `esp_intr_free()` moves to the line's core
 * through the IPC task.
 */
#ifndef KLEIDOS_METER_INTERRUPT_H
#define KLEIDOS_METER_INTERRUPT_H

#include <esp_err.h>

#include <cstdint>

namespace kleidos::meter::interrupt {

/// @addtogroup interrupt_routing
/// @{

/// @brief A CPU line's handler, as the Xtensa port's dispatcher calls it.
using Handler = void ( * )( void* arg );

/// @brief The CPU lines of a core (`SOC_CPU_INTR_NUM`, 32 on the ESP32-S3).
constexpr std::int32_t kLineCount = 32;

/// @brief The one priority routeHere() gives out: level 1, the lowest, whose
///        handlers run in C on the port's common dispatcher.
constexpr std::int32_t kRoutedPriority = 1;

/// @brief Where routeHere() put a source.
struct Route {
    std::int32_t core = -1;  ///< The core whose line takes the source; -1 before a route.
    std::int32_t line = -1;  ///< The CPU line, 0 to kLineCount - 1; -1 before a route.
};

/// @brief What decides whether a CPU line can take a routed source: its entry
///        in the CPU's descriptor table and the port's handler table.
struct LineFacts {
    std::int32_t priority  = 0;  ///< The line's fixed priority (`esp_cpu_intr_desc_t::priority`).
    bool         levelType = false;  ///< Level-triggered (`ESP_CPU_INTR_TYPE_LEVEL`).
    bool         reserved  = false;  ///< `ESP_CPU_INTR_DESC_FLAG_RESVD`: kept for the system.
    bool         special =
        false;  ///< `ESP_CPU_INTR_DESC_FLAG_SPECIAL`: a CPU timer, software or profiling line.
    bool hasHandler = false;  ///< A handler is set on it, by the allocator or a route.
};

/// @brief Whether a line with @p facts can take a routed source.
///
/// It must be priority kRoutedPriority and level-triggered (a peripheral's
/// status bit holds the line until its handler clears it), neither reserved
/// nor special, and have no handler: the same conditions ESP-IDF's
/// allocator applies to a non-shared level-1 request
/// (`is_vect_desc_usable()`).
/// @param[in] facts The line's descriptor and handler state.
/// @retval true  The line is free for a route.
/// @retval false It is taken, kept, or of the wrong kind.
bool lineIsFree( const LineFacts& facts ) noexcept;

/// @brief Routes peripheral interrupt @p source to a free level-1 line of the
///        calling core, with @p handler on it, and enables the line.
///
/// Allocates nothing: every table written is the CPU's or the port's own
/// static one. Called once per source, at boot; a route is never taken down.
/// @param[in]  source  The interrupt matrix's source (`periph_interrupt_t`,
///                     `soc/interrupts.h`), such as `ETS_GPIO_INTR_SOURCE`.
/// @param[in]  handler What the line runs; it must clear the source's
///                     status before it returns, because the line is
///                     level-triggered. In IRAM.
/// @param[in]  arg     Passed to @p handler.
/// @param[out] route   The core and the line, on success; unchanged
///                     otherwise.
/// @retval ESP_OK              Routed and enabled.
/// @retval ESP_ERR_INVALID_ARG @p handler is null; nothing was written.
/// @retval ESP_ERR_NOT_FOUND   No line of this core is free; nothing was
///                             written.
esp_err_t routeHere( std::uint32_t source, Handler handler, void* arg, Route& route ) noexcept;

/// @}

}  // namespace kleidos::meter::interrupt

#endif  // KLEIDOS_METER_INTERRUPT_H
