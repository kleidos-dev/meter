// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_pin_boot.cpp
 * @brief The boot from the pin map (pin_boot.cpp, relay.cpp, and the reports
 *        that name the live map): no record, a stored map on the board wired
 *        to it, a stored bus the INA228 is not on, and one whose default pins
 *        another role holds; each relay at both polarities; the parked set of
 *        200 seeded maps (FW-04).
 */
#include "bench.h"
#include "i2c_bus.h"
#include "pin_boot.h"
#include "pin_table.h"
#include "pinmap.h"
#include "pinmap_store.h"
#include "relay.h"
#include "scope.h"
#include "unit.h"

#include <soc/gpio_sig_map.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

using namespace kleidos::meter;
using bench::Bench;
using bench::field;
using bench::Scope;
using pin_table::Role;
using pinmap::kDefaultMap;
using pinmap::PinMap;
using pinmap::Pull;
using relay::Drive;

namespace {

/// The text `SYSTem:PINS?` gives the default map.
const std::string kDefaultText =
    "sda=4 scl=5 alert=6 addr=auto scl_hz=400000 pull_sda=up pull_scl=up pull_alert=up "
    "relay1=7 relay1_active=low relay2=15 relay2_active=high";

/// A map unlike the default in every field.
PinMap otherMap() {
    PinMap map      = kDefaultMap;
    map.pins        = { 8, 9, 10, 11, 12 };
    map.pullSda     = Pull::Off;
    map.pullScl     = Pull::Off;
    map.pullAlert   = Pull::Off;
    map.relay1Drive = Drive::ActiveHigh;
    map.relay2Drive = Drive::ActiveLow;
    map.address     = 0x45;
    map.sclHz       = 100'000;
    return map;
}

/// The default map with its bus on @p sda and @p scl.
PinMap onBus( std::int32_t sda, std::int32_t scl ) {
    return pinmap::withPin( pinmap::withPin( kDefaultMap, Role::Sda, sda ), Role::Scl, scl );
}

/// Wires @p world's board as @p map says.
void wireTo( sim::World& world, const PinMap& map ) {
    world.wires.sda            = map.pins[0];
    world.wires.scl            = map.pins[1];
    world.wires.alert          = map.pins[2];
    world.wires.lead           = map.pins[3];
    world.wires.bypass         = map.pins[4];
    world.wires.leadActiveHigh = map.relay1Drive == Drive::ActiveHigh;
    world.circuit.bypassPart   = map.relay2Drive == Drive::ActiveHigh ? sim::BypassPart::Relay2Click
                                                                      : sim::BypassPart::Hw316;
}

/// Keeps @p map as the board's newest record, as an apply on an earlier boot.
void store( const PinMap& map ) {
    REQUIRE( pinmap_store::save( map ) == pinmap_store::Saved::Ok );
}

/// A boot of the rig as wired (rev D) with @p map stored.
Bench::Setup storing( const PinMap& map ) {
    return [map]( sim::World& ) { store( map ); };
}

/// Whether @p pad is parked: an input with the pull-down, nothing driving it.
bool parked( const sim::Pad& pad ) {
    return pad.configured && pad.input && !pad.output && !pad.oneByPeripheral && pad.pullDown
           && !pad.pullUp;
}

/// Whether @p gpio carries the bus's @p signal.
bool carries( const sim::World& world, std::size_t gpio, int signal ) {
    const sim::Pad& pad = world.pads[gpio];
    return pad.oneByPeripheral && pad.openDrain && pad.outSignal == signal && !pad.pullDown;
}

}  // namespace

// --- The record -----------------------------------------------------------------------

TEST( "pin_boot", "each source has its word" ) {
    CHECK_EQ( std::string( pin_boot::sourceWord( pin_boot::Source::Default ) ), "default" );
    CHECK_EQ( std::string( pin_boot::sourceWord( pin_boot::Source::Stored ) ), "stored" );
    CHECK_EQ( std::string( pin_boot::sourceWord( pin_boot::Source::Fallback ) ), "fallback" );
}

TEST( "pin_boot", "the load reads the flash and touches no pin" ) {
    Scope                scope;
    const pin_boot::Live none = pin_boot::load();
    CHECK( none.map == kDefaultMap && none.stored == kDefaultMap );
    CHECK( none.source == pin_boot::Source::Default && none.bus == pin_boot::Bus::Sound );
    store( otherMap() );
    const std::size_t    changes = scope->flash.changes;
    const pin_boot::Live other   = pin_boot::load();
    CHECK( other.map == otherMap() && other.stored == otherMap() );
    CHECK( other.source == pin_boot::Source::Stored && other.bus == pin_boot::Bus::Sound );
    CHECK( scope->flash.changes == changes );
    for ( const sim::Pad& pad : scope->pads ) {
        CHECK( !pad.configured );
    }
    // The defaults, stored, are the defaults.
    store( kDefaultMap );
    CHECK( pin_boot::load().source == pin_boot::Source::Default );
    // A flash that cannot be read is no record.
    store( otherMap() );
    scope->flash.readsUntilFailure = 0;
    const pin_boot::Live unread    = pin_boot::load();
    CHECK( unread.map == kDefaultMap && unread.source == pin_boot::Source::Default );
}

// --- The boots --------------------------------------------------------------------------

TEST( "pin_boot", "with no record the default map runs, and every report says so" ) {
    Bench b;
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultText + " src=default verified=1 table=0x90072FE9" );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "pins_src" ), "default" );
    CHECK_EQ( field( b.ask( "SYST:HEAL?" ), "pins_src" ), "default" );
    CHECK_EQ( b.ask( "*TST?" ), "0" );
    CHECK( b.bringUp() == BringUpResult::Ok );
}

TEST( "pin_boot", "a stored map on the board wired to it runs whole" ) {
    const PinMap map = otherMap();
    Bench        b( [map]( sim::World& w ) {
        wireTo( w, map );
        w.ina.address   = 0x45;
        w.circuit.dutNa = 5'000'000;  // 500 uV: the null path's check can judge it
        store( map );
    } );
    sim::World&  w = b.world();
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS?" ),
              "sda=8 scl=9 alert=10 addr=0x45 scl_hz=100000 pull_sda=off pull_scl=off "
              "pull_alert=off relay1=11 relay1_active=high relay2=12 relay2_active=low "
              "src=stored verified=0 table=0x90072FE9" );
    // The staged map starts as the live one.
    CHECK_EQ( field( b.ask( "SYST:PINS:PEND?" ), "relay2" ), "12" );
    CHECK_EQ( b.ask( "SYST:PINS:STAG SDA,18;CLE;PEND?" ),
              "sda=8 scl=9 alert=10 addr=0x45 scl_hz=100000 pull_sda=off pull_scl=off "
              "pull_alert=off relay1=11 relay1_active=high relay2=12 relay2_active=low "
              "valid=1 problem=- at=-" );
    // The bus: its pads, its pulls, its clock, the one address asked for.
    CHECK( carries( w, 8, I2CEXT0_SDA_OUT_IDX ) && !w.pads[8].pullUp );
    CHECK( carries( w, 9, I2CEXT0_SCL_OUT_IDX ) && !w.pads[9].pullUp );
    CHECK( w.pads[10].input && !w.pads[10].pullUp && !w.pads[10].pullDown );
    CHECK( w.ina.probes == 1 );
    const std::string info = b.ask( "SYST:INF?" );
    CHECK_EQ( field( info, "chip" ), "ina228" );
    CHECK_EQ( field( info, "addr" ), "0x45" );
    CHECK_EQ( field( info, "pins_src" ), "stored" );
    CHECK_EQ( field( b.ask( "SYST:HEAL?" ), "pins_src" ), "stored" );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "100000" );
    // `*RST` goes back to the map's boot clock, not to the default's.
    b.tell( "SYST:COMM:I2C:FREQ 200000" );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "200000" );
    b.tell( "*RST" );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "100000" );
    const int written = w.i2c.timingWrites;
    b.tell( "*RST" );
    CHECK( w.i2c.timingWrites == written );
    CHECK_EQ( b.ask( "*TST?" ), "0" );
    // The relays: where the map puts them, released first, energized by its
    // levels, and nothing else moved.
    CHECK( w.pads[11].firstDriven == 0 && w.pads[12].firstDriven == 1 );
    CHECK( w.pads[11].level() == 0 && w.pads[12].level() == 1 );
    const std::string route = b.ask( "ROUT:STAT?" );
    CHECK_EQ( field( route, "pads" ), "ok" );
    CHECK_EQ( field( route, "relay1_gpio" ), "11" );
    CHECK_EQ( field( route, "relay1_active" ), "high" );
    CHECK_EQ( field( route, "relay2_gpio" ), "12" );
    CHECK_EQ( field( route, "relay2_active" ), "low" );
    const std::string on = b.ask( "ROUT:NULL ON;:ROUT:STAT?" );
    CHECK_EQ( field( on, "path" ), "bypass" );
    CHECK_EQ( field( on, "check" ), "ok" );
    CHECK( w.leadOpen() && w.bypassClosed() );
    CHECK( w.pads[11].level() == 1 && w.pads[12].level() == 0 );
    b.tell( "ROUT:NULL OFF", 200'000 );
    CHECK( !w.leadOpen() && !w.bypassClosed() && w.circuit.disconnects == 0 );
    // The rig's own pins are nobody's now, and parked.
    for ( const std::size_t gpio : { 4U, 5U, 6U, 7U, 15U } ) {
        CHECK( parked( w.pads[gpio] ) );
    }
    // ALERT paces from the map's pin, and the capture's header names the map.
    b.tell( "SENS:PROF STAT;:INIT", 2'000'000 );
    const std::string header = b.ask( "FETC:HEAD?" );
    CHECK_EQ( field( header, "pace" ), "alert" );
    CHECK_EQ( field( header, "pins_src" ), "stored" );
    b.tell( "ABOR", 200'000 );
    CHECK( b.clean() );
}

TEST( "pin_boot", "a stored bus the sensor is not on falls back to the default bus" ) {
    PinMap map    = onBus( 18, 21 );
    map.address   = 0x41;
    map.sclHz     = 100'000;
    map.pullSda   = Pull::Off;
    map.pullAlert = Pull::Off;
    map           = pinmap::withPin( map, Role::Relay1, 10 );
    Bench       b( [map]( sim::World& w ) {
        w.wires.lead = 10;
        store( map );
    } );
    sim::World& w = b.world();
    // One error, which names the bus the record asked for.
    CHECK_EQ( b.errors(),
              "117,\"Pin map reverted;the stored bus (sda=18 scl=21 addr=0x41) did not answer: "
              "the default bus is back (the record is kept)\"" );
    // The bus is the default one, whole; the rest is the record's.
    CHECK_EQ( b.ask( "SYST:PINS?" ),
              "sda=4 scl=5 alert=6 addr=auto scl_hz=400000 pull_sda=up pull_scl=up "
              "pull_alert=off relay1=10 relay1_active=low relay2=15 relay2_active=high "
              "src=fallback verified=0 table=0x90072FE9" );
    CHECK_EQ( field( b.ask( "SYST:PINS:PEND?" ), "sda" ), "4" );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "pins_src" ), "fallback" );
    CHECK_EQ( field( b.ask( "SYST:HEAL?" ), "pins_src" ), "fallback" );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "chip" ), "ina228" );
    CHECK_EQ( b.ask( "*TST?" ), "0" );
    CHECK( carries( w, 4, I2CEXT0_SDA_OUT_IDX ) && carries( w, 5, I2CEXT0_SCL_OUT_IDX ) );
    CHECK( w.pads[4].pullUp && w.pads[5].pullUp && w.i2c.core == 1 );
    // The pads the record's bus had carry nothing and are parked.
    CHECK( parked( w.pads[18] ) && parked( w.pads[21] ) );
    // The relay was never second-guessed: it sits where the record says.
    CHECK( w.pads[10].driving() && w.pads[10].firstDriven == 1 && parked( w.pads[7] ) );
    CHECK( !w.leadOpen() && w.circuit.disconnects == 0 );
    // The record is kept for the person to see.
    CHECK( pinmap_store::load().map == map );
    CHECK( b.pins().stored == map && b.clean() );
}

TEST( "pin_boot", "a swapped pair, and a pinned address nothing answers at, fall back too" ) {
    {
        Bench b( storing( onBus( 5, 4 ) ) );
        CHECK_EQ( field( b.ask( "SYST:PINS?" ), "src" ), "fallback" );
        CHECK( carries( b.world(), 4, I2CEXT0_SDA_OUT_IDX ) );
        CHECK( carries( b.world(), 5, I2CEXT0_SCL_OUT_IDX ) );
        CHECK_CONTAINS( b.errors(), "(sda=5 scl=4 addr=auto) did not answer" );
        CHECK_EQ( b.ask( "*TST?" ), "0" );
    }
    PinMap pinned  = kDefaultMap;
    pinned.address = 0x41;
    Bench b( storing( pinned ) );
    // The one address was asked for, then the default bus's scan.
    CHECK( b.world().ina.probes == 1 + 16 );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultText + " src=fallback verified=1 table=0x90072FE9" );
    CHECK_CONTAINS( b.errors(), "(sda=4 scl=5 addr=0x41) did not answer" );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "addr" ), "0x40" );
}

TEST( "pin_boot", "a stored bus whose default pins another role holds does not fall back" ) {
    for ( const Role role : { Role::Alert, Role::Relay1, Role::Relay2 } ) {
        for ( const std::int32_t gpio : { 4, 5 } ) {
            const PinMap map = pinmap::withPin( onBus( 18, 21 ), role, gpio );
            Bench        b( [map]( sim::World& w ) {
                wireTo( w, map );
                w.wires.sda = 4;  // the INA228 stayed where the rig has it
                w.wires.scl = 5;
                store( map );
            } );
            sim::World&  w = b.world();
            CHECK_EQ( b.errors(),
                      "117,\"Pin map reverted;the stored bus (sda=18 scl=21 addr=auto) did not "
                      "answer and a role holds the default bus pins: no INA228\"" );
            const std::string pins = b.ask( "SYST:PINS?" );
            CHECK_EQ( field( pins, "src" ), "stored" );
            CHECK_EQ( field( pins, "sda" ), "18" );
            // No INA228, and the console up.
            CHECK_EQ( field( b.ask( "SYST:INF?" ), "chip" ), "none" );
            CHECK_EQ( b.ask( "*IDN?" ).substr( 0, 21 ), "KLEIDOS,KLEIDOS-METER" );
            CHECK_EQ( b.ask( "MEAS:CURR?" ), "" );
            CHECK_CONTAINS( b.errors(), "-241," );
            // The relays are released and answer.
            CHECK( !w.leadOpen() && !w.bypassClosed() && w.circuit.disconnects == 0 );
            CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "pads" ), "ok" );
            // The record's bus keeps its pads, and nobody took the default's.
            CHECK( carries( w, 18, I2CEXT0_SDA_OUT_IDX ) );
            CHECK( !carries( w, 4, I2CEXT0_SDA_OUT_IDX ) );
        }
    }
}

TEST( "pin_boot", "a fallback whose default bus is silent too says both" ) {
    Bench             b( []( sim::World& w ) {
        w.ina.present = false;
        store( onBus( 18, 21 ) );
    } );
    const std::string errors = b.errors();
    CHECK_CONTAINS( errors, "117,\"Pin map reverted;the stored bus (sda=18 scl=21 addr=auto)" );
    CHECK_CONTAINS( errors, "-241,\"Hardware missing;no device (scan found none)\"" );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "src" ), "fallback" );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "chip" ), "none" );
}

TEST( "pin_boot", "a transaction that fails on the stored bus is a bus that did not answer" ) {
    Bench b( []( sim::World& w ) {
        wireTo( w, onBus( 18, 21 ) );
        w.ina.failEvery = 1;  // it acknowledges its address and no register
        store( onBus( 18, 21 ) );
    } );
    CHECK( b.bringUp() == BringUpResult::NoDevice );  // the default bus's
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "src" ), "fallback" );
    CHECK_CONTAINS( b.errors(), "117," );
}

TEST( "pin_boot", "a part that answers and is not a sound INA228 is not a wrong bus" ) {
    const PinMap map = onBus( 18, 21 );
    Bench        b( [map]( sim::World& w ) {
        wireTo( w, map );
        w.ina.memStat = false;
        store( map );
    } );
    CHECK( b.bringUp() == BringUpResult::TrimChecksum );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "src" ), "stored" );
    const std::string errors = b.errors();
    CHECK_CONTAINS( errors, "trim memory checksum" );
    CHECK( errors.find( "117," ) == std::string::npos );
}

TEST( "pin_boot", "the default bus has nothing to fall back to" ) {
    // The defaults, and a record whose bus is the default one: no INA228 on
    // either is the absent part alone.
    for ( const bool relayMoved : { false, true } ) {
        Bench             b( [relayMoved]( sim::World& w ) {
            w.ina.present = false;
            if ( relayMoved ) {
                store( pinmap::withPin( kDefaultMap, Role::Relay1, 10 ) );
            }
        } );
        const std::string pins = b.ask( "SYST:PINS?" );
        CHECK_EQ( field( pins, "src" ), relayMoved ? "stored" : "default" );
        CHECK_EQ( field( pins, "verified" ), relayMoved ? "0" : "1" );
        CHECK_EQ( b.errors(), "-241,\"Hardware missing;no device (scan found none)\"" );
        CHECK( b.world().ina.probes == 16 );  // one scan, not two
    }
}

TEST( "pin_boot", "a bus that will not open is reported, not fallen back from" ) {
    Scope        scope;
    Ina228Driver device;
    REQUIRE( device.openBus( GPIO_NUM_4, GPIO_NUM_5, 400'000, {} ) == ESP_OK );
    pin_boot::Live pins;
    CHECK( pin_boot::bringUp( device, pins ) == BringUpResult::BusError );
    CHECK( pins.map == kDefaultMap && pins.source == pin_boot::Source::Default );
    // Closed, the same call opens it, on any pads.
    device.closeBus();
    CHECK( !device.ready() && device.busSpeedHz() == 0 && device.manufacturerId() == 0 );
    CHECK( device.address() == ina228::kAddressDefault );
    std::array<std::uint8_t, 4> found = {};
    CHECK( device.scan( found.data(), found.size() ) == 0 );
    CHECK( device.write16( ina228::kRegConfig, 0 ) == ESP_ERR_INVALID_STATE );
    CHECK( pin_boot::bringUp( device, pins ) == BringUpResult::Ok );
    CHECK( device.ready() && device.manufacturerId() == 0x5449 && device.dieId() == 0x228 );
    device.closeBus();
    CHECK( !device.ready() && device.dieId() == 0 && device.revisionId() == 0 );
}

TEST( "pin_boot", "a boot from a stored map and a fallback allocate nothing" ) {
    for ( const PinMap& map : { otherMap(), onBus( 18, 21 ) } ) {
        Bench             b( storing( map ) );
        const std::string heap = b.ask( "DIAG:HEAP?" );
        CHECK_EQ( field( heap, "allocs_since_main" ), "0" );
        CHECK_EQ( field( heap, "frees_since_main" ), "0" );
    }
}

// --- The INA228's address -----------------------------------------------------------------

TEST( "pin_boot", "a pinned address is the one asked for, whatever else answers" ) {
    PinMap map  = kDefaultMap;
    map.address = 0x44;
    Bench b( [map]( sim::World& w ) {
        w.ina.address     = 0x44;
        w.ina.alsoAnswers = 0x40;  // a scan would take the lower one
        store( map );
    } );
    CHECK( b.clean() );
    CHECK( b.world().ina.probes == 1 );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "addr" ), "0x44" );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "src" ), "stored" );
}

TEST( "pin_boot", "each bus line and ALERT have the pull the map gives them" ) {
    for ( const Pull pull : { Pull::Up, Pull::Off } ) {
        for ( std::size_t line = 0; line < 3U; ++line ) {
            PinMap map = kDefaultMap;
            // The line under test differs from the two beside it.
            const Pull others = pull == Pull::Up ? Pull::Off : Pull::Up;
            map.pullSda       = line == 0U ? pull : others;
            map.pullScl       = line == 1U ? pull : others;
            map.pullAlert     = line == 2U ? pull : others;
            Bench b( storing( map ) );
            for ( std::size_t other = 0; other < 3U; ++other ) {
                const bool up = ( other == line ? pull : others ) == Pull::Up;
                CHECK( b.world().pads[4U + other].pullUp == up );
            }
            CHECK( b.clean() );
        }
    }
}

// --- The relays ----------------------------------------------------------------------------

TEST( "pin_boot", "each relay's pad is first driven at its released level, either polarity" ) {
    for ( const Drive lead : { Drive::ActiveLow, Drive::ActiveHigh } ) {
        for ( const Drive bypass : { Drive::ActiveLow, Drive::ActiveHigh } ) {
            PinMap map      = kDefaultMap;
            map.relay1Drive = lead;
            map.relay2Drive = bypass;
            Scope scope;
            wireTo( *scope.world, map );
            // What the output registers were before is the worst case: each
            // latch holds the level that energizes its relay.
            scope->pads[7].latch  = static_cast<int>( relay::energizeLevel( lead ) );
            scope->pads[15].latch = static_cast<int>( relay::energizeLevel( bypass ) );
            relay::Bank bank;
            REQUIRE( bank.begin( map ) );
            const int leadReleased   = static_cast<int>( relay::releaseLevel( lead ) );
            const int bypassReleased = static_cast<int>( relay::releaseLevel( bypass ) );
            CHECK( scope->pads[7].firstDriven == leadReleased && scope->pads[7].enabledAt == 1 );
            CHECK( scope->pads[15].firstDriven == bypassReleased );
            CHECK( scope->pads[15].enabledAt == 1 );
            CHECK( !scope->leadOpen() && !scope->bypassClosed() );
            CHECK( scope->circuit.disconnects == 0 );
            // Driven, read back, and each a mismatch against the other level.
            bank.drive( relay::kBypass, true );
            CHECK( scope->pads[15].level() == 1 - bypassReleased && scope->bypassClosed() );
            bank.drive( relay::kLead, true );
            CHECK( scope->pads[7].level() == 1 - leadReleased && scope->leadOpen() );
            CHECK( bank.padMismatches() == 0 );
            scope->pads[7].forced  = leadReleased;
            scope->pads[15].forced = bypassReleased;
            CHECK( bank.padMismatches()
                   == ( relay::bitOf( relay::kLead ) | relay::bitOf( relay::kBypass ) ) );
            scope->pads[7].forced  = -1;
            scope->pads[15].forced = -1;
            bank.drive( relay::kLead, false );
            bank.drive( relay::kBypass, false );
            CHECK( bank.energized() == 0 && bank.padMismatches() == 0 );
            CHECK( scope->circuit.disconnects == 0 );
        }
    }
}

TEST( "pin_boot", "a reset's record names the coils, whichever pins the next map puts them on" ) {
    Scope scope;
    scope->resetReason = 4;  // a panic: RTC memory kept
    {
        relay::Bank bank;
        REQUIRE( bank.begin( kDefaultMap ) );
        bank.drive( relay::kBypass, true );
    }
    relay::Bank moved;
    REQUIRE( moved.begin( otherMap() ) );
    CHECK( moved.releasedByReset() == relay::bitOf( relay::kBypass ) );
}

TEST( "pin_boot", "park() takes the pins it is given and no other, and any a bus was on" ) {
    Scope scope;
    REQUIRE( i2c_bus::begin( 18, 21, 400'000, {} ) == ESP_OK );
    CHECK( carries( *scope.world, 18, I2CEXT0_SDA_OUT_IDX ) );
    relay::Bank::park( pinmap::pinBit( 18 ) | pinmap::pinBit( 0 ) | pinmap::pinBit( 48 ) );
    for ( std::size_t gpio = 0; gpio < scope->pads.size(); ++gpio ) {
        CHECK( parked( scope->pads[gpio] ) == ( gpio == 18U || gpio == 0U || gpio == 48U ) );
    }
    CHECK( carries( *scope.world, 21, I2CEXT0_SCL_OUT_IDX ) );
}

TEST( "pin_boot", "the bank parks parkedSet() of 200 seeded maps, pin for pin" ) {
    // xorshift32 from a fixed seed, so that every host runs the same maps.
    std::uint32_t state  = 0x6B6D6574U;
    const auto    random = [&state] {
        state ^= state << 13U;
        state ^= state >> 17U;
        state ^= state << 5U;
        return state;
    };
    std::size_t tried = 0;
    for ( std::size_t taken = 0; taken < 200U; ) {
        ++tried;
        REQUIRE( tried < 20'000U );
        PinMap map = kDefaultMap;
        for ( std::int32_t& gpio : map.pins ) {
            gpio = static_cast<std::int32_t>( random() % 49U );
        }
        map.relay1Drive = ( random() & 1U ) != 0U ? Drive::ActiveHigh : Drive::ActiveLow;
        map.relay2Drive = ( random() & 1U ) != 0U ? Drive::ActiveHigh : Drive::ActiveLow;
        if ( pinmap::validate( map ).problem != pinmap::Problem::None ) {
            continue;
        }
        ++taken;
        Scope       scope;
        relay::Bank bank;
        REQUIRE( bank.begin( map ) );
        pinmap::PinSet found  = 0;
        pinmap::PinSet driven = 0;
        for ( std::size_t gpio = 0; gpio < scope->pads.size(); ++gpio ) {
            const sim::Pad& pad = scope->pads[gpio];
            if ( parked( pad ) ) {
                found |= pinmap::pinBit( static_cast<std::int32_t>( gpio ) );
            } else if ( pad.configured ) {
                driven |= pinmap::pinBit( static_cast<std::int32_t>( gpio ) );
            }
        }
        CHECK( found == pinmap::parkedSet( map ) );
        CHECK( driven == ( pinmap::pinBit( map.pins[3] ) | pinmap::pinBit( map.pins[4] ) ) );
    }
}
