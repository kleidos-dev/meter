// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file pin_apply.cpp
 * @ingroup pin_map
 * @brief The move from one pin map to another: relays, ALERT, bus.
 */
#include "pin_apply.h"

#include "pin_boot.h"
#include "pin_table.h"

#include <driver/gpio.h>
#include <esp_err.h>

namespace kleidos::meter::pin_apply {

namespace {

/// What the half of a move the bus's core makes takes and gives.
struct OnBusCore {
    Ina228Driver*         device  = nullptr;                  ///< The INA228.
    Sampler*              sampler = nullptr;                  ///< The sampling engine.
    const pinmap::PinMap* from    = nullptr;                  ///< The map it is on.
    const pinmap::PinMap* to      = nullptr;                  ///< The map to move to.
    BringUpResult         result  = BringUpResult::BusError;  ///< The bring-up's verdict.
};

/// @brief ALERT, the pins nobody holds any longer, and the bus.
/// @param[in,out] context The OnBusCore to run and answer.
void onBusCore( void* context ) noexcept {
    auto&                 move = *static_cast<OnBusCore*>( context );
    const pinmap::PinMap& to   = *move.to;
    move.sampler->moveAlert(
        static_cast<gpio_num_t>( to.pins[pinmap::roleIndex( pin_table::Role::Alert )] ),
        to.pullAlert == pinmap::Pull::Up );
    move.device->closeBus();
    // The relays' own pins are parked toward their released level already
    // (relay::Bank::move()).
    const pinmap::PinSet freed = pinmap::parkedSet( to ) & ~pinmap::parkedSet( *move.from );
    relay::Bank::park( freed & ~pinmap::relayPins( *move.from ) );
    move.result = pin_boot::bringUpOn( *move.device, to );
}

}  // namespace

Moved moveTo( const Parts& parts, const pinmap::PinMap& from, const pinmap::PinMap& to ) noexcept {
    parts.relays.move( to );
    OnBusCore move;
    move.device  = &parts.device;
    move.sampler = &parts.sampler;
    move.from    = &from;
    move.to      = &to;

    Moved moved;
    moved.made = parts.device.runOnBusCore( &onBusCore, &move ) == ESP_OK;
    moved.bus  = move.result;
    return moved;
}

}  // namespace kleidos::meter::pin_apply
