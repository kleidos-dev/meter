// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file relay.cpp
 * @ingroup relay_bank
 * @brief The relay inputs: released at boot, driven on order, read back.
 */
#include "relay.h"

#include "bits.h"
#include "pin_table.h"
#include "pinmap.h"
#include "text.h"

#include <esp_attr.h>
#include <esp_err.h>
#include <esp_system.h>

#include <string_view>

#include <sdkconfig.h>

// GPIO15, the default map's Relay 2, and GPIO16, any role's, are GPIOs only
// because the RTC slow clock runs on the internal RC: an external 32 kHz
// crystal or oscillator takes them as XTAL_32K_P / XTAL_32K_N, and would drive
// the bypass relay's input with a clock. sdkconfig.defaults states the source;
// this stops a build that selects another one.
#if defined( CONFIG_RTC_CLK_SRC_EXT_CRYS ) || defined( CONFIG_RTC_CLK_SRC_EXT_OSC )
    #error "GPIO15/16 are the map's: the RTC slow clock must stay on the internal RC"
#endif

namespace kleidos::meter::relay {

namespace {

/// @brief The level a channel driven @p drive is given for a commanded state.
/// @param[in] drive    Which input level energizes it.
/// @param[in] energize True to energize its coil, false to release it.
/// @return The GPIO level, 0 or 1.
constexpr std::uint32_t levelFor( Drive drive, bool energize ) noexcept {
    return energize ? energizeLevel( drive ) : releaseLevel( drive );
}

/// @brief Where @p map puts a relay.
/// @param[in] map   A map pinmap::validate() accepts.
/// @param[in] role  The relay's role.
/// @param[in] drive The level that energizes it, the map's.
/// @return Its wiring.
Wiring wiringOf( const pinmap::PinMap& map, pin_table::Role role, Drive drive ) noexcept {
    return { static_cast<gpio_num_t>( map.pins[pinmap::roleIndex( role )] ), drive };
}

/// @brief Parks one GPIO: an input with one internal pull, no interrupt and
/// no peripheral driving it. A pin that will not park stays as it was.
/// @param[in] pin  The pin's set (pinmap::pinBit()).
/// @param[in] high The pull-up in the pull-down's place.
void parkPin( std::uint64_t pin, bool high ) noexcept {
    gpio_config_t config = {};
    config.pin_bit_mask  = pin;
    config.mode          = GPIO_MODE_INPUT;
    config.pull_up_en    = high ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE;
    config.pull_down_en  = high ? GPIO_PULLDOWN_DISABLE : GPIO_PULLDOWN_ENABLE;
    config.intr_type     = GPIO_INTR_DISABLE;
    (void)gpio_config( &config );
}

/// @brief Writes @p state, whole, into the ResetRecord.
/// @param[in] state The coils energized from now on.
void record( Mask state ) noexcept {
    ResetRecord& kept = detail::resetRecord();
    kept.magic        = kResetRecordMagic;
    kept.energized    = state;
    kept.inverse      = ~state;
}

/// @brief The word for a channel's circuit in @p state.
/// @param[in] state   The energized coils.
/// @param[in] channel The channel's index in kChannels.
/// @return Its energized or released word, or `unwired`.
std::string_view wordFor( Mask state, std::size_t channel ) noexcept {
    const ChannelSpec& spec = kChannels[channel];
    if ( !spec.wired ) {
        return "unwired";
    }
    return isEnergized( state, channel ) ? spec.energizedWord : spec.releasedWord;
}

}  // namespace

ResetRecord& detail::resetRecord() noexcept {
    // RTC slow memory, not zeroed at boot: an initializer would be dropped
    // without a word, so there is none, and recordWhole() judges what is there.
    static RTC_NOINIT_ATTR ResetRecord kept;
    return kept;
}

const char* evidenceName( Evidence evidence ) noexcept {
    switch ( evidence ) {
        case Evidence::Seen:
            return "ok";
        case Evidence::Unverified:
            return "unverified";
        case Evidence::Skipped:
            return "skipped";
        case Evidence::None:
            break;
    }
    return "-";
}

const char* pathName( Path path ) noexcept {
    switch ( path ) {
        case Path::Shunt:
            return "shunt";
        case Path::Bypass:
            return "bypass";
        case Path::Both:
            return "both";
        case Path::None:
            break;
    }
    return "none";
}

void Bank::park( std::uint64_t pins ) noexcept {
    for ( std::int32_t gpio = 0; gpio < pin_table::kGpioCount; ++gpio ) {
        const std::uint64_t pin = pinmap::pinBit( gpio );
        if ( ( pins & pin ) != 0U ) {
            parkPin( pin, false );
        }
    }
}

void Bank::connect( const pinmap::PinMap& map ) noexcept {
    wiring_[kLead]   = wiringOf( map, pin_table::Role::Relay1, map.relay1Drive );
    wiring_[kBypass] = wiringOf( map, pin_table::Role::Relay2, map.relay2Drive );

    bool configured = true;
    for ( std::size_t channel = 0; channel < kRelayCount; ++channel ) {
        if ( !kChannels[channel].wired ) {
            continue;  // no GPIO of the map's: nothing is driven for it
        }
        const Wiring& wired = wiring_[channel];
        // The output latch first. `gpio_set_level()` writes the GPIO output
        // register whether or not the pad is an output yet, and
        // `gpio_output_enable()` inside `gpio_config()` only routes the plain
        // GPIO signal to the pad and enables it -- so the pad's first driven
        // level is the release level written here (high for an active-low
        // input such as the HW-316's, low for an active-high one such as the
        // Relay 2 Click's LED), never the register's reset value.
        (void)gpio_set_level( wired.pin, releaseLevel( wired.drive ) );

        // Input as well as output, so that the pad's real level can be read
        // back (padMismatches()). Push-pull, no pulls: a driven pin is never
        // floating, and the module's own input network is what it drives.
        gpio_config_t config = {};
        config.pin_bit_mask  = pinmap::pinBit( static_cast<std::int32_t>( wired.pin ) );
        config.mode          = GPIO_MODE_INPUT_OUTPUT;
        config.pull_up_en    = GPIO_PULLUP_DISABLE;
        config.pull_down_en  = GPIO_PULLDOWN_DISABLE;
        config.intr_type     = GPIO_INTR_DISABLE;
        if ( gpio_config( &config ) != ESP_OK ) {
            configured = false;
            continue;
        }
        (void)gpio_set_level( wired.pin, releaseLevel( wired.drive ) );
    }
    ready_ = configured;
}

bool Bank::begin( const pinmap::PinMap& map ) noexcept {
    connect( map );
    energized_ = kAllReleased;

    // Read only now, with every pin released: nothing here delays that. What
    // a power-on leaves in RTC memory is noise whatever it reads.
    const ResetRecord& kept = detail::resetRecord();
    releasedByReset_        = ( esp_reset_reason() != ESP_RST_POWERON && recordWhole( kept ) )
                                  ? kept.energized
                                  : kAllReleased;
    record( kAllReleased );

    // Every relay is released: now the pins no role of the map holds are given
    // a defined level.
    park( pinmap::parkedSet( map ) );
    return ready_;
}

void Bank::move( const pinmap::PinMap& map ) noexcept {
    const std::array<Wiring, kRelayCount> left = wiring_;
    connect( map );
    for ( const std::size_t channel : { kLead, kBypass } ) {
        const auto gpio = static_cast<std::int32_t>( left[channel].pin );
        if ( !pinmap::held( map, gpio ) ) {
            parkPin( pinmap::pinBit( gpio ), releaseLevel( left[channel].drive ) != 0U );
        }
    }
}

void Bank::drive( std::size_t channel, bool energize ) noexcept {
    if ( !ready_ || channel >= kRelayCount || !kChannels[channel].wired ) {
        return;
    }
    const Wiring& wired = wiring_[channel];
    (void)gpio_set_level( wired.pin, levelFor( wired.drive, energize ) );
    energized_ = energize ? ( energized_ | bitOf( channel ) ) : ( energized_ & ~bitOf( channel ) );
    record( energized_ );
}

Mask Bank::padMismatches() const noexcept {
    Mask mismatched = 0;
    for ( std::size_t channel = 0; channel < kRelayCount; ++channel ) {
        if ( !kChannels[channel].wired ) {
            continue;
        }
        const Wiring&       wired    = wiring_[channel];
        const std::uint32_t expected = levelFor( wired.drive, isEnergized( energized_, channel ) );
        const std::int32_t  level    = gpio_get_level( wired.pin );
        if ( static_cast<std::uint32_t>( level ) != expected ) {
            mismatched |= bitOf( channel );
        }
    }
    return mismatched;
}

void writeState( text::Writer& w, Mask state, Evidence evidence ) noexcept {
    for ( std::size_t channel = 0; channel < kRelayCount; ++channel ) {
        const ChannelSpec& spec = kChannels[channel];
        w.key( spec.name );
        w.text( spec.roleName );
        w.character( ':' );
        w.text( wordFor( state, channel ) );
    }
    w.field( "path", pathName( pathOf( state ) ) );
    w.field( "check", evidenceName( evidence ) );
}

void writeChannels( text::Writer& w, Mask channels ) noexcept {
    std::size_t named = 0;
    for ( std::size_t channel = 0; channel < kRelayCount; ++channel ) {
        if ( isEnergized( channels, channel ) ) {
            w.text( ( named == 0U ) ? "" : "," );
            w.text( kChannels[channel].name );
            ++named;
        }
    }
    if ( named == 0U ) {
        w.character( '-' );
    }
}

}  // namespace kleidos::meter::relay
