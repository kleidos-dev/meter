// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file pin_apply.h
 * @ingroup pin_map
 * @brief The instrument moved from the pin map it runs to another one, and
 *        back: the hardware half of `SYSTem:PINS:APPLy` and
 *        `SYSTem:PINS:DEFault` (docs/architecture.md SS 2.7).
 *
 * moveTo() is one move, whichever way it goes: the console calls it to try a
 * map, proves the instrument on it, and calls it again, the maps the other
 * way round, to put the last good one back (console.cpp). The order is the
 * boot's, so that what carries the DUT's current is settled first:
 *
 *  1. the relays, released, on their new pins (relay::Bank::move());
 *  2. on the bus's core, where the ALERT interrupt is routed too: ALERT on its
 *     new pin (Sampler::moveAlert());
 *  3. the bus closed, the pins no role holds any longer parked, so that they
 *     carry nothing of the bus that moves, and the bus opened and the INA228
 *     brought up on its new pins (pin_boot::bringUpOn()).
 *
 * The caller has checked that every coil is released, that no window is open
 * and that both maps keep the rules. Nothing is allocated.
 */
#ifndef KLEIDOS_METER_PIN_APPLY_H
#define KLEIDOS_METER_PIN_APPLY_H

#include "ina228.h"
#include "pinmap.h"
#include "relay.h"
#include "sampler.h"

namespace kleidos::meter::pin_apply {

/// @addtogroup pin_map
/// @{

/// The objects whose pins a move changes.
struct Parts {
    Ina228Driver& device;   ///< The INA228 and its bus.
    Sampler&      sampler;  ///< The ALERT pin's interrupt.
    relay::Bank&  relays;   ///< The relay inputs.
};

/// What a move did.
struct Moved {
    /// The bus's core ran its half. When it refused to, the relays alone moved:
    /// ALERT and the bus are where they were.
    bool          made = false;
    BringUpResult bus  = BringUpResult::BusError;  ///< The INA228's verdict, when @c made.
};

/// @brief Moves the instrument from @p from to @p to, and brings the INA228
/// up on it.
///
/// The relays' verdict is relay::Bank::ready()'s and ALERT's is
/// Sampler::engineReady()'s.
/// @param[in,out] parts  The relays, the sampler and the INA228.
/// @param[in]     from   The map they are on.
/// @param[in]     to     The map to move to.
/// @return Whether the bus's core ran, and the bring-up's verdict.
[[nodiscard]] Moved moveTo( const Parts& parts, const pinmap::PinMap& from,
                            const pinmap::PinMap& to ) noexcept;

/// @}

}  // namespace kleidos::meter::pin_apply

#endif  // KLEIDOS_METER_PIN_APPLY_H
