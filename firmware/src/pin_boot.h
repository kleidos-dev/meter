// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file pin_boot.h
 * @ingroup pin_map
 * @brief The boot's use of the pin map: the map it runs, where it came from,
 *        and the INA228 brought up on it, with the default bus to fall back
 *        to.
 *
 * load() reads the stored map (pinmap_store.h) before app_main() drives
 * anything; the relays are then released where that map puts them
 * (relay::Bank::begin()), and bringUp() opens the bus the map names, on the
 * sampler's core.
 *
 * **The fallback** (docs/architecture.md SS 9). A bus is the one part of a
 * map a boot can prove: the INA228 acknowledges on it or it does not. When a
 * stored map's bus gets no answer, the default bus (GPIO4 and GPIO5, the
 * address scanned, 400 kHz) is taken in its place if the map leaves those
 * pins free, the pads the stored bus had are parked, and the console queues
 * one error at its announcement; the stored record is not touched, so the
 * person sees what was asked for. When another role of the map holds one of
 * those pins nothing falls back: the instrument has no INA228 and the
 * console, on GPIO43 and GPIO44, which no map may take, is the way back.
 * ALERT and the relays never fall back by themselves: nothing can tell a
 * wrong pin from an absent part. A part that does answer and is not an
 * INA228, or not a sound one, is not a wrong bus, and is reported as it is.
 */
#ifndef KLEIDOS_METER_PIN_BOOT_H
#define KLEIDOS_METER_PIN_BOOT_H

#include "ina228.h"
#include "pinmap.h"
#include "relay.h"

#include <cstdint>
#include <string_view>

namespace kleidos::meter::pin_boot {

/// @addtogroup pin_map
/// @{

/// Where the map the instrument runs came from.
enum class Source : std::uint8_t {
    Default,   ///< The defaults: no stored record, one this image refuses, or the defaults stored.
    Stored,    ///< The stored record.
    Fallback,  ///< The stored record with the default bus in its own bus's place.
};

/// What the boot found on the stored map's bus.
enum class Bus : std::uint8_t {
    Sound,     ///< Nothing to report: it answered, or it is the default bus.
    FellBack,  ///< It did not answer, and the default bus took its place.
    Held,      ///< It did not answer, and a role of the map holds the default bus's pins.
};

/// The map the instrument runs.
struct Live {
    pinmap::PinMap map    = pinmap::kDefaultMap;  ///< The map.
    pinmap::PinMap stored = pinmap::kDefaultMap;  ///< The map the boot loaded, or the last applied.
    Source         source = Source::Default;      ///< Where @c map came from.
    Bus            bus    = Bus::Sound;           ///< What the boot found on @c stored's bus.
};

/// @brief @p source's word, `SYSTem:PINS?`'s `src` and the reports' `pins_src`.
/// @param[in] source  The source.
/// @return `default`, `stored` or `fallback`.
[[nodiscard]] std::string_view sourceWord( Source source ) noexcept;

/// @brief @p map as the one the instrument runs, its bus sound: what a boot
/// that loads it, or the apply that kept it, runs.
/// @param[in] map  A map pinmap::validate() accepts.
/// @return The map, Source::Default for the default map and Source::Stored for
///         any other, and Bus::Sound.
[[nodiscard]] Live liveOf( const pinmap::PinMap& map ) noexcept;

/// @brief Whether a bring-up failed because nothing answered.
/// @param[in] result  The bring-up's verdict.
/// @return true for no acknowledge at any address asked for, and for a bus
///         whose transactions fail.
[[nodiscard]] bool busSilent( BringUpResult result ) noexcept;

/// @brief Opens @p map's bus and brings the INA228 up on it.
///
/// Run on the core that is to own the bus (ina228.h).
/// @param[in,out] device  The INA228, its bus not open.
/// @param[in]     map     A map pinmap::validate() accepts.
/// @return The bring-up's verdict; BringUpResult::BusError when the bus could
///         not be opened.
[[nodiscard]] BringUpResult bringUpOn( Ina228Driver& device, const pinmap::PinMap& map ) noexcept;

/// @brief Reads the map the boot runs: the stored one, or the defaults.
///
/// Reads the flash alone, reaches no pin and allocates nothing. A stored map
/// that is the default one is the default one.
/// @return The map, its source and Bus::Sound.
[[nodiscard]] Live load() noexcept;

/// @brief Opens the bus @p pins names and brings the INA228 up on it, falling
/// back to the default bus as the file's comment says.
///
/// Run on the core that is to own the bus (ina228.h).
/// @param[in,out] device  The INA228, its bus not open.
/// @param[in,out] pins    The map load() answered; on a fallback its map,
///                        source and finding are changed.
/// @return The bring-up's verdict on the bus @p pins ends with;
///         BringUpResult::BusError when that bus could not be opened.
[[nodiscard]] BringUpResult bringUp( Ina228Driver& device, Live& pins ) noexcept;

/// @}

}  // namespace kleidos::meter::pin_boot

#endif  // KLEIDOS_METER_PIN_BOOT_H
