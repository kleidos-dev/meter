// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_console_pins.cpp
 * @brief The pin map's wire contract (0.18.0): `SYSTem:PINS?` true of the
 *        wiring the boot runs, the staging orders and validate()'s verdict on
 *        the staged map, and the keys appended to the existing reports; and the orders that
 *        change the board (FW-05): `SYSTem:PINS:APPLy` proved or reverted,
 *        saved, refused, `SYSTem:PINS:DEFault`, `SYSTem:PINS:RESult?`.
 */
#include "bench.h"
#include "flash_log.h"
#include "flash_port.h"
#include "pin_apply.h"
#include "pin_boot.h"
#include "pin_table.h"
#include "pinmap.h"
#include "pinmap_store.h"
#include "relay.h"
#include "scope.h"
#include "unit.h"

#include <soc/gpio_sig_map.h>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

using namespace kleidos::meter;
using bench::Bench;
using bench::field;
using pin_table::Role;
using pinmap::kDefaultMap;
using pinmap::PinMap;

namespace {

/// The live map's twelve keys: the rig as wired, rev D.
const std::string kDefaultKeys =
    "sda=4 scl=5 alert=6 addr=auto scl_hz=400000 pull_sda=up pull_scl=up pull_alert=up"
    " relay1=7 relay1_active=low relay2=15 relay2_active=high";

/// `SYSTem:PINS:PENDing?`'s answer: the staged map's @p keys, then @p verdict.
std::string pending( const std::string& keys, const std::string& verdict ) {
    return keys + " " + verdict;
}

/// @p keys with `key=` set to @p value.
std::string with( std::string keys, const std::string& key, const std::string& value ) {
    const std::string      marker = key + "=";
    std::string::size_type at = ( keys.rfind( marker, 0 ) == 0 ) ? 0 : keys.find( " " + marker );
    if ( at != 0 ) {
        ++at;
    }
    const std::string::size_type end = keys.find( ' ', at );
    keys.replace( at, ( end == std::string::npos ? keys.size() : end ) - at, marker + value );
    return keys;
}

/// The default map's report, unmoved.
const std::string kDefaultReport = kDefaultKeys + " src=default verified=1 table=0x90072FE9";

/// The longest an apply takes on the modeled board, a whole scan that times
/// out and its revert included.
constexpr sim::Micros kApplyUs = 5'000'000;

/// How many errors the queue holds.
std::string queued( Bench& b ) {
    return b.ask( "SYST:ERR:COUN?" );
}

/// Stages @p units on a clean queue and applies them.
void apply( Bench& b, const std::string& units ) {
    b.tell( "*CLS" );
    if ( !units.empty() ) {
        b.tell( units );
    }
    CHECK_EQ( queued( b ), "0" );
    b.tell( "SYST:PINS:APPL", kApplyUs );
}

/// Whether @p pad is parked: an input nothing drives, with the pull of @p high.
bool parkedAt( const sim::Pad& pad, bool high ) {
    return pad.configured && pad.input && !pad.output && !pad.oneByPeripheral
           && pad.pullDown == !high && pad.pullUp == high;
}

/// Whether @p gpio carries the bus's @p signal.
bool carries( Bench& b, std::size_t gpio, int signal ) {
    const sim::Pad& pad = b.world().pads[gpio];
    return pad.oneByPeripheral && pad.openDrain && pad.outSignal == signal && !pad.pullDown;
}

/// Everything of the board the default map has, as a boot leaves it.
void checkRig( Bench& b ) {
    sim::World& w = b.world();
    CHECK( carries( b, 4, I2CEXT0_SDA_OUT_IDX ) && carries( b, 5, I2CEXT0_SCL_OUT_IDX ) );
    CHECK( w.pads[4].pullUp && w.pads[5].pullUp && w.i2c.core == 1 );
    CHECK( w.pads[6].input && !w.pads[6].output && w.pads[6].pullUp && !w.pads[6].pullDown );
    CHECK( w.pads[7].driving() && w.pads[7].level() == 1 );
    CHECK( w.pads[15].driving() && w.pads[15].level() == 0 );
    CHECK( w.gpioIntrType.at( 6 ) == GPIO_INTR_NEGEDGE );
    CHECK( !w.leadOpen() && !w.bypassClosed() && w.circuit.disconnects == 0 );
}

/// A failed apply left everything as it was: the live map, the board, the
/// record, and one error, @p error.
void checkReverted( Bench& b, const std::string& error, const std::string& result ) {
    CHECK_EQ( queued( b ), "1" );
    CHECK_EQ( b.errors(), error );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ), result );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
    CHECK_EQ( b.ask( "*TST?", 2'000'000 ), "0" );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "chip" ), "ina228" );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "400000" );
    CHECK( pinmap_store::load().found == pinmap_store::Found::Empty );
    checkRig( b );
    CHECK( b.clean() );
}

/// The flash of a board, for the boot after it.
Bench::Setup withFlashOf( Bench& b ) {
    return [bytes = b.world().flash.bytes]( sim::World& w ) { w.flash.bytes = bytes; };
}

/// The one error a unit queued, as `SYSTem:ERRor:ALL?` reads it.
std::string only( Bench& b, const std::string& unit ) {
    b.tell( "*CLS" );
    b.tell( unit );
    return b.errors();
}

}  // namespace

TEST( "pins", "the live map is the fixed wiring, from the defaults, verified, with the table" ) {
    Bench b;
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultKeys + " src=default verified=1 table=0x90072FE9" );
    CHECK_EQ( b.ask( "SYSTEM:PINS?" ), b.ask( "syst:pins?" ) );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ), pending( kDefaultKeys, "valid=1 problem=- at=-" ) );
    CHECK( b.clean() );
}

TEST( "pins", "the three refusals the contract names: GPIO45, one pin twice, ALERT on 39" ) {
    Bench b;
    b.tell( "SYST:PINS:STAG SDA,45" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ),
              pending( with( kDefaultKeys, "sda", "45" ), "valid=0 problem=reserved at=sda" ) );
    b.tell( "SYST:PINS:CLE;:SYST:PINS:STAG SDA,5" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ),
              pending( with( kDefaultKeys, "sda", "5" ), "valid=0 problem=shared at=scl" ) );
    b.tell( "SYST:PINS:CLE;:SYST:PINS:STAG ALER,39" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ),
              pending( with( kDefaultKeys, "alert", "39" ), "valid=0 problem=too_high at=alert" ) );
    CHECK( b.clean() );
    // Staging changed nothing on the board, nor the live map.
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultKeys + " src=default verified=1 table=0x90072FE9" );
}

TEST( "pins", "every role, each option, NONE, and clear back to the live map" ) {
    Bench b;
    b.tell( "SYST:PINS:STAG SDA,8,OFF;:SYST:PINS:STAG SCL,9,UP;:SYST:PINS:STAG ALERT,10,OFF" );
    b.tell( "SYST:PINS:STAG RELAY1,11,HIGH;:SYST:PINS:STAG rel2,12,low" );
    std::string keys = kDefaultKeys;
    keys             = with( keys, "sda", "8" );
    keys             = with( keys, "pull_sda", "off" );
    keys             = with( keys, "scl", "9" );
    keys             = with( keys, "alert", "10" );
    keys             = with( keys, "pull_alert", "off" );
    keys             = with( keys, "relay1", "11" );
    keys             = with( keys, "relay1_active", "high" );
    keys             = with( keys, "relay2", "12" );
    keys             = with( keys, "relay2_active", "low" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ), pending( keys, "valid=1 problem=- at=-" ) );
    // UP on SCL and the other pulls back; a relay with no option keeps its level.
    b.tell( "SYST:PINS:STAG SCL,9,OFF;:SYST:PINS:STAG SDA,8,UP;:SYST:PINS:STAG REL1,13" );
    keys = with( with( with( keys, "pull_scl", "off" ), "pull_sda", "up" ), "relay1", "13" );
    b.tell( "SYST:PINS:STAG REL2,12,HIGH;:SYST:PINS:STAG REL1,13,LOW" );
    keys = with( with( keys, "relay2_active", "high" ), "relay1_active", "low" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ), pending( keys, "valid=1 problem=- at=-" ) );
    b.tell( "SYST:PINS:STAG SCL,NONE;:SYST:PINS:STAG ALER,9,UP" );
    keys = with( with( with( keys, "scl", "none" ), "alert", "9" ), "pull_alert", "up" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ), pending( keys, "valid=0 problem=missing at=scl" ) );
    CHECK( b.clean() );
    b.tell( "SYST:PINS:CLE" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ), pending( kDefaultKeys, "valid=1 problem=- at=-" ) );
}

TEST( "pins", "the address and the clock, staged and judged" ) {
    Bench b;
    b.tell( "SYST:PINS:ADDR #H41" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ),
              pending( with( kDefaultKeys, "addr", "0x41" ), "valid=1 problem=- at=-" ) );
    b.tell( "SYST:PINS:ADDR 79" );
    CHECK_EQ( field( b.ask( "SYST:PINS:PEND?" ), "addr" ), "0x4F" );
    b.tell( "SYST:PINS:ADDR 255" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ),
              pending( with( kDefaultKeys, "addr", "0xFF" ), "valid=0 problem=address at=addr" ) );
    b.tell( "SYST:PINS:ADDR AUTO;:SYST:PINS:CLOC 100000" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ),
              pending( with( kDefaultKeys, "scl_hz", "100000" ), "valid=1 problem=- at=-" ) );
    b.tell( "SYST:PINS:CLOC 49999" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ), pending( with( kDefaultKeys, "scl_hz", "49999" ),
                                                   "valid=0 problem=clock at=scl_hz" ) );
    CHECK( b.clean() );
}

TEST( "pins", "a refused staging queues one error and stages nothing" ) {
    Bench             b;
    const std::string before = b.ask( "SYST:PINS:PEND?" );
    const std::string kStage =
        "use SYSTem:PINS:STAGe SDA|SCL|ALERt|RELay1|RELay2,<gpio>|NONE[,HIGH|LOW|UP|OFF]";
    const std::string kAddress =
        "use SYSTem:PINS:ADDRess AUTO|<1..255> (the INA228 answers at #H40..#H4F)";
    CHECK_EQ( only( b, "SYST:PINS:STAG SDX,4" ),
              "-224,\"Illegal parameter value;" + kStage + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:STAG 4,4" ), "-104,\"Data type error;" + kStage + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:STAG SDA" ), "-109,\"Missing parameter;" + kStage + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:STAG SDA,64" ), "-222,\"Data out of range;" + kStage + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:STAG SDA,ZZZ" ),
              "-224,\"Illegal parameter value;" + kStage + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:STAG SDA,-1" ), "-222,\"Data out of range;" + kStage + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:STAG SDA,8,HIGH" ),
              "-224,\"Illegal parameter value;" + kStage + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:STAG REL1,8,UP" ),
              "-224,\"Illegal parameter value;" + kStage + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:ADDR SOME" ),
              "-224,\"Illegal parameter value;" + kAddress + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:ADDR" ), "-109,\"Missing parameter;" + kAddress + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:ADDR 0" ), "-222,\"Data out of range;" + kAddress + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:ADDR 256" ), "-222,\"Data out of range;" + kAddress + "\"" );
    CHECK_EQ( only( b, "SYST:PINS:CLOC" ),
              "-109,\"Missing parameter;use SYSTem:PINS:CLOCk <hz>\"" );
    CHECK_EQ( b.ask( "SYST:PINS:PEND?" ), before );
}

TEST( "pins", "the keys appended to the reports, in their place at the end" ) {
    Bench             b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
    const std::string pins = " pins_src=default pins_table=0x90072FE9";
    CHECK( b.ask( "SYST:INF?" ).ends_with( pins ) );
    CHECK( b.ask( "SYST:HEAL?" ).ends_with( pins ) );
    CHECK( b.ask( "ROUT:STAT?" )
               .ends_with( " bypass_part=relay2click relay1_gpio=7 relay1_active=low"
                           " relay2_gpio=15 relay2_active=high" ) );
    // STATE converts the bus, TRACE does not: the bus's conversion time follows.
    b.tell( "SENS:PROF STATE" );
    CHECK( b.ask( "SENS:PROF:REP?" ).ends_with( " avg=1 avg_state=off vbusct_us=50" ) );
    b.tell( "SENS:PROF TRAC" );
    CHECK( b.ask( "SENS:PROF:REP?" ).ends_with( " avg=1 avg_state=off vbusct_us=0" ) );
    b.tell( "SENS:PROF STATE;:INIT", 100'000 );
    b.tell( "ABOR", 50'000 );
    CHECK( b.ask( "FETC:HEAD?" ).ends_with( " avg_state=off pins_src=default" ) );
    CHECK( b.clean() );
}

// --- SYSTem:PINS:APPLy: proved ------------------------------------------------------------

TEST( "pins_apply", "before any apply the result is none" ) {
    Bench b;
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ), "result=none reason=- mfg=- die=- tst=- saved=0" );
}

TEST( "pins_apply", "a good change is proved, saved, and survives a reboot" ) {
    // The board is wired to the map the host stages, all but its relays.
    const std::string keys = with(
        with( with( with( with( with( kDefaultKeys, "sda", "8" ), "scl", "9" ), "alert", "10" ),
                    "addr", "0x44" ),
              "scl_hz", "100000" ),
        "pull_sda", "off" );
    std::vector<std::uint8_t> flash;
    {
        Bench       b;
        sim::World& w = b.world();
        w.wires.sda   = 8;
        w.wires.scl   = 9;
        w.wires.alert = 10;
        w.ina.address = 0x44;
        apply( b,
               "SYST:PINS:STAG SDA,8,OFF;:SYST:PINS:STAG SCL,9;:SYST:PINS:STAG ALER,10;:"
               "SYST:PINS:ADDR #H44;:SYST:PINS:CLOC 100000" );
        CHECK( b.clean() );
        CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
                  "result=ok reason=- mfg=0x5449 die=0x228 tst=0 saved=1" );
        CHECK_EQ( b.ask( "SYST:PINS?" ), keys + " src=stored verified=1 table=0x90072FE9" );
        CHECK_EQ( b.ask( "SYST:PINS:PEND?" ), pending( keys, "valid=1 problem=- at=-" ) );
        CHECK_EQ( field( b.ask( "SYST:INF?" ), "pins_src" ), "stored" );
        CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "100000" );
        CHECK_EQ( b.ask( "*TST?", 2'000'000 ), "0" );
        // The bus and ALERT are on their pads, and the ones they left parked.
        CHECK( carries( b, 8, I2CEXT0_SDA_OUT_IDX ) && !w.pads[8].pullUp );
        CHECK( carries( b, 9, I2CEXT0_SCL_OUT_IDX ) && w.pads[9].pullUp );
        CHECK( w.gpioIntrType.at( 10 ) == GPIO_INTR_NEGEDGE );
        CHECK( w.gpioIntrType.at( 6 ) == GPIO_INTR_DISABLE );
        for ( const std::size_t gpio : { 4U, 5U, 6U } ) {
            CHECK( parkedAt( w.pads[gpio], false ) );
        }
        // A capture paces from the new ALERT pin, and its header names the map.
        b.tell( "SENS:PROF STAT;:INIT", 2'000'000 );
        const std::string header = b.ask( "FETC:HEAD?" );
        CHECK_EQ( field( header, "pace" ), "alert" );
        CHECK_EQ( field( header, "pins_src" ), "stored" );
        b.tell( "ABOR", 200'000 );
        CHECK( b.clean() );
        CHECK( w.circuit.disconnects == 0 );
        flash = w.flash.bytes;
    }
    Bench b( [flash]( sim::World& w ) {
        w.flash.bytes = flash;
        w.wires.sda   = 8;
        w.wires.scl   = 9;
        w.wires.alert = 10;
        w.ina.address = 0x44;
    } );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS?" ), keys + " src=stored verified=1 table=0x90072FE9" );
    CHECK_EQ( b.ask( "*TST?", 2'000'000 ), "0" );
}

TEST( "pins_apply", "the saved map is written after the proof, and no other change is" ) {
    Bench             b;
    const std::size_t changes = b.world().flash.changes;
    apply( b, "SYST:PINS:STAG SDA,18" );
    CHECK( b.world().flash.changes == changes );
    apply( b, "SYST:PINS:CLE;:SYST:PINS:CLOC 200000" );
    CHECK( b.world().flash.changes > changes );
    CHECK( pinmap_store::load().map.sclHz == 200'000U );
    CHECK( b.clean() );
}

TEST( "pins_apply", "the default map staged and applied is the default map, stored" ) {
    Bench b;
    apply( b, "SYST:PINS:CLOC 100000" );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "src" ), "stored" );
    apply( b, "SYST:PINS:CLOC 400000" );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ), "result=ok reason=- mfg=0x5449 die=0x228 tst=0 saved=1" );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
    CHECK( pinmap_store::load().found == pinmap_store::Found::Stored );
    CHECK( pinmap_store::load().map == kDefaultMap );
    checkRig( b );
}

TEST( "pins_apply", "the range, the running clock and the accumulators across an apply" ) {
    Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
    b.tell( "SENS:CURR:RANG:IND 1;:SYST:COMM:I2C:FREQ 1000000", 200'000 );
    CHECK( b.clean() );
    // Reverted: the range and the running clock are as they were.
    apply( b, "SYST:PINS:STAG SDA,18" );
    CHECK_CONTAINS( b.errors(), "117," );
    CHECK_EQ( b.ask( "SENS:CURR:RANG:IND?" ), "1" );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "1000000" );
    CHECK( b.device().range() == ina228::ShuntRange::Narrow41mV );
    CHECK( ( b.world().ina.regs[ina228::kRegConfig] & ina228::kConfigAdcRange ) != 0 );
    // Applied: the range stays, the clock is the map's.
    b.world().run( 60'000'000 );
    apply( b, "SYST:PINS:CLE" );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SENS:CURR:RANG:IND?" ), "1" );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "400000" );
    CHECK( ( b.world().ina.regs[ina228::kRegConfig] & ina228::kConfigAdcRange ) != 0 );
    // The device's reset cleared the accumulators, and their window starts there.
    const std::string charge = b.ask( "SENS:CHAR?", 400'000 );
    CHECK( b.clean() );
    CHECK( std::stoll( field( charge, "dur_us" ) ) < kApplyUs + 2'000'000 );
}

// --- SYSTem:PINS:APPLy: reverted ----------------------------------------------------------

TEST( "pins_apply", "a wrong SDA, a wrong SCL, the pair swapped: reverted, one error" ) {
    struct Wrong {
        const char* units;
        const char* bus;
    };
    for ( const Wrong& wrong :
          { Wrong{ "SYST:PINS:STAG SDA,18", "sda=18 scl=5 addr=auto" },
            Wrong{ "SYST:PINS:STAG SCL,21,OFF", "sda=4 scl=21 addr=auto" },
            Wrong{ "SYST:PINS:STAG SDA,5;:SYST:PINS:STAG SCL,4", "sda=5 scl=4 addr=auto" },
            Wrong{ "SYST:PINS:ADDR #H41", "sda=4 scl=5 addr=0x41" } } ) {
        Bench b;
        apply( b, wrong.units );
        checkReverted( b,
                       std::string( "117,\"Pin map reverted;the INA228 did not answer on " )
                           + wrong.bus + ": the last good map is back\"",
                       "result=reverted reason=no_chip mfg=- die=- tst=1 saved=0" );
        // What was refused stays staged, for the person to correct.
        CHECK( b.ask( "SYST:PINS:PEND?" ) != pending( kDefaultKeys, "valid=1 problem=- at=-" ) );
        // The pads the trial used are parked again.
        CHECK( parkedAt( b.world().pads[18], false ) && parkedAt( b.world().pads[21], false ) );
    }
}

TEST( "pins_apply", "an ALERT pin with no wire: reverted, one error" ) {
    Bench b;
    apply( b, "SYST:PINS:STAG ALER,10,OFF" );
    checkReverted( b, "117,\"Pin map reverted;no ALERT edge on GPIO10: the last good map is back\"",
                   "result=reverted reason=alert mfg=0x5449 die=0x228 tst=32 saved=0" );
    CHECK( parkedAt( b.world().pads[10], false ) );
    CHECK( b.world().gpioIntrType.at( 10 ) == GPIO_INTR_DISABLE );
}

TEST( "pins_apply", "a part that answers and is not a sound INA228: reverted" ) {
    Bench b;
    b.world().ina.memStat = false;
    apply( b, "SYST:PINS:CLOC 100000" );
    CHECK_EQ( queued( b ), "1" );
    CHECK_EQ( b.errors(),
              "117,\"Pin map reverted;the map failed its proof (chip): the last good "
              "map is back; it fails too (tst=1)\"" );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
              "result=reverted reason=chip mfg=0x5449 die=0x228 tst=1 saved=0" );
    // The instrument says what stopped it as a boot would.
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "chip" ), "none" );
    b.tell( "MEAS:CURR?" );
    CHECK_CONTAINS( b.errors(), "trim memory checksum" );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
}

TEST( "pins_apply", "a self-test part that fails after a good bring-up: reverted" ) {
    Bench b;
    // A pad that does not read its level is the relays' part.
    b.world().pads[7].forced = 0;
    apply( b, "SYST:PINS:CLOC 100000" );
    CHECK_EQ( queued( b ), "1" );
    CHECK_EQ( b.errors(),
              "117,\"Pin map reverted;the map failed its proof (pads): the last good "
              "map is back; it fails too (tst=64)\"" );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
              "result=reverted reason=pads mfg=0x5449 die=0x228 tst=64 saved=0" );
    b.world().pads[7].forced = -1;
    CHECK_EQ( b.ask( "*TST?", 2'000'000 ), "0" );
}

TEST( "pins_apply", "a relay pin the pin layer refuses: reverted, the relays back" ) {
    Bench b;
    b.world().faults.failGpioConfig    = true;
    b.world().faults.failGpioConfigPin = 16;
    apply( b, "SYST:PINS:STAG REL1,16" );
    b.world().faults.failGpioConfig = false;
    checkReverted( b,
                   "117,\"Pin map reverted;the map failed its proof (pads): the last good map is "
                   "back\"",
                   "result=reverted reason=pads mfg=0x5449 die=0x228 tst=4 saved=0" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "pads" ), "ok" );
}

TEST( "pins_apply", "an ALERT pin the pin layer refuses: no engine, then back" ) {
    Bench b;
    b.world().faults.failGpioConfig    = true;
    b.world().faults.failGpioConfigPin = 10;
    apply( b, "SYST:PINS:STAG ALER,10" );
    b.world().faults.failGpioConfig = false;
    checkReverted( b,
                   "117,\"Pin map reverted;the map failed its proof (engine): the last good map "
                   "is back\"",
                   "result=reverted reason=engine mfg=0x5449 die=0x228 tst=2 saved=0" );
}

TEST( "pins_apply", "a flash that does not take the record: reverted" ) {
    Bench b;
    b.world().flash.failProgram = true;
    apply( b, "SYST:PINS:CLOC 100000" );
    b.world().flash.failProgram = false;
    checkReverted( b,
                   "117,\"Pin map reverted;the map failed its proof (save): the last good map is "
                   "back\"",
                   "result=reverted reason=save mfg=0x5449 die=0x228 tst=0 saved=0" );
}

TEST( "pins_apply", "a bus's core that refuses its half: the relays put back, one error" ) {
    for ( const bool defaults : { false, true } ) {
        Bench b;
        b.tell( "SYST:PINS:STAG REL1,16;:SYST:PINS:STAG SDA,18" );
        b.world().faults.failIpc = true;
        b.tell( defaults ? "SYST:PINS:DEF" : "SYST:PINS:APPL", kApplyUs );
        b.world().faults.failIpc = false;
        CHECK_EQ( queued( b ), "1" );
        CHECK_CONTAINS( b.errors(),
                        "117,\"Pin map reverted;the map failed its proof (core): the "
                        "last good map is back; it fails too" );
        const std::string result = b.ask( "SYST:PINS:RES?" );
        CHECK_EQ( result.substr( 0, 43 ), "result=reverted reason=core mfg=- die=- tst" );
        CHECK_EQ( field( result, "saved" ), "0" );
        CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
        CHECK_EQ( b.ask( "*TST?", 2'000'000 ), "0" );
        checkRig( b );
    }
}

TEST( "pins_apply", "a revert whose own proof fails: no INA228, and the console answers" ) {
    Bench b;
    b.tell( "SYST:PINS:STAG SDA,18" );
    b.world().ina.present = false;  // its supply is gone under the apply
    b.tell( "SYST:PINS:APPL", kApplyUs );
    CHECK_EQ( queued( b ), "1" );
    CHECK_EQ( b.errors(),
              "117,\"Pin map reverted;the INA228 did not answer on sda=18 scl=5 "
              "addr=auto: the last good map is back; it fails too (tst=1)\"" );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
              "result=reverted reason=no_chip mfg=- die=- tst=1 saved=0" );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "chip" ), "none" );
    CHECK_EQ( b.ask( "*IDN?" ).substr( 0, 21 ), "KLEIDOS,KLEIDOS-METER" );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
    b.tell( "MEAS:CURR?" );
    CHECK_EQ( b.errors(), "-241,\"Hardware missing;no device (scan found none)\"" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    // The part is back: applying the live map again finds it.
    b.world().ina.present = true;
    apply( b, "SYST:PINS:CLE" );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "*TST?", 2'000'000 ), "0" );
}

TEST( "pins_apply", "a board whose boot found no INA228 is proved by an apply" ) {
    // The boot's map is right and the part's SDA is elsewhere.
    Bench b( []( sim::World& w ) { w.wires.sda = 18; } );
    CHECK_CONTAINS( b.errors(), "-241," );
    apply( b, "SYST:PINS:STAG SDA,18" );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ), "result=ok reason=- mfg=0x5449 die=0x228 tst=0 saved=1" );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "chip" ), "ina228" );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "src" ), "stored" );
}

TEST( "pins_apply", "a fallback's live map, applied, becomes the record" ) {
    Bench b( []( sim::World& ) {
        const PinMap silent = pinmap::withPin( kDefaultMap, Role::Sda, 18 );
        REQUIRE( pinmap_store::save( silent ) == pinmap_store::Saved::Ok );
    } );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "src" ), "fallback" );
    apply( b, "" );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
    CHECK( b.pins().bus == pin_boot::Bus::Sound && b.pins().stored == kDefaultMap );
    Bench next( withFlashOf( b ) );
    CHECK( next.clean() );
    CHECK_EQ( next.ask( "SYST:PINS?" ), kDefaultReport );
}

// --- SYSTem:PINS:APPLy: refused -----------------------------------------------------------

TEST( "pins_apply", "refused under a running window, an open one, and each with its own error" ) {
    Bench b( []( sim::World& w ) {
        w.circuit.dutNa = 5'000'000;
        w.ringSamples   = 4096;
    } );
    b.tell( "SYST:PINS:CLOC 100000;:SYST:COMM:I2C:FREQ 1000000" );
    for ( const std::string order : { "SYST:PINS:APPL", "SYST:PINS:DEF" } ) {
        b.tell( "SENS:PROF STATE;:INIT", 100'000 );
        CHECK_EQ( only( b, order ), "-221,\"Settings conflict;capture running (ABORt first)\"" );
        b.tell( "ABOR", 200'000 );
        b.tell( "CAL:ZERO 10", 1'000'000 );
        CHECK_EQ( only( b, order ),
                  "112,\"Null window running;null window running (ABORt first)\"" );
        b.tell( "ABOR", 200'000 );
        b.tell( "SENS:PROF SLEEP;:CAL:ZERO 1", 2'000'000 );  // ended by itself, not closed
        CHECK_EQ( only( b, order ),
                  "112,\"Null window running;null window ended, not closed (ABORt first)\"" );
        b.tell( "ABOR", 400'000 );
        b.tell( "SENS:PROF TRAC;:INIT", 2'000'000 );  // the ring is full
        CHECK_EQ( field( b.ask( "SYST:STAT?" ), "running" ), "0" );
        CHECK_EQ( only( b, order ),
                  "-221,\"Settings conflict;capture ended, not closed (ABORt first)\"" );
        b.tell( "ABOR", 400'000 );
        b.tell( "*CLS" );
        CHECK_EQ( b.ask( "SYST:PINS:RES?" ), "result=none reason=- mfg=- die=- tst=- saved=0" );
    }
    CHECK( pinmap_store::load().found == pinmap_store::Found::Empty );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
}

TEST( "pins_apply", "refused with a relay energized or a null path held" ) {
    Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
    for ( const std::string order : { "SYST:PINS:APPL", "SYST:PINS:DEF" } ) {
        b.tell( "ROUT:NULL ON", 500'000 );
        CHECK_EQ( only( b, order ),
                  "-221,\"Settings conflict;a null path is held (path=bypass): "
                  "ROUTe:NULLpath OFF first\"" );
        CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
                  "result=refused reason=relays mfg=- die=- tst=- saved=0" );
        b.tell( "ROUT:NULL OFF", 500'000 );
        b.tell( "ROUT:BYP ON", 500'000 );
        CHECK_EQ( only( b, order ),
                  "-221,\"Settings conflict;the relays are not at rest "
                  "(path=both): put them back first\"" );
        b.tell( "ROUT:BYP OFF;:ROUT:LEAD OPEN,FORC", 500'000 );
        CHECK_EQ( only( b, order ),
                  "-221,\"Settings conflict;the relays are not at rest "
                  "(path=none): put them back first\"" );
        b.tell( "ROUT:LEAD CLOS", 500'000 );
        b.tell( "*CLS" );
    }
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
    CHECK( b.world().flash.changes == 0U );
}

TEST( "pins_apply", "refused for a staged map that breaks a rule" ) {
    Bench b;
    b.tell( "SYST:PINS:STAG SDA,45" );
    CHECK_EQ( only( b, "SYST:PINS:APPL" ),
              "-221,\"Settings conflict;the staged map breaks a rule: reserved at sda "
              "(SYSTem:PINS:PENDing?)\"" );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
              "result=refused reason=invalid mfg=- die=- tst=- saved=0" );
    b.tell( "SYST:PINS:CLE;:SYST:PINS:CLOC 1" );
    CHECK_EQ( only( b, "SYST:PINS:APPL" ),
              "-221,\"Settings conflict;the staged map breaks a rule: clock at scl_hz "
              "(SYSTem:PINS:PENDing?)\"" );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
}

// --- The power cut ------------------------------------------------------------------------

TEST( "pins_apply", "a power cut at every byte of the save leaves the old record" ) {
    constexpr std::size_t kRecord = 16U + pinmap_store::kBlobBytes;
    for ( std::size_t keep = 0; keep <= kRecord; ++keep ) {
        const bool tears = ( keep % 2U ) == 1U;
        Bench      b;
        apply( b, "SYST:PINS:CLOC 200000" );
        CHECK_EQ( field( b.ask( "SYST:PINS:RES?" ), "saved" ), "1" );
        sim::Flash& flash = b.world().flash;
        flash.cutAt       = static_cast<long long>( flash.changes );
        flash.cutKeep     = keep;
        flash.cutTears    = tears;
        apply( b, "SYST:PINS:CLOC 100000" );
        flash.powerOn();
        Bench next( withFlashOf( b ) );
        // The last byte landed: the new record is whole, and it is the one.
        const std::string clock = keep == kRecord ? "100000" : "200000";
        CHECK_EQ( field( next.ask( "SYST:PINS?" ), "scl_hz" ), clock );
        CHECK_EQ( field( next.ask( "SYST:PINS?" ), "src" ), "stored" );
        CHECK( next.clean() );
    }
}

// --- The relays ---------------------------------------------------------------------------

TEST( "pins_apply", "a relay moves latch first, its old pin parked toward its released level" ) {
    Bench       b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
    sim::World& w = b.world();
    // The rewired board: Relay 1's input is on GPIO16 now.
    w.pads[16].latch = 0;
    apply( b, "SYST:PINS:STAG REL1,16" );
    CHECK( b.clean() );
    CHECK( w.pads[16].driving() && w.pads[16].firstDriven == 1 && w.pads[16].level() == 1 );
    CHECK( parkedAt( w.pads[7], true ) );  // active low: released is high
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "0" );
    std::string route = b.ask( "ROUT:STAT?" );
    CHECK_EQ( field( route, "check" ), "-" );
    CHECK_EQ( field( route, "pads" ), "ok" );
    CHECK_EQ( field( route, "relay1_gpio" ), "16" );
    CHECK_EQ( field( route, "path" ), "shunt" );
    // Wired there, the null path forms and verifies it.
    w.wires.lead = 16;
    b.tell( "ROUT:NULL ON", 500'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "check" ), "ok" );
    CHECK( w.leadOpen() && w.bypassClosed() );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "1" );
    b.tell( "ROUT:NULL OFF", 500'000 );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "1" );
    // An apply that leaves the relays where they are leaves them verified.
    apply( b, "SYST:PINS:CLOC 100000" );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "1" );
    // Relay 2 moved, active low: its old pin, active high, is parked low; and
    // GPIO7 is still parked toward Relay 1's released level.
    w.wires.bypass       = 17;
    w.circuit.bypassPart = sim::BypassPart::Hw316;
    w.pads[17].latch     = 0;
    apply( b, "SYST:PINS:STAG REL2,17,LOW" );
    CHECK( b.clean() );
    CHECK( w.pads[17].firstDriven == 1 && parkedAt( w.pads[15], false ) );
    CHECK( parkedAt( w.pads[7], true ) );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "0" );
    CHECK( w.circuit.disconnects == 0 );
}

TEST( "pins_apply", "back on the rig's own pins, the relays are verified by a check again" ) {
    Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
    apply( b, "SYST:PINS:STAG REL1,16" );
    b.world().wires.lead = 7;
    b.tell( "SYST:PINS:DEF", kApplyUs );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultKeys + " src=default verified=0 table=0x90072FE9" );
    CHECK( parkedAt( b.world().pads[16], true ) );
    checkRig( b );
    b.tell( "ROUT:NULL ON;:ROUT:NULL OFF", 1'000'000 );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
}

TEST( "pins_apply", "a null path that fails its check, or sees no current, verifies nothing" ) {
    // Relay 2 is told to be where nothing is: the bypass takes no current.
    Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
    apply( b, "SYST:PINS:STAG REL2,16" );
    b.tell( "ROUT:NULL ON", 500'000 );
    CHECK_CONTAINS( b.errors(), "103," );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "0" );
    b.tell( "SYST:PINS:DEF", kApplyUs );
    // With nothing drawn there is nothing to judge by.
    b.world().circuit.dutNa = 0;
    b.tell( "ROUT:NULL ON", 500'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "check" ), "unverified" );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "0" );
    b.tell( "ROUT:NULL OFF", 500'000 );
    b.world().circuit.dutNa = 5'000'000;
    b.tell( "ROUT:NULL ON;:ROUT:NULL OFF", 1'000'000 );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
    // A check that fails on verified relays takes it back.
    b.world().wires.bypass = 16;
    b.tell( "ROUT:NULL ON", 500'000 );
    CHECK_CONTAINS( b.errors(), "103," );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "0" );
}

TEST( "pins_apply",
      "a relay's level flipped on its own wire: the DUT at rest proves it, reverted" ) {
    // Relay 1 told active high on GPIO7, where the active-low part is: at
    // rest the pad is driven low, which energizes it and opens the lead.
    {
        Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
        apply( b, "SYST:PINS:STAG REL1,7,HIGH" );
        CHECK_EQ( queued( b ), "1" );
        const std::string error = b.errors();
        CHECK_CONTAINS( error, "117," );
        CHECK_CONTAINS( error, "the relays at rest took the dut's current off the shunt (vs_pv=" );
        CHECK_CONTAINS( error, ": the last good map is back" );
        CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
                  "result=reverted reason=rest mfg=0x5449 die=0x228 tst=0 saved=0" );
        CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
        CHECK( pinmap_store::load().found == pinmap_store::Found::Empty );
        // The lead opened once, while the map was tried, and is closed again.
        CHECK( b.world().dutFed() && !b.world().leadOpen() );
        CHECK( b.world().circuit.disconnects == 1 );
        CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
        CHECK( b.clean() );
    }
    // Relay 2 told active low on GPIO15, where the active-high Click is: at
    // rest it closes the bypass, and the shunt keeps only part of the share.
    {
        Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
        apply( b, "SYST:PINS:STAG REL2,15,LOW" );
        CHECK_EQ( queued( b ), "1" );
        CHECK_CONTAINS( b.errors(), "117," );
        CHECK_EQ( field( b.ask( "SYST:PINS:RES?" ), "reason" ), "rest" );
        CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
        CHECK( !b.world().bypassClosed() && b.world().circuit.disconnects == 0 );
        CHECK( pinmap_store::load().found == pinmap_store::Found::Empty );
    }
    // A relay moved to a pin with no wire rests released all the same: the
    // share stays, the map is kept, and only a null path can verify it.
    {
        Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
        apply( b, "SYST:PINS:STAG REL1,16" );
        CHECK( b.clean() );
        CHECK_EQ( field( b.ask( "SYST:PINS:RES?" ), "result" ), "ok" );
        CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "0" );
    }
}

TEST( "pins_apply", "a flipped level under the 2 mA floor cannot be judged, and is kept" ) {
    // What docs/scpi.md says of a quiet DUT: the map is saved, the lead stays
    // open at rest, and SYSTem:PINS:DEFault is the way back.
    Bench b( []( sim::World& w ) { w.circuit.dutNa = 42'000; } );
    apply( b, "SYST:PINS:STAG REL1,7,HIGH" );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ), "result=ok reason=- mfg=0x5449 die=0x228 tst=0 saved=1" );
    CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "0" );
    CHECK( b.world().leadOpen() && !b.world().dutFed() );
    b.tell( "SYST:PINS:DEF", kApplyUs );
    CHECK( b.clean() );
    CHECK( b.world().dutFed() && !b.world().leadOpen() );
}

TEST( "pins_apply", "with no INA228 a relay move is not judged by the shunt" ) {
    Bench b( []( sim::World& w ) {
        w.circuit.dutNa = 5'000'000;
        w.ina.present   = false;
    } );
    apply( b, "SYST:PINS:STAG REL1,7,HIGH" );
    CHECK_EQ( field( b.ask( "SYST:PINS:RES?" ), "reason" ), "no_chip" );
}

TEST( "pins_apply", "a rest check whose reading fails puts the map back" ) {
    // A reading that fails after the move is a share of nothing.
    constexpr int kVShunt = 0x04;
    Bench         b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
    sim::World&   w     = b.world();
    int           reads = 0;
    w.i2c.onStart       = [&w, &reads]() {
        // Once the move drives Relay 1's new pin, every read of VSHUNT fails:
        // the proof reads none, so the one it meets is the rest check's.
        if ( w.pads[16].driving() && reads++ == 0 ) {
            w.ina.failRegister = kVShunt;
            w.ina.failCount    = 1000;
        }
    };
    apply( b, "SYST:PINS:STAG REL1,16" );
    w.i2c.onStart      = nullptr;
    w.ina.failCount    = 0;
    w.ina.failRegister = -1;
    CHECK_EQ( queued( b ), "1" );
    CHECK_CONTAINS( b.errors(), "the relays at rest took the dut's current off the shunt" );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
              "result=reverted reason=rest mfg=0x5449 die=0x228 tst=0 saved=0" );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
}

TEST( "pins_apply", "a bus closed by a move forgets its pinned address" ) {
    // A map pinned to 0x41 runs; then the defaults, whose scan finds nothing:
    // the report names the default address, not the old map's.
    Bench       b;
    sim::World& w = b.world();
    w.ina.address = 0x41;
    apply( b, "SYST:PINS:ADDR #H41" );
    CHECK( b.clean() );
    std::string info = b.ask( "SYST:INF?" );
    CHECK_EQ( field( info, "addr" ), "0x41" );
    CHECK_EQ( field( info, "chip" ), "ina228" );
    w.ina.present = false;
    b.tell( "SYST:PINS:DEF", kApplyUs );
    CHECK_EQ( field( b.ask( "SYST:PINS:RES?" ), "reason" ), "no_chip" );
    info = b.ask( "SYST:INF?" );
    CHECK_EQ( field( info, "addr" ), "0x40" );
    CHECK_EQ( field( info, "chip" ), "none" );
}

TEST( "pins_apply", "relays swapped onto each other's wires, levels and all, fail the null path" ) {
    // Relay 1 on GPIO15 active high and Relay 2 on GPIO7 active low: each part
    // rests released, so the apply keeps the map; the null path's first step
    // then opens the lead, which the shunt alone tells from a closed bypass
    // only by the share it leaves, nothing at all.
    const auto swapped = []( Bench& b ) {
        apply( b, "SYST:PINS:STAG REL1,15,HIGH;:SYST:PINS:STAG REL2,7,LOW" );
        CHECK( b.clean() );
        CHECK_EQ( field( b.ask( "SYST:PINS:RES?" ), "result" ), "ok" );
        CHECK( b.world().dutFed() && b.world().circuit.disconnects == 0 );
        b.tell( "ROUT:NULL ON", 500'000 );
        const std::string error = b.errors();
        CHECK_CONTAINS( error, "103," );
        CHECK_CONTAINS( error, "bypass check failed (the shunt lost all of the dut's current" );
        CHECK_EQ( field( b.ask( "SYST:PINS?" ), "verified" ), "0" );
        CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
        CHECK( b.world().dutFed() );
    };
    {
        Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
        swapped( b );
    }
    // With a null stored the shunt reads nothing at all once the lead opens.
    Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
    b.tell( "ROUT:NULL ON", 500'000 );
    b.tell( "SENS:PROF SLEEP;:CAL:ZERO 2", 800'000 );
    b.world().run( 2'500'000 );
    b.tell( "ABOR", 300'000 );
    b.tell( "ROUT:NULL OFF", 500'000 );
    CHECK( b.clean() );
    CHECK_EQ( field( b.ask( "CAL:DATA?" ), "nulled_r0" ), "1" );
    swapped( b );
}

TEST( "pins_apply", "two roles that change places, and a pin that changes roles" ) {
    // Relay 1 takes SDA's pin and SDA Relay 1's; the board is wired so.
    Bench       b;
    sim::World& w = b.world();
    w.wires.sda   = 7;
    w.wires.lead  = 4;
    apply( b, "SYST:PINS:STAG SDA,7;:SYST:PINS:STAG REL1,4" );
    CHECK( b.clean() );
    CHECK( carries( b, 7, I2CEXT0_SDA_OUT_IDX ) && w.pads[7].pullUp );
    CHECK( w.pads[4].driving() && w.pads[4].outSignal == sim::kSigGpioOut );
    CHECK( !w.pads[4].openDrain && w.pads[4].level() == 1 );
    CHECK( !w.leadOpen() );
    // ALERT on SCL's pin and SCL on ALERT's.
    w.wires.scl   = 6;
    w.wires.alert = 5;
    apply( b, "SYST:PINS:STAG SCL,6;:SYST:PINS:STAG ALER,5" );
    CHECK( b.clean() );
    CHECK( carries( b, 6, I2CEXT0_SCL_OUT_IDX ) );
    CHECK( w.pads[5].input && !w.pads[5].oneByPeripheral );
    CHECK( w.gpioIntrType.at( 6 ) == GPIO_INTR_DISABLE );
    CHECK( w.gpioIntrType.at( 5 ) == GPIO_INTR_NEGEDGE );
    CHECK_EQ( b.ask( "*TST?", 2'000'000 ), "0" );
}

// --- SYSTem:PINS:DEFault ------------------------------------------------------------------

TEST( "pins_apply", "DEFault brings the rig back from any map, with an INA228 or without" ) {
    for ( const bool present : { true, false } ) {
        Bench b( [present]( sim::World& w ) {
            PinMap map = pinmap::withPin( kDefaultMap, Role::Sda, 18 );
            map        = pinmap::withPin( map, Role::Scl, 21 );
            map        = pinmap::withPin( map, Role::Relay2, 4 );  // no fallback
            REQUIRE( pinmap_store::save( map ) == pinmap_store::Saved::Ok );
            w.wires.bypass = 4;
            w.ina.present  = present;
        } );
        CHECK_CONTAINS( b.errors(), "117," );
        CHECK_EQ( field( b.ask( "SYST:INF?" ), "chip" ), "none" );
        b.world().wires.bypass = 15;
        b.tell( "SYST:PINS:DEF", kApplyUs );
        CHECK( b.clean() );
        CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
                  present ? "result=ok reason=- mfg=0x5449 die=0x228 tst=0 saved=1"
                          : "result=ok reason=no_chip mfg=- die=- tst=1 saved=1" );
        CHECK_EQ( b.ask( "SYST:PINS?" ),
                  kDefaultKeys + " src=default verified=0 table=0x90072FE9" );
        CHECK_EQ( b.ask( "SYST:PINS:PEND?" ), pending( kDefaultKeys, "valid=1 problem=- at=-" ) );
        CHECK_EQ( field( b.ask( "SYST:INF?" ), "chip" ), present ? "ina228" : "none" );
        checkRig( b );
        for ( const std::size_t gpio : { 18U, 21U } ) {
            CHECK( parkedAt( b.world().pads[gpio], false ) );
        }
        if ( !present ) {
            b.tell( "MEAS:CURR?" );
            CHECK_CONTAINS( b.errors(), "no device (scan found none)" );
        }
        Bench next( withFlashOf( b ) );
        CHECK( next.clean() );
        CHECK_EQ( next.ask( "SYST:PINS?" ), kDefaultReport );
    }
}

TEST( "pins_apply", "DEFault is kept when its ALERT wire fails, and when the flash does" ) {
    Bench b;
    apply( b, "SYST:PINS:CLOC 100000" );
    b.world().ina.alertPin = sim::Ina228Model::AlertPin::Absent;
    b.tell( "SYST:PINS:DEF", kApplyUs );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
              "result=ok reason=alert mfg=0x5449 die=0x228 tst=32 saved=1" );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
    b.world().ina.alertPin = sim::Ina228Model::AlertPin::HeldUntilRead;

    apply( b, "SYST:PINS:CLOC 100000" );
    b.world().flash.failProgram = true;
    b.tell( "SYST:PINS:DEF", kApplyUs );
    b.world().flash.failProgram = false;
    CHECK_EQ( queued( b ), "1" );
    CHECK_EQ( b.errors(),
              "-240,\"Hardware error;the default map is live and was not saved (a "
              "reboot loads the stored one)\"" );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
              "result=ok reason=save mfg=0x5449 die=0x228 tst=0 saved=0" );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "400000" );
    Bench next( withFlashOf( b ) );
    CHECK_EQ( field( next.ask( "SYST:PINS?" ), "scl_hz" ), "100000" );
}

TEST( "pins_apply", "an engine the boot did not start stays down, and DEFault still works" ) {
    Bench b( []( sim::World& w ) {
        w.faults.failTask     = true;
        w.faults.failTaskName = "meter-sample";
    } );
    CHECK( !b.samplerReady() );
    b.tell( "*CLS;:SYST:PINS:DEF", kApplyUs );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ),
              "result=ok reason=engine mfg=0x5449 die=0x228 tst=2 saved=1" );
    CHECK_EQ( b.ask( "SYST:PINS?" ), kDefaultReport );
}

// --- Where it runs ------------------------------------------------------------------------

TEST( "pins_apply", "with the bus on the console's core, the move is made in place" ) {
    Bench b( []( sim::World& w ) { w.faults.failIpc = true; } );
    CHECK( b.world().i2c.core == 0 );
    b.world().faults.failIpc = false;  // the sampler's transactions cross from Core 1
    apply( b, "SYST:PINS:CLOC 100000" );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "SYST:PINS:RES?" ), "result=ok reason=- mfg=0x5449 die=0x228 tst=0 saved=1" );
    CHECK( b.world().i2c.core == 0 );
}

TEST( "pins_apply", "an apply, a revert and DEFault allocate nothing" ) {
    Bench b;
    apply( b, "SYST:PINS:CLOC 100000" );
    apply( b, "SYST:PINS:STAG SDA,18" );
    b.tell( "SYST:PINS:DEF", kApplyUs );
    const std::string heap = b.ask( "DIAG:HEAP?" );
    CHECK_EQ( field( heap, "allocs_since_main" ), "0" );
    CHECK_EQ( field( heap, "frees_since_main" ), "0" );
}

TEST( "pins_apply", "ALERT moves to a pin its status register holds, and no other" ) {
    Bench b;
    b.sampler().moveAlert( GPIO_NUM_39, true );
    CHECK( !b.sampler().engineReady() );
    CHECK( b.world().gpioIntrType.at( 6 ) == GPIO_INTR_DISABLE );
    b.sampler().moveAlert( GPIO_NUM_6, true );
    CHECK( b.sampler().engineReady() );
    CHECK( b.world().gpioIntrType.at( 6 ) == GPIO_INTR_NEGEDGE );
    CHECK_EQ( b.ask( "*TST?", 2'000'000 ), "0" );
}
