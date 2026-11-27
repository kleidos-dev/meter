// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file rtos_ticks.h
 * @ingroup rtos_ticks
 * @brief Milliseconds to FreeRTOS ticks, and the wait with no deadline, with
 *        no C-style cast at the call site.
 *
 * FreeRTOS spells both as macros, `pdMS_TO_TICKS()` and `portMAX_DELAY`,
 * which expand to C-style casts where they are used (MISRA C++:2023 8.2.2;
 * `docs/misra.md`, "Held"). ticksFromMs() and kWaitForever say the same in
 * C++: the conversion is made in 64 bits and narrowed by one `static_cast`,
 * and the forever wait is the largest tick count, the value FreeRTOS gives
 * `portMAX_DELAY` for a 32-bit tick. `scripts/misra_rules.py` fails a source
 * of `firmware/src` that names either macro.
 */
#ifndef KLEIDOS_METER_RTOS_TICKS_H
#define KLEIDOS_METER_RTOS_TICKS_H

#include "freertos/FreeRTOS.h"

#include <cstdint>
#include <limits>

namespace kleidos::meter::rtos {

/// @addtogroup rtos_ticks
/// @{

/// The tick rate, in ticks per second (`CONFIG_FREERTOS_HZ`, 1000 on the
/// meter: one tick a millisecond).
constexpr std::uint64_t kTickRateHz = configTICK_RATE_HZ;

/// Milliseconds in a second.
constexpr std::uint64_t kMillisecondsPerSecond = 1000U;

/// The wait with no deadline: the largest tick count, which FreeRTOS reads as
/// "block until given" (`portMAX_DELAY` for a 32-bit tick).
constexpr TickType_t kWaitForever = std::numeric_limits<TickType_t>::max();

static_assert( sizeof( TickType_t ) == sizeof( std::uint32_t ),
               "kWaitForever is portMAX_DELAY only for a 32-bit tick" );

/// @brief The ticks of @p milliseconds, truncated as `pdMS_TO_TICKS()`
///        truncates.
///
/// The product is taken in 64 bits, so it cannot wrap where FreeRTOS's macro,
/// which multiplies in `TickType_t`, would; at 1000 Hz it is @p milliseconds
/// itself.
///
/// @param[in] milliseconds The time, in milliseconds.
/// @return The time, in ticks.
constexpr TickType_t ticksFromMs( std::uint32_t milliseconds ) noexcept {
    return static_cast<TickType_t>( ( static_cast<std::uint64_t>( milliseconds ) * kTickRateHz )
                                    / kMillisecondsPerSecond );
}

/// @}

}  // namespace kleidos::meter::rtos

#endif  // KLEIDOS_METER_RTOS_TICKS_H
