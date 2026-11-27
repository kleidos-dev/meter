// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file pin_boot.cpp
 * @ingroup pin_map
 * @brief The boot's pin map: loaded, its bus brought up, the fallback.
 */
#include "pin_boot.h"

#include "pin_table.h"
#include "pinmap_store.h"

#include <driver/gpio.h>
#include <esp_err.h>

#include <array>
#include <cstddef>

namespace kleidos::meter::pin_boot {

namespace {

/// Each Source's word, in the enum's order.
constexpr std::array<std::string_view, 3> kSourceWords = { {
    "default",
    "stored",
    "fallback",
} };

/// @brief The GPIO of a role.
/// @param[in] map  A map pinmap::validate() accepts.
/// @param[in] role The role.
/// @return Its GPIO, as ESP-IDF names it.
gpio_num_t pinOf( const pinmap::PinMap& map, pin_table::Role role ) noexcept {
    return static_cast<gpio_num_t>( map.pins[pinmap::roleIndex( role )] );
}
}  // namespace

std::string_view sourceWord( Source source ) noexcept {
    return kSourceWords[static_cast<std::size_t>( source )];
}

Live liveOf( const pinmap::PinMap& map ) noexcept {
    Live pins;
    if ( map != pinmap::kDefaultMap ) {
        pins.map    = map;
        pins.stored = map;
        pins.source = Source::Stored;
    }
    return pins;
}

bool busSilent( BringUpResult result ) noexcept {
    return result == BringUpResult::NoDevice || result == BringUpResult::BusError;
}

BringUpResult bringUpOn( Ina228Driver& device, const pinmap::PinMap& map ) noexcept {
    const i2c_bus::Pulls pulls = { map.pullSda == pinmap::Pull::Up,
                                   map.pullScl == pinmap::Pull::Up };
    if ( device.openBus( pinOf( map, pin_table::Role::Sda ), pinOf( map, pin_table::Role::Scl ),
                         map.sclHz, pulls )
         != ESP_OK ) {
        return BringUpResult::BusError;
    }
    return device.bringUp( map.address );
}

Live load() noexcept {
    return liveOf( pinmap_store::load().map );
}

BringUpResult bringUp( Ina228Driver& device, Live& pins ) noexcept {
    const BringUpResult result = bringUpOn( device, pins.map );
    if ( !busSilent( result ) || pinmap::sameBus( pins.map, pinmap::kDefaultMap ) ) {
        return result;
    }
    const pinmap::PinMap fallback = pinmap::withBusOf( pins.map, pinmap::kDefaultMap );
    if ( pinmap::validate( fallback ).problem != pinmap::Problem::None ) {
        pins.bus = Bus::Held;  // one of its pins is another role's
        return result;
    }
    // The stored bus's pads first, off the controller and parked, so that they
    // carry nothing of the bus that takes its place.
    device.closeBus();
    relay::Bank::park( pinmap::parkedSet( fallback ) & ~pinmap::parkedSet( pins.map ) );
    pins.map    = fallback;
    pins.source = Source::Fallback;
    pins.bus    = Bus::FellBack;
    return bringUpOn( device, pins.map );
}

}  // namespace kleidos::meter::pin_boot
