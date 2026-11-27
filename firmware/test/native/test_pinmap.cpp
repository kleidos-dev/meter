// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_pinmap.cpp
 * @brief Host tests of the pin map's rules (src/pinmap.h) over the board's
 *        table (src/pin_table.h, generated from firmware/board/pins.toml),
 *        and the vectors that hold every other copy of the rules to them.
 *
 * The rules exist once as code, pinmap::validate(); the simulator's port and
 * the panel's are held to it by golden/pinmap.vectors, which this file
 * prints: every role on every GPIO, every pair of roles on one GPIO, every
 * address byte, the clock's edges and 2,000 seeded maps, each with the
 * verdict validate() gives it. A change of the table or of a rule shows in
 * that file's diff:
 *
 *     make -C firmware/test/native vectors   # writes golden/pinmap.vectors
 *     git diff firmware/test/native/golden/pinmap.vectors
 *
 * Every case below calls the rules at run time, as the vectors do, so the
 * coverage report sees each decision taken both ways; the build's
 * static_asserts (pinmap.h, sampler.cpp) hold the same rules on the
 * real table at compile time.
 */
#include "pinmap.h"
#include "relay.h"
#include "status_led.h"
#include "unit.h"

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iterator>
#include <optional>
#include <span>
#include <sstream>
#include <string>
#include <string_view>
#include <utility>

#ifndef METER_PINMAP_VECTORS
    #error "the Makefile passes the vectors' path as METER_PINMAP_VECTORS"
#endif

using namespace kleidos::meter;
using pin_table::PinClass;
using pin_table::Role;
using pin_table::Row;
using pinmap::PinMap;
using pinmap::Problem;
using pinmap::Pull;

namespace {

/// Every role, in Role's order.
constexpr std::array<Role, pin_table::kRoleCount> kRoles = {
    Role::Sda, Role::Scl, Role::Alert, Role::Relay1, Role::Relay2,
};

/// @p map's pin for @p role.
std::int32_t pin( const PinMap& map, Role role ) {
    return map.pins[pinmap::roleIndex( role )];
}

/// The problem validate() finds in @p map on the board's table.
Problem problemOf( const PinMap& map ) {
    return pinmap::validate( map ).problem;
}

/// Whether @p set holds @p gpio.
bool parks( pinmap::PinSet set, std::int32_t gpio ) {
    return ( set & pinmap::pinBit( gpio ) ) != 0U;
}

/// The key validate() names for @p map's problem.
std::string atOf( const PinMap& map ) {
    return std::string( pinmap::validate( map ).at );
}

/// The default map with @p role moved to @p gpio.
PinMap moved( Role role, std::int32_t gpio ) {
    return pinmap::withPin( pinmap::kDefaultMap, role, gpio );
}

}  // namespace

// --- The default map ------------------------------------------------------------

TEST( "pinmap", "the default map is the rig as wired, and it is valid" ) {
    const PinMap& map = pinmap::kDefaultMap;
    CHECK( pin( map, Role::Sda ) == 4 );
    CHECK( pin( map, Role::Scl ) == 5 );
    CHECK( pin( map, Role::Alert ) == 6 );
    CHECK( pin( map, Role::Relay1 ) == 7 );
    CHECK( pin( map, Role::Relay2 ) == 15 );
    CHECK( map.relay1Drive == relay::Drive::ActiveLow );
    CHECK( map.relay2Drive == relay::Drive::ActiveHigh );
    CHECK( map.pullSda == Pull::Up && map.pullScl == Pull::Up && map.pullAlert == Pull::Up );
    CHECK( map.address == pinmap::kAddressAuto );
    CHECK( map.sclHz == 400000U );
    const pinmap::Verdict verdict = pinmap::validate( map );
    CHECK( verdict.problem == Problem::None );
    CHECK_EQ( std::string( verdict.at ), "" );
}

// --- A map's bus and its relays, taken from another ----------------------------------

namespace {

/// A map unlike the default in every field, which the table takes.
PinMap otherMap() {
    PinMap map      = pinmap::kDefaultMap;
    map.pins        = { 8, 9, 10, 11, 12 };
    map.pullSda     = Pull::Off;
    map.pullScl     = Pull::Off;
    map.pullAlert   = Pull::Off;
    map.relay1Drive = relay::Drive::ActiveHigh;
    map.relay2Drive = relay::Drive::ActiveLow;
    map.address     = 0x45;
    map.sclHz       = 100'000;
    return map;
}

}  // namespace

TEST( "pinmap", "two maps are equal when every field is, and differ by any one" ) {
    const PinMap map = otherMap();
    CHECK( map == otherMap() );
    for ( const Role role : kRoles ) {
        CHECK( map != pinmap::withPin( map, role, 13 ) );
    }
    PinMap pull  = map;
    pull.pullSda = Pull::Up;
    CHECK( pull != map );
    pull         = map;
    pull.pullScl = Pull::Up;
    CHECK( pull != map );
    pull           = map;
    pull.pullAlert = Pull::Up;
    CHECK( pull != map );
    PinMap drive      = map;
    drive.relay1Drive = relay::Drive::ActiveLow;
    CHECK( drive != map );
    drive             = map;
    drive.relay2Drive = relay::Drive::ActiveHigh;
    CHECK( drive != map );
    PinMap bus  = map;
    bus.address = 0x46;
    CHECK( bus != map );
    bus       = map;
    bus.sclHz = 100'001;
    CHECK( bus != map );
}

TEST( "pinmap", "a map takes another's bus, or its relays, and nothing else" ) {
    const PinMap other = otherMap();
    const PinMap bus   = pinmap::withBusOf( other, pinmap::kDefaultMap );
    CHECK( bus.pins == ( std::array<std::int32_t, 5>{ 4, 5, 10, 11, 12 } ) );
    CHECK( bus.pullSda == Pull::Up && bus.pullScl == Pull::Up && bus.pullAlert == Pull::Off );
    CHECK( bus.address == pinmap::kAddressAuto && bus.sclHz == 400'000U );
    CHECK( bus.relay1Drive == other.relay1Drive && bus.relay2Drive == other.relay2Drive );
    CHECK( pinmap::sameBus( bus, pinmap::kDefaultMap ) && !pinmap::sameBus( bus, other ) );
    CHECK( pinmap::sameRelays( bus, other ) && !pinmap::sameRelays( bus, pinmap::kDefaultMap ) );
    const PinMap relays = pinmap::withRelaysOf( other, pinmap::kDefaultMap );
    CHECK( relays.pins == ( std::array<std::int32_t, 5>{ 8, 9, 10, 7, 15 } ) );
    CHECK( relays.relay1Drive == relay::Drive::ActiveLow );
    CHECK( relays.relay2Drive == relay::Drive::ActiveHigh );
    CHECK( pinmap::sameBus( relays, other ) && relays.pullAlert == Pull::Off );
    CHECK( pinmap::sameRelays( relays, pinmap::kDefaultMap ) );
    // The same pins energized by the other level are not the same relays.
    PinMap inverted      = pinmap::kDefaultMap;
    inverted.relay2Drive = relay::Drive::ActiveLow;
    CHECK( !pinmap::sameRelays( inverted, pinmap::kDefaultMap ) );
}

// --- Each problem, at its key --------------------------------------------------------

TEST( "pinmap", "a role with no pin is missing" ) {
    for ( const Role role : kRoles ) {
        const PinMap map = moved( role, pin_table::kNoGpio );
        CHECK( problemOf( map ) == Problem::Missing );
        CHECK_EQ( atOf( map ), std::string( pin_table::kRoleWords[pinmap::roleIndex( role )] ) );
    }
}

TEST( "pinmap", "a number that is no GPIO of the chip is no pin" ) {
    for ( const std::int32_t gpio : { 22, 23, 24, 25, 49, 50, 1000, -2, -1000 } ) {
        CHECK( problemOf( moved( Role::Sda, gpio ) ) == Problem::NoPin );
        CHECK( problemOf( moved( Role::Relay2, gpio ) ) == Problem::NoPin );
    }
    CHECK_EQ( atOf( moved( Role::Relay2, 22 ) ), "relay2" );
}

TEST( "pinmap", "a reserved pin is refused for every role, GPIO45 among them" ) {
    for ( const Role role : kRoles ) {
        CHECK( problemOf( moved( role, 45 ) ) == Problem::Reserved );
        CHECK( problemOf( moved( role, 43 ) ) == Problem::Reserved );  // the console
        CHECK( problemOf( moved( role, 48 ) ) == Problem::Reserved );  // the LED
        CHECK( problemOf( moved( role, 26 ) ) == Problem::Reserved );  // off the board
    }
    CHECK_EQ( atOf( moved( Role::Sda, 45 ) ), "sda" );
}

TEST( "pinmap", "ALERT is refused above the status bank, and a caution pin taken elsewhere" ) {
    CHECK( problemOf( moved( Role::Alert, 39 ) ) == Problem::TooHigh );
    CHECK_EQ( atOf( moved( Role::Alert, 39 ) ), "alert" );
    CHECK( problemOf( moved( Role::Alert, 47 ) ) == Problem::TooHigh );
    CHECK( problemOf( moved( Role::Alert, 21 ) ) == Problem::None );  // the highest free below 32
    CHECK( problemOf( moved( Role::Sda, 39 ) ) == Problem::None );    // caution, not refused
    CHECK( problemOf( moved( Role::Relay1, 47 ) ) == Problem::None );
}

TEST( "pinmap", "two roles on one pin: the later role is the one named" ) {
    CHECK( problemOf( moved( Role::Scl, 4 ) ) == Problem::Shared );
    CHECK_EQ( atOf( moved( Role::Scl, 4 ) ), "scl" );
    CHECK( problemOf( moved( Role::Sda, 5 ) ) == Problem::Shared );
    CHECK_EQ( atOf( moved( Role::Sda, 5 ) ), "scl" );  // SDA moved onto SCL: SCL meets it
    CHECK( problemOf( moved( Role::Relay2, 7 ) ) == Problem::Shared );
    CHECK_EQ( atOf( moved( Role::Relay2, 7 ) ), "relay2" );
}

TEST( "pinmap", "the address is auto or the INA228's strap space (MC/DC)" ) {
    PinMap map = pinmap::kDefaultMap;
    for ( const std::uint8_t ok : { std::uint8_t{ 0x00U }, std::uint8_t{ 0x40U },
                                    std::uint8_t{ 0x47U }, std::uint8_t{ 0x4FU } } ) {
        map.address = ok;
        CHECK( problemOf( map ) == Problem::None );
    }
    for ( const std::uint8_t bad : { std::uint8_t{ 0x01U }, std::uint8_t{ 0x3FU },
                                     std::uint8_t{ 0x50U }, std::uint8_t{ 0xFFU } } ) {
        map.address = bad;
        CHECK( problemOf( map ) == Problem::Address );
        CHECK_EQ( atOf( map ), "addr" );
    }
}

TEST( "pinmap", "the clock is inside the I2C master's range (MC/DC)" ) {
    PinMap map = pinmap::kDefaultMap;
    for ( const std::uint32_t ok : { 50000U, 100000U, 1000000U } ) {
        map.sclHz = ok;
        CHECK( problemOf( map ) == Problem::None );
    }
    for ( const std::uint32_t bad : { 0U, 49999U, 1000001U } ) {
        map.sclHz = bad;
        CHECK( problemOf( map ) == Problem::Clock );
        CHECK_EQ( atOf( map ), "scl_hz" );
    }
}

TEST( "pinmap", "the first problem wins: roles in order, then the address, then the clock" ) {
    PinMap map  = moved( Role::Sda, 45 );
    map.pins[1] = pin_table::kNoGpio;  // SCL missing too
    map.address = 0x01U;
    map.sclHz   = 1U;
    CHECK( problemOf( map ) == Problem::Reserved );
    CHECK_EQ( atOf( map ), "sda" );
    map.pins[0] = 4;
    CHECK( problemOf( map ) == Problem::Missing );
    CHECK_EQ( atOf( map ), "scl" );
    map.pins[1] = 5;
    CHECK( problemOf( map ) == Problem::Address );
    map.address = pinmap::kAddressAuto;
    CHECK( problemOf( map ) == Problem::Clock );
}

TEST( "pinmap", "each problem has its word" ) {
    CHECK_EQ( std::string( pinmap::problemWord( Problem::None ) ), "none" );
    CHECK_EQ( std::string( pinmap::problemWord( Problem::Missing ) ), "missing" );
    CHECK_EQ( std::string( pinmap::problemWord( Problem::NoPin ) ), "no_pin" );
    CHECK_EQ( std::string( pinmap::problemWord( Problem::Reserved ) ), "reserved" );
    CHECK_EQ( std::string( pinmap::problemWord( Problem::TooHigh ) ), "too_high" );
    CHECK_EQ( std::string( pinmap::problemWord( Problem::Shared ) ), "shared" );
    CHECK_EQ( std::string( pinmap::problemWord( Problem::Address ) ), "address" );
    CHECK_EQ( std::string( pinmap::problemWord( Problem::Clock ) ), "clock" );
}

// --- The rules on planted tables -----------------------------------------------------

TEST( "pinmap", "a role's limit comes from the table: a planted row above it is too high" ) {
    // A table whose only free rows are the default map's and GPIO60, which
    // no role but one whose limit were above it could take.
    std::array<Row, 6> rows = { {
        { pin_table::Rail::Left, 4, 4, PinClass::Free, pin_table::Group::None },
        { pin_table::Rail::Left, 5, 5, PinClass::Free, pin_table::Group::None },
        { pin_table::Rail::Left, 6, 6, PinClass::Free, pin_table::Group::None },
        { pin_table::Rail::Left, 7, 7, PinClass::Free, pin_table::Group::None },
        { pin_table::Rail::Left, 8, 15, PinClass::Free, pin_table::Group::None },
        { pin_table::Rail::Right, 9, 60, PinClass::Caution, pin_table::Group::Jtag },
    } };
    CHECK( pinmap::validate( pinmap::kDefaultMap, rows ).problem == Problem::None );
    const PinMap high = moved( Role::Sda, 60 );
    CHECK( pinmap::validate( high, rows ).problem == Problem::TooHigh );
    CHECK( pinmap::validate( moved( Role::Sda, 18 ), rows ).problem == Problem::NoPin );
    CHECK( !pinmap::rowsBelow( rows, pin_table::kGpioCount ) );
    CHECK( pinmap::rowsBelow( rows, 61 ) );
    CHECK( pinmap::rowsBelow( pin_table::kRows, pin_table::kGpioCount ) );
}

TEST( "pinmap", "rowOf finds a GPIO's row and nothing for a number with none" ) {
    const std::optional<Row> four = pinmap::rowOf( 4, pin_table::kRows );
    REQUIRE( four.has_value() );
    CHECK( four->rail == pin_table::Rail::Left && four->terminal == 4U );
    const std::optional<Row> memory = pinmap::rowOf( 30, pin_table::kRows );
    REQUIRE( memory.has_value() );
    CHECK( memory->rail == pin_table::Rail::OffBoard && memory->pinClass == PinClass::Reserved );
    CHECK( !pinmap::rowOf( 22, pin_table::kRows ).has_value() );
    CHECK( !pinmap::rowOf( pin_table::kNoGpio, pin_table::kRows ).has_value() );
}

TEST( "pinmap", "refusedForEveryRole and everyRefused: the reserved and the LED pins" ) {
    CHECK( pinmap::refusedForEveryRole( 45, Problem::Reserved ) );
    CHECK( !pinmap::refusedForEveryRole( 45, Problem::Shared ) );    // refused, for another reason
    CHECK( !pinmap::refusedForEveryRole( 18, Problem::Reserved ) );  // free: taken by any role
    CHECK( !pinmap::refusedForEveryRole( 39, Problem::TooHigh ) );   // too high for ALERT alone
    CHECK( pinmap::everyRefused( relay::kReservedPins, Problem::Reserved ) );
    constexpr std::array<std::int32_t, 3> planted = { { 0, 45, 18 } };
    CHECK( !pinmap::everyRefused( planted, Problem::Reserved ) );
    for ( const gpio_num_t line : led::kLedDataGpios ) {
        CHECK(
            pinmap::refusedForEveryRole( static_cast<std::int32_t>( line ), Problem::Reserved ) );
    }
}

// --- The parked set ------------------------------------------------------------

TEST( "pinmap", "the default map parks exactly relay.h's kParkedPins" ) {
    const pinmap::PinSet parked = pinmap::parkedSet( pinmap::kDefaultMap );
    CHECK( parked == pinmap::setOf( relay::kParkedPins ) );
    std::size_t count = 0;
    for ( std::int32_t gpio = 0; gpio < 64; ++gpio ) {
        count += parks( parked, gpio ) ? 1U : 0U;
    }
    CHECK( count == relay::kParkedPins.size() );
    CHECK( !parks( parked, 4 ) && !parks( parked, 38 ) );
    CHECK( parks( parked, 16 ) && parks( parked, 47 ) );
}

TEST( "pinmap", "a pin a role leaves is parked, and the one it takes is not" ) {
    const pinmap::PinSet parked = pinmap::parkedSet( moved( Role::Relay1, 16 ) );
    CHECK( parks( parked, 7 ) );
    CHECK( !parks( parked, 16 ) );
    const pinmap::PinSet none = pinmap::parkedSet( moved( Role::Alert, pin_table::kNoGpio ) );
    CHECK( parks( none, 6 ) );
}

TEST( "pinmap", "withPin moves one role and nothing else" ) {
    const PinMap map = moved( Role::Alert, 18 );
    CHECK( pin( map, Role::Alert ) == 18 );
    CHECK( pin( map, Role::Sda ) == 4 && pin( map, Role::Relay2 ) == 15 );
    CHECK( map.sclHz == pinmap::kDefaultMap.sclHz );
}

// --- The vectors --------------------------------------------------------------------

namespace {

/// The vectors' columns: the wire's keys (docs/scpi.md, `SYSTem:PINS:PENDing?`),
/// then the verdict.
constexpr std::string_view kColumns =
    "sda scl alert addr scl_hz pull_sda pull_scl pull_alert relay1 relay1_active relay2 "
    "relay2_active | valid problem at";

/// The header the file starts with: its holder and license (spelled in
/// pieces, so that REUSE does not take these literals for this file's own
/// tags), what it is, and its columns.
std::string header() {
    std::string out =
        "# SPDX-"
        "FileCopyrightText: 2026 José M. Requena Plens\n"
        "# SPDX-"
        "License-Identifier: GPL-3.0-or-later\n"
        "#\n"
        "# The pin map's vectors: firmware/test/native/test_pinmap.cpp prints them from\n"
        "# pinmap::validate() over firmware/src/pin_table.h. Generated; never edit them by\n"
        "# hand (make -C firmware/test/native vectors). Every port of the rules (the\n"
        "# simulator's, the panel's) must give every line its verdict.\n"
        "#\n";
    char digest[64] = {};
    std::snprintf( digest, sizeof( digest ), "# table 0x%08X\n",
                   static_cast<unsigned>( pin_table::kPinTableDigest ) );
    out += digest;
    out += "# columns: ";
    out += kColumns;
    out += "\n";
    return out;
}

/// @p gpio as a column: its number, or `none`.
std::string gpioWord( std::int32_t gpio ) {
    return gpio == pin_table::kNoGpio ? std::string( "none" ) : std::to_string( gpio );
}

/// @p address as a column: `auto` or two hex digits.
std::string addressWord( std::uint8_t address ) {
    if ( address == pinmap::kAddressAuto ) {
        return "auto";
    }
    char hex[8] = {};
    std::snprintf( hex, sizeof( hex ), "0x%02X", static_cast<unsigned>( address ) );
    return hex;
}

/// One line: @p map's columns and validate()'s verdict on it.
std::string line( const PinMap& map ) {
    const auto pull  = []( Pull p ) { return p == Pull::Up ? "up" : "off"; };
    const auto drive = []( relay::Drive d ) {
        return d == relay::Drive::ActiveHigh ? "high" : "low";
    };
    std::string out = gpioWord( pin( map, Role::Sda ) ) + " " + gpioWord( pin( map, Role::Scl ) )
                      + " " + gpioWord( pin( map, Role::Alert ) ) + " " + addressWord( map.address )
                      + " " + std::to_string( map.sclHz ) + " " + pull( map.pullSda ) + " "
                      + pull( map.pullScl ) + " " + pull( map.pullAlert ) + " "
                      + gpioWord( pin( map, Role::Relay1 ) ) + " " + drive( map.relay1Drive ) + " "
                      + gpioWord( pin( map, Role::Relay2 ) ) + " " + drive( map.relay2Drive );
    const pinmap::Verdict verdict = pinmap::validate( map );
    if ( verdict.problem == Problem::None ) {
        return out + " | 1 - -\n";
    }
    return out + " | 0 " + std::string( pinmap::problemWord( verdict.problem ) ) + " "
           + std::string( verdict.at ) + "\n";
}

/// The seeded maps' generator: xorshift32 (Marsaglia), from a fixed seed, so
/// the file is the same on every host and at 32 bits.
struct Xorshift {
    std::uint32_t state = 0x6B6D6574U;  ///< "kmet"
    std::uint32_t next() {
        state ^= state << 13U;
        state ^= state >> 17U;
        state ^= state << 5U;
        return state;
    }
};

/// The number of seeded maps.
constexpr std::size_t kSeededMaps = 2000;

/// The clocks a seeded map draws from: the range's edges, both sides of
/// them, nothing at all, and the usual rates.
constexpr std::array<std::uint32_t, 10> kClocks = {
    { 50000U, 100000U, 400000U, 1000000U, 49999U, 1000001U, 0U, 400000U, 100000U, 750000U } };

/// One seeded map: on even @p index every role draws any number from -1 to
/// 50; on odd ones the roles take distinct pins a role may hold, so the
/// verdict turns on the limits, the address and the clock.
PinMap seeded( Xorshift& random, std::size_t index ) {
    PinMap map = pinmap::kDefaultMap;
    if ( index % 2U == 0U ) {
        for ( std::int32_t& gpio : map.pins ) {
            gpio = static_cast<std::int32_t>( random.next() % 52U ) - 1;
        }
    } else {
        std::array<std::int32_t, pin_table::kRowCount> open{};
        std::size_t                                    count = 0;
        for ( const Row& row : pin_table::kRows ) {
            if ( row.pinClass != PinClass::Reserved ) {
                open[count++] = row.gpio;
            }
        }
        for ( std::size_t role = 0; role < pin_table::kRoleCount; ++role ) {
            const std::size_t pick = role + random.next() % ( count - role );
            std::swap( open[role], open[pick] );
            map.pins[role] = open[role];
        }
    }
    map.pullSda   = ( random.next() & 1U ) != 0U ? Pull::Up : Pull::Off;
    map.pullScl   = ( random.next() & 1U ) != 0U ? Pull::Up : Pull::Off;
    map.pullAlert = ( random.next() & 1U ) != 0U ? Pull::Up : Pull::Off;
    map.relay1Drive =
        ( random.next() & 1U ) != 0U ? relay::Drive::ActiveHigh : relay::Drive::ActiveLow;
    map.relay2Drive =
        ( random.next() & 1U ) != 0U ? relay::Drive::ActiveHigh : relay::Drive::ActiveLow;
    constexpr std::array<std::uint8_t, 8> kOutside = {
        { 0x00U, 0x00U, 0x00U, 0x00U, 0x3FU, 0x50U, 0x7FU, 0x01U } };
    const std::uint32_t address = random.next() % 24U;
    map.address =
        address < 16U ? static_cast<std::uint8_t>( 0x40U + address ) : kOutside[address - 16U];
    map.sclHz = kClocks[random.next() % kClocks.size()];
    return map;
}

/// Every vector, as the file holds them.
std::string vectors() {
    std::string out = header();
    out += "#\n# every role on every number from -1 (none) to 50\n";
    for ( const Role role : kRoles ) {
        for ( std::int32_t gpio = -1; gpio <= 50; ++gpio ) {
            out += line( moved( role, gpio ) );
        }
    }
    out += "#\n# every pair of roles on one GPIO of the table\n";
    for ( std::size_t first = 0; first < kRoles.size(); ++first ) {
        for ( std::size_t second = first + 1U; second < kRoles.size(); ++second ) {
            for ( const Row& row : pin_table::kRows ) {
                if ( row.gpio != pin_table::kNoGpio ) {
                    out += line( pinmap::withPin( moved( kRoles[first], row.gpio ), kRoles[second],
                                                  row.gpio ) );
                }
            }
        }
    }
    out += "#\n# every address byte\n";
    for ( std::uint32_t address = 0; address <= 0xFFU; ++address ) {
        PinMap map  = pinmap::kDefaultMap;
        map.address = static_cast<std::uint8_t>( address );
        out += line( map );
    }
    out += "#\n# the clock's edges\n";
    for ( const std::uint32_t hz : { 0U, 1U, 49999U, 50000U, 50001U, 100000U, 400000U, 999999U,
                                     1000000U, 1000001U, 4294967295U } ) {
        PinMap map = pinmap::kDefaultMap;
        map.sclHz  = hz;
        out += line( map );
    }
    out += "#\n# 2000 seeded maps (xorshift32 from 0x6B6D6574)\n";
    Xorshift random;
    for ( std::size_t index = 0; index < kSeededMaps; ++index ) {
        out += line( seeded( random, index ) );
    }
    return out;
}

/// The file at @p path, whole; empty when it cannot be read.
std::string readFile( const char* path ) {
    std::ifstream in( path, std::ios::binary );
    return std::string( std::istreambuf_iterator<char>( in ), std::istreambuf_iterator<char>() );
}

/// The first line where @p expected and @p actual differ, or "" when equal.
std::string firstDifference( const std::string& expected, const std::string& actual ) {
    std::istringstream want( expected );
    std::istringstream got( actual );
    std::string        a;
    std::string        b;
    for ( std::size_t number = 1;; ++number ) {
        const bool more = static_cast<bool>( std::getline( want, a ) );
        const bool also = static_cast<bool>( std::getline( got, b ) );
        if ( !more && !also ) {
            return "";
        }
        if ( more != also || a != b ) {
            return "line " + std::to_string( number ) + ": the file has '" + a
                   + "', the rules give '" + b + "'";
        }
    }
}

}  // namespace

TEST( "pinmap-vectors", "every vector is validate()'s verdict" ) {
    const std::string actual = vectors();
    if ( std::getenv( "METER_VECTORS_WRITE" ) != nullptr ) {
        std::ofstream out( METER_PINMAP_VECTORS, std::ios::binary | std::ios::trunc );
        out << actual;
        REQUIRE( static_cast<bool>( out ) );
        std::printf( "  wrote %s (%zu bytes)\n", METER_PINMAP_VECTORS, actual.size() );
        return;
    }
    const std::string golden = readFile( METER_PINMAP_VECTORS );
    REQUIRE( !golden.empty() );
    CHECK_EQ( firstDifference( golden, actual ), "" );
}

TEST( "pinmap-vectors", "the vectors hold valid and refused maps, every problem among them" ) {
    const std::string text  = vectors();
    std::size_t       valid = 0;
    for ( std::size_t at = text.find( " | 1 " ); at != std::string::npos;
          at             = text.find( " | 1 ", at + 1U ) ) {
        ++valid;
    }
    CHECK( valid > 100U );
    for ( const Problem problem :
          { Problem::Missing, Problem::NoPin, Problem::Reserved, Problem::TooHigh, Problem::Shared,
            Problem::Address, Problem::Clock } ) {
        CHECK_CONTAINS( text, " | 0 " + std::string( pinmap::problemWord( problem ) ) + " " );
    }
}
