// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file pinmap.h
 * @ingroup pin_map
 * @brief The pin map: which GPIO each role takes, how each is driven and
 *        pulled, the INA228's address and the boot clock, and the one
 *        definition of the rules a map must keep.
 *
 * A map gives each of the five roles (pin_table::Role: the INA228's SDA, SCL
 * and ALERT, Relay 1, Relay 2) one GPIO, each relay its active level, each
 * bus and alert pin its pull, the INA228's address (or a scan, `auto`) and
 * the bus clock at boot. kDefaultMap is the rig as wired (rev D), and the
 * static_asserts below and in `sampler.cpp` hold it, and the board's table,
 * to what the image can drive. The boot runs the map the store holds, or
 * kDefaultMap (pin_boot.h, FW-04).
 *
 * **The rules exist once, as code: validate().** It reads the board's table
 * (`pin_table.h`, generated from `firmware/board/pins.toml`) and refuses, at
 * the first problem it meets, in this order, role by role: a role with no pin
 * (`missing`), a number that is no GPIO of the chip (`no_pin`), a pin the
 * chip, the board or the firmware holds (`reserved`: strapping, flash and
 * PSRAM, USB, UART0, the LED lines), a pin at or above the role's limit
 * (`too_high`: ALERT must be below GPIO32, the one status register the
 * handler reads), a pin an earlier role already holds (`shared`, named at the
 * later role); then an address that is neither `auto` nor 0x40 to 0x4F
 * (`address`) and a clock outside the I2C master's range (`clock`). The
 * simulator's port and the panel's are held to it by
 * `firmware/test/native/golden/pinmap.vectors`, which the host tests print
 * from this function. No role may take GPIO43 or GPIO44, so the console, and
 * with it every way back to the defaults, never depends on a map.
 *
 * parkedSet() is what the relay bank parks for a map: every pin a role may
 * take that no role holds. For the default map it is relay.h's kParkedPins,
 * pin for pin (a static_assert below).
 *
 * Everything here is `constexpr` and calls no hardware; it allocates nothing.
 */
#ifndef KLEIDOS_METER_PINMAP_H
#define KLEIDOS_METER_PINMAP_H

#include "i2c_bus.h"
#include "ina228_regs.h"
#include "pin_table.h"
#include "relay.h"
#include "status_led.h"

#include <driver/gpio.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string_view>

namespace kleidos::meter::pinmap {

/// @addtogroup pin_map
/// @{

/// Whether a bus or alert pin has the chip's internal pull-up.
enum class Pull : std::uint8_t {
    Off,  ///< No internal pull: the breakout's own pull-up alone.
    Up,   ///< The internal pull-up as well, as every image so far sets it.
};

/// The address that asks for a scan of the INA228's strap space at boot.
constexpr std::uint8_t kAddressAuto = 0U;

/// @brief A pin map: what the instrument's five roles are wired to.
struct PinMap {
    /// Each role's GPIO, in pin_table::Role's order; pin_table::kNoGpio when
    /// the role has none (which validate() refuses).
    std::array<std::int32_t, pin_table::kRoleCount> pins{};
    Pull                                            pullSda   = Pull::Up;  ///< SDA's pull.
    Pull                                            pullScl   = Pull::Up;  ///< SCL's pull.
    Pull                                            pullAlert = Pull::Up;  ///< ALERT's pull.
    relay::Drive  relay1Drive = relay::Drive::ActiveLow;  ///< The level that energizes Relay 1.
    relay::Drive  relay2Drive = relay::Drive::ActiveLow;  ///< The level that energizes Relay 2.
    std::uint8_t  address     = kAddressAuto;  ///< The INA228's address, or kAddressAuto.
    std::uint32_t sclHz       = 0;             ///< The bus clock at boot, in Hz.

    /// @brief Whether two maps are the same, field by field.
    ///
    /// Written out rather than defaulted: the address byte is compared as a
    /// `std::uint32_t`, so no narrow operand is promoted to `int` (MISRA
    /// C++:2023 7.0.5, as addressAllowed() does).
    /// @param[in] left   One map.
    /// @param[in] right  The other.
    /// @return true when every field is equal.
    friend constexpr bool operator==( const PinMap& left, const PinMap& right ) noexcept {
        return left.pins == right.pins && left.pullSda == right.pullSda
               && left.pullScl == right.pullScl && left.pullAlert == right.pullAlert
               && left.relay1Drive == right.relay1Drive && left.relay2Drive == right.relay2Drive
               && std::uint32_t{ left.address } == std::uint32_t{ right.address }
               && left.sclHz == right.sclHz;
    }
};

/// @brief The rig as wired (rev D): the table's defaults.
///
/// SDA on GPIO4, SCL on GPIO5, ALERT on GPIO6, each with the internal
/// pull-up (`i2c_bus.cpp` routes both bus pads with it, `sampler.cpp`
/// configures ALERT with it); the address scanned; 400 kHz at boot; Relay 1,
/// the HW-316's K1 in the DUT's battery lead, on GPIO7, active low; Relay 2,
/// the Relay 2 Click's shunt bypass, on GPIO15, active high.
constexpr PinMap kDefaultMap = {
    pin_table::kDefaultPins,
    pin_table::kDefaultPullSdaUp ? Pull::Up : Pull::Off,
    pin_table::kDefaultPullSclUp ? Pull::Up : Pull::Off,
    pin_table::kDefaultPullAlertUp ? Pull::Up : Pull::Off,
    pin_table::kDefaultRelay1ActiveHigh ? relay::Drive::ActiveHigh : relay::Drive::ActiveLow,
    pin_table::kDefaultRelay2ActiveHigh ? relay::Drive::ActiveHigh : relay::Drive::ActiveLow,
    kAddressAuto,
    pin_table::kDefaultSclHz,
};

/// What validate() can find wrong with a map, in the order it looks.
enum class Problem : std::uint8_t {
    None,      ///< Nothing: the map is valid.
    Missing,   ///< A role has no pin.
    NoPin,     ///< A role's number is no GPIO of the chip.
    Reserved,  ///< A role's pin is one the chip, the board or the firmware holds.
    TooHigh,   ///< A role's pin is at or above the role's limit.
    Shared,    ///< A role's pin is one an earlier role holds.
    Address,   ///< The address is neither kAddressAuto nor in the strap space.
    Clock,     ///< The boot clock is outside the I2C master's range.
};

/// The number of problems, None included.
constexpr std::size_t kProblemCount = 8;

/// Each problem's word, as `SYSTem:PINS:PENDing?` and the vectors spell it.
constexpr std::array<std::string_view, kProblemCount> kProblemWords = { {
    "none",
    "missing",
    "no_pin",
    "reserved",
    "too_high",
    "shared",
    "address",
    "clock",
} };

/// The key a problem of the address is named at.
constexpr std::string_view kAddressKey = "addr";

/// The key a problem of the clock is named at.
constexpr std::string_view kClockKey = "scl_hz";

/// @brief validate()'s finding: the first problem and the key it is at.
struct Verdict {
    Problem          problem = Problem::None;  ///< What is wrong, or Problem::None.
    std::string_view at{};  ///< The role's key, kAddressKey or kClockKey; empty when valid.
};

/// @brief @p role's index in PinMap::pins and pin_table's per-role arrays.
///
/// @param[in] role  The role.
/// @return Its position in pin_table::Role's order.
[[nodiscard]] constexpr std::size_t roleIndex( pin_table::Role role ) noexcept {
    return static_cast<std::size_t>( role );
}

/// @brief @p problem's word.
///
/// @param[in] problem  The problem.
/// @return Its entry of kProblemWords.
[[nodiscard]] constexpr std::string_view problemWord( Problem problem ) noexcept {
    return kProblemWords[static_cast<std::size_t>( problem )];
}

/// @brief The row of @p rows whose GPIO is @p gpio.
///
/// A terminal that is not a GPIO (power, ground, reset) carries
/// pin_table::kNoGpio, which is never looked up as a pin: rowOf() finds
/// nothing for it.
///
/// @param[in] gpio  The GPIO to look up; the table gives each GPIO one row.
/// @param[in] rows  The table.
/// @return The row, or nothing when @p gpio has none or is kNoGpio.
[[nodiscard]] constexpr std::optional<pin_table::Row> rowOf(
    std::int32_t gpio, std::span<const pin_table::Row> rows ) noexcept {
    for ( const pin_table::Row& row : rows ) {
        if ( ( row.gpio == gpio ) && ( gpio != pin_table::kNoGpio ) ) {
            return row;
        }
    }
    return std::nullopt;
}

/// @brief @p map with @p role moved to @p gpio, every other field as it was.
///
/// @param[in] map   The map.
/// @param[in] role  The role to move.
/// @param[in] gpio  Its new GPIO, or pin_table::kNoGpio.
/// @return The changed copy.
[[nodiscard]] constexpr PinMap withPin( PinMap map, pin_table::Role role,
                                        std::int32_t gpio ) noexcept {
    map.pins[roleIndex( role )] = gpio;
    return map;
}

/// @brief @p map with the INA228's bus of @p source, every other field as it
/// was.
///
/// The bus is what a probe of the INA228 runs on: SDA, SCL, their pulls, the
/// address and the boot clock. ALERT and the relays are not part of it.
///
/// @param[in] map     The map to change.
/// @param[in] source  The map whose bus it takes.
/// @return The changed copy.
[[nodiscard]] constexpr PinMap withBusOf( PinMap map, const PinMap& source ) noexcept {
    for ( const pin_table::Role role : { pin_table::Role::Sda, pin_table::Role::Scl } ) {
        map.pins[roleIndex( role )] = source.pins[roleIndex( role )];
    }
    map.pullSda = source.pullSda;
    map.pullScl = source.pullScl;
    map.address = source.address;
    map.sclHz   = source.sclHz;
    return map;
}

/// @brief @p map with the relays of @p source, every other field as it was.
///
/// @param[in] map     The map to change.
/// @param[in] source  The map whose relays, pins and active levels, it takes.
/// @return The changed copy.
[[nodiscard]] constexpr PinMap withRelaysOf( PinMap map, const PinMap& source ) noexcept {
    for ( const pin_table::Role role : { pin_table::Role::Relay1, pin_table::Role::Relay2 } ) {
        map.pins[roleIndex( role )] = source.pins[roleIndex( role )];
    }
    map.relay1Drive = source.relay1Drive;
    map.relay2Drive = source.relay2Drive;
    return map;
}

/// @brief Whether @p map's bus is @p other's.
///
/// @param[in] map    One map.
/// @param[in] other  The other.
/// @return true when withBusOf() would change nothing.
[[nodiscard]] constexpr bool sameBus( const PinMap& map, const PinMap& other ) noexcept {
    return withBusOf( map, other ) == map;
}

/// @brief Whether @p map's relays are @p other's.
///
/// @param[in] map    One map.
/// @param[in] other  The other.
/// @return true when withRelaysOf() would change nothing.
[[nodiscard]] constexpr bool sameRelays( const PinMap& map, const PinMap& other ) noexcept {
    return withRelaysOf( map, other ) == map;
}

/// @brief Whether @p address is one a map may hold.
///
/// The address byte is compared as a `std::uint32_t` (the parameter's type,
/// so the conversion happens at the call, unsigned to unsigned), so no narrow
/// operand is promoted to `int` (MISRA C++:2023 7.0.5, as `bits.h` does).
///
/// @param[in] address  The INA228's 7-bit address, or kAddressAuto.
/// @return true for kAddressAuto and for pin_table::kAddressFirst to
///         pin_table::kAddressLast.
[[nodiscard]] constexpr bool addressAllowed( std::uint32_t address ) noexcept {
    return ( address == std::uint32_t{ kAddressAuto } )
           || ( ( address >= std::uint32_t{ pin_table::kAddressFirst } )
                && ( address <= std::uint32_t{ pin_table::kAddressLast } ) );
}

/// @brief The rules, on @p rows: the first problem of @p map, or none.
///
/// The file's comment gives the order. A role's checks are its pin's own
/// (missing, no pin, reserved, too high) and then whether an earlier role
/// holds it; the address and the clock come after every role.
///
/// @param[in] map   The map.
/// @param[in] rows  The board's table (the host tests plant others).
/// @return The first problem and the key it is at; Problem::None when the map
///         keeps every rule.
[[nodiscard]] constexpr Verdict validate( const PinMap&                   map,
                                          std::span<const pin_table::Row> rows ) noexcept {
    for ( std::size_t index = 0U; index < pin_table::kRoleCount; ++index ) {
        const std::int32_t     gpio = map.pins[index];
        const std::string_view at   = pin_table::kRoleWords[index];
        if ( gpio == pin_table::kNoGpio ) {
            return { Problem::Missing, at };
        }
        const std::optional<pin_table::Row> row = rowOf( gpio, rows );
        if ( !row.has_value() ) {
            return { Problem::NoPin, at };
        }
        if ( row->pinClass == pin_table::PinClass::Reserved ) {
            return { Problem::Reserved, at };
        }
        if ( gpio >= pin_table::kRoleBelow[index] ) {
            return { Problem::TooHigh, at };
        }
        for ( std::size_t earlier = 0U; earlier < index; ++earlier ) {
            if ( map.pins[earlier] == gpio ) {
                return { Problem::Shared, at };
            }
        }
    }
    if ( !addressAllowed( map.address ) ) {
        return { Problem::Address, kAddressKey };
    }
    if ( ( map.sclHz < pin_table::kClockMinHz ) || ( map.sclHz > pin_table::kClockMaxHz ) ) {
        return { Problem::Clock, kClockKey };
    }
    return {};
}

/// @brief The rules on the board's table: validate( @p map, pin_table::kRows ).
///
/// @param[in] map  The map.
/// @return The first problem and the key it is at, or Problem::None.
[[nodiscard]] constexpr Verdict validate( const PinMap& map ) noexcept {
    return validate( map, pin_table::kRows );
}

/// A set of GPIOs, one bit each: GPIO @e n is bit @e n.
using PinSet = std::uint64_t;

/// @brief The set that holds @p gpio alone.
///
/// @param[in] gpio  A GPIO, 0 to 63.
/// @return Its bit.
[[nodiscard]] constexpr PinSet pinBit( std::int32_t gpio ) noexcept {
    return PinSet{ 1U } << static_cast<std::uint32_t>( gpio );
}

/// @brief The set of @p pins.
///
/// @param[in] pins  GPIOs, as ESP-IDF names them.
/// @return Their bits.
[[nodiscard]] constexpr PinSet setOf( std::span<const gpio_num_t> pins ) noexcept {
    PinSet set = 0U;
    for ( const gpio_num_t pin : pins ) {
        set |= pinBit( static_cast<std::int32_t>( pin ) );
    }
    return set;
}

/// @brief Whether any role of @p map holds @p gpio.
///
/// @param[in] map   The map.
/// @param[in] gpio  The GPIO.
/// @return true when one of @p map's pins is @p gpio.
[[nodiscard]] constexpr bool held( const PinMap& map, std::int32_t gpio ) noexcept {
    for ( const std::int32_t pin : map.pins ) {
        if ( pin == gpio ) {
            return true;
        }
    }
    return false;
}

/// @brief The set of the pins @p map gives its two relays.
///
/// @param[in] map  A map validate() accepts.
/// @return Relay 1's and Relay 2's pins.
[[nodiscard]] constexpr PinSet relayPins( const PinMap& map ) noexcept {
    return pinBit( map.pins[roleIndex( pin_table::Role::Relay1 )] )
           | pinBit( map.pins[roleIndex( pin_table::Role::Relay2 )] );
}

/// @brief What the relay bank parks for @p map, on @p rows: every pin a role
/// may take (free or caution) that no role of @p map holds.
///
/// @param[in] map   The map.
/// @param[in] rows  The board's table; a row that is not reserved has a GPIO
///                  (`scripts/gen_pin_table.py` refuses a table otherwise).
/// @return The parked pins.
[[nodiscard]] constexpr PinSet parkedSet( const PinMap&                   map,
                                          std::span<const pin_table::Row> rows ) noexcept {
    PinSet set = 0U;
    for ( const pin_table::Row& row : rows ) {
        if ( ( row.pinClass != pin_table::PinClass::Reserved ) && !held( map, row.gpio ) ) {
            set |= pinBit( row.gpio );
        }
    }
    return set;
}

/// @brief What the relay bank parks for @p map on the board's table.
///
/// @param[in] map  The map.
/// @return parkedSet( @p map, pin_table::kRows ).
[[nodiscard]] constexpr PinSet parkedSet( const PinMap& map ) noexcept {
    return parkedSet( map, pin_table::kRows );
}

/// @brief Whether every role is refused @p gpio, each for @p problem: the
/// default map with that one role moved to it, role by role.
///
/// @param[in] gpio     The GPIO.
/// @param[in] problem  The problem each refusal must be.
/// @return true when validate() refuses @p gpio for every role, each time
///         with @p problem.
[[nodiscard]] constexpr bool refusedForEveryRole( std::int32_t gpio, Problem problem ) noexcept {
    for ( std::size_t index = 0U; index < pin_table::kRoleCount; ++index ) {
        const PinMap map = withPin( kDefaultMap, static_cast<pin_table::Role>( index ), gpio );
        if ( validate( map ).problem != problem ) {
            return false;
        }
    }
    return true;
}

/// @brief Whether every pin of @p pins is refusedForEveryRole() for @p problem.
///
/// @param[in] pins     The GPIOs, as numbers.
/// @param[in] problem  The problem each refusal must be.
/// @return true when each of @p pins is refused for every role with @p problem.
[[nodiscard]] constexpr bool everyRefused( std::span<const std::int32_t> pins,
                                           Problem                       problem ) noexcept {
    for ( const std::int32_t gpio : pins ) {
        if ( !refusedForEveryRole( gpio, problem ) ) {
            return false;
        }
    }
    return true;
}

/// @brief Whether every row of @p rows that has a GPIO has one below @p limit.
///
/// @param[in] rows   The table.
/// @param[in] limit  The first GPIO number not allowed.
/// @return true when no row's GPIO is @p limit or above.
[[nodiscard]] constexpr bool rowsBelow( std::span<const pin_table::Row> rows,
                                        std::int32_t                    limit ) noexcept {
    for ( const pin_table::Row& row : rows ) {
        if ( row.gpio >= limit ) {
            return false;
        }
    }
    return true;
}

// --- What ties the table and the default map to the image ------------------------

static_assert( validate( kDefaultMap ).problem == Problem::None,
               "the default map breaks a rule of the table" );
static_assert( kDefaultMap
                   == PinMap{ { 4, 5, 6, 7, 15 },
                              Pull::Up,
                              Pull::Up,
                              Pull::Up,
                              relay::Drive::ActiveLow,
                              relay::Drive::ActiveHigh,
                              kAddressAuto,
                              400000U },
               "the default map is not the rig as wired (rev D): the INA228 on GPIO4, 5 and 6, "
               "Relay 1 (the DUT's lead) on GPIO7 active low, Relay 2 (the shunt bypass) on "
               "GPIO15 active high, the address scanned, 400 kHz" );
static_assert( everyRefused( relay::kReservedPins, Problem::Reserved ),
               "a pin of relay.h's kReservedPins is not refused, as reserved, for every role" );
static_assert( refusedForEveryRole( static_cast<std::int32_t>( led::kLedDataGpios[0] ),
                                    Problem::Reserved )
                   && refusedForEveryRole( static_cast<std::int32_t>( led::kLedDataGpios[1] ),
                                           Problem::Reserved ),
               "an LED line the firmware holds low is not reserved for every role" );
static_assert( parkedSet( kDefaultMap ) == setOf( relay::kParkedPins ),
               "the default map does not park relay.h's kParkedPins, pin for pin" );
static_assert( pin_table::kGpioCount == static_cast<std::int32_t>( i2c_bus::kGpioCount )
                   && rowsBelow( pin_table::kRows, pin_table::kGpioCount ),
               "a row's GPIO is one the I2C master could not route" );
static_assert( std::uint32_t{ pin_table::kAddressFirst } == std::uint32_t{ ina228::kAddressFirst }
                   && std::uint32_t{ pin_table::kAddressLast }
                          == std::uint32_t{ ina228::kAddressLast },
               "the table's address range is not the INA228's strap space" );
static_assert( pin_table::kClockMinHz == i2c_bus::kMinClockHz
                   && pin_table::kClockMaxHz == i2c_bus::kMaxClockHz,
               "the table's clock range is not the I2C master's" );
static_assert( problemWord( Problem::Clock ) == kProblemWords.back(),
               "the problems and their words are not in one order" );

/// @}

}  // namespace kleidos::meter::pinmap

#endif  // KLEIDOS_METER_PINMAP_H
