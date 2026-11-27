// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_console_route.cpp
 * @brief ROUTe: every order and interlock, the null-path checks against a
 *        circuit whose contacts work or do not, LIVE marks and the hold.
 */
#include "bench.h"
#include "unit.h"

#include <string>

using bench::Bench;
using bench::field;

namespace {

void drawing5mA( sim::World& w ) {
    w.circuit.dutNa = 5'000'000;
}

}  // namespace

TEST( "route", "the state at rest, and orders that change nothing" ) {
    Bench             b;
    const std::string state = b.ask( "ROUT:STAT?" );
    CHECK_EQ( field( state, "path" ), "shunt" );
    CHECK_EQ( field( state, "pads" ), "ok" );
    CHECK_EQ( field( state, "hold_s" ), "-" );
    CHECK_EQ( field( state, "operate_settle_ms" ), "20" );
    CHECK_EQ( field( state, "bypass_part" ), "relay2click" );
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "moved" ), "-" );
    b.tell( "ROUT:LEAD CLOS;:ROUT:BYP OFF;:ROUT:NULL OFF" );
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "moved" ), "-" );
    CHECK( b.clean() );
    b.world().pads[7].forced = 0;
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "pads" ), "k1" );
}

TEST( "route", "the lead: refused into none, forced, closed again" ) {
    Bench b;
    b.tell( "ROUT:LEAD OPEN" );
    CHECK_CONTAINS( b.errors(), "109," );
    CHECK( !b.world().leadOpen() );
    b.tell( "ROUT:LEAD OPEN,FORC", 100'000 );
    CHECK( b.world().leadOpen() );
    CHECK( b.world().circuit.disconnects == 1 );
    const std::string result = b.ask( "ROUT:RES?" );
    CHECK_EQ( field( result, "moved" ), "k1" );
    CHECK_EQ( field( result, "path" ), "none" );
    b.tell( "ROUT:LEAD CLOS", 100'000 );
    CHECK( !b.world().leadOpen() );
    b.tell( "ROUT:LEAD AJAR;:ROUT:LEAD OPEN,NOW;:ROUT:BYP MAYBE" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
}

TEST( "route", "the null path: seen, both ways make-before-break, never unpowering" ) {
    Bench             b( drawing5mA );
    const std::string on = b.ask( "ROUT:NULL ON;:ROUT:RES?", 400'000 );
    CHECK_EQ( field( on, "path" ), "bypass" );
    CHECK_EQ( field( on, "check" ), "ok" );
    CHECK_EQ( field( on, "moved" ), "k2,k1" );
    CHECK( field( on, "vs_pv" ).find( ',' ) != std::string::npos );
    CHECK( field( on, "hold_s" ) != "-" );
    CHECK_EQ( field( on, "q" ), "invalid" );
    b.tell( "ROUT:NULL OFF", 300'000 );
    const std::string off = b.ask( "ROUT:RES?" );
    CHECK_EQ( field( off, "moved" ), "k1,k2" );
    CHECK_EQ( field( off, "check" ), "-" );
    CHECK( b.world().circuit.disconnects == 0 );
    CHECK( b.clean() );
}

TEST( "route", "the null path under 2 mA is unverified, and with no INA228 skipped" ) {
    {
        Bench b( []( sim::World& w ) { w.circuit.dutNa = 500'000; } );
        CHECK_EQ( field( b.ask( "ROUT:NULL ON;:ROUT:STAT?", 400'000 ), "check" ), "unverified" );
        b.tell( "ROUT:NULL OFF", 300'000 );
    }
    {
        // A stored null is the reference the check judges against.
        Bench b( drawing5mA );
        b.calibration().setOffset( kleidos::meter::ina228::ShuntRange::Wide163mV, 480'000, 25'000 );
        CHECK_EQ( field( b.ask( "ROUT:NULL ON;:ROUT:STAT?", 400'000 ), "check" ), "ok" );
        b.tell( "ROUT:NULL OFF", 300'000 );
    }
    Bench b( []( sim::World& w ) { w.ina.present = false; } );
    CHECK_EQ( field( b.ask( "ROUT:NULL ON;:ROUT:STAT?", 400'000 ), "check" ), "skipped" );
    b.tell( "ROUT:NULL OFF", 300'000 );
}

TEST( "route", "a bypass that does not close is refused and undone" ) {
    Bench b( []( sim::World& w ) {
        drawing5mA( w );
        w.circuit.bypassStuckOpen = true;
    } );
    b.tell( "ROUT:NULL ON", 400'000 );
    CHECK_CONTAINS( b.errors(), "103,\"Bypass check failed" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK( b.world().circuit.disconnects == 0 );
}

TEST( "route", "a lead that does not open is refused and undone" ) {
    Bench b( []( sim::World& w ) {
        drawing5mA( w );
        w.circuit.leadStuckClosed = true;
    } );
    b.tell( "ROUT:NULL ON", 400'000 );
    CHECK_CONTAINS( b.errors(), "104,\"Lead check failed" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    // Under the residual limit as well (40 uV left, more than the sixteenth a
    // closed bypass must leave): K1's step changed nothing.
    b.world().circuit.bypassSharePercent = 92;
    b.tell( "ROUT:NULL ON", 400'000 );
    CHECK_CONTAINS( b.errors(), "104,\"Lead check failed" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
}

TEST( "route", "a lead already open that still conducts is refused whatever K2 then takes" ) {
    // K1 is open before the order, so the order moves K2 alone and no step
    // shows K1 leave the shunt dead. The DUT's current through the shunt
    // before anything moves says it did not open, even when K2 then takes so
    // much of it that what is left is under the residual limit (5 uV of 500).
    Bench b( []( sim::World& w ) {
        drawing5mA( w );
        w.circuit.leadStuckClosed    = true;
        w.circuit.bypassSharePercent = 99;
    } );
    b.tell( "ROUT:LEAD OPEN,FORC", 100'000 );
    b.tell( "ROUT:NULL ON", 400'000 );
    CHECK_CONTAINS( b.errors(), "104,\"Lead check failed" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    // A lead that did open leaves nothing to judge, and the path forms.
    b.world().circuit.leadStuckClosed = false;
    b.tell( "ROUT:LEAD OPEN,FORC", 100'000 );
    b.tell( "ROUT:NULL ON", 400'000 );
    const std::string state = b.ask( "ROUT:STAT?" );
    CHECK_EQ( field( state, "path" ), "bypass" );
    CHECK_EQ( field( state, "check" ), "unverified" );
    CHECK( b.clean() );
}

TEST( "route", "a check whose reading fails moves nothing, or releases" ) {
    Bench b( drawing5mA );
    b.world().ina.failCount = 1;
    b.tell( "ROUT:NULL ON", 400'000 );
    CHECK_CONTAINS( b.errors(), "nothing moved" );
    CHECK( !b.world().bypassClosed() );
    b.world().ina.failAfter = 3;  // the first reading passes, the second fails
    b.tell( "ROUT:NULL ON", 400'000 );
    b.world().ina.failAfter = -1;
    CHECK_CONTAINS( b.errors(), "released to path=shunt" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    // The restore of the profile's word fails after a good reading.
    b.world().ina.failRegister = kleidos::meter::ina228::kRegAdcConfig;
    b.world().ina.failAfter    = -1;
    b.world().ina.failCount    = 2;
    b.tell( "ROUT:NULL ON", 400'000 );
    b.world().ina.failRegister = -1;
    CHECK_CONTAINS( b.errors(), "-240," );
}

TEST( "route", "the hold releases an idle null path after 120 s, re-armed by a window's close" ) {
    Bench b( drawing5mA );
    b.tell( "ROUT:NULL ON", 400'000 );
    b.tell( "INIT:FORC", 50'000 );
    b.world().run( 100'000'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "bypass" );
    b.tell( "ABOR", 50'000 );
    CHECK( std::stoi( field( b.ask( "ROUT:STAT?" ), "hold_s" ) ) > 100 );
    b.world().run( 121'000'000 );
    const std::string state = b.ask( "ROUT:STAT?" );
    CHECK_EQ( field( state, "path" ), "shunt" );
    CHECK_EQ( field( state, "hold_released" ), "1" );
    CHECK_CONTAINS( b.errors(),
                    "113,\"Null path released;the hold released the null path after 120 s "
                    "(ROUTe:NULLpath ON again)" );
    CHECK( b.world().circuit.disconnects == 0 );
}

TEST( "route", "a bypass left across a closed lead is held too, and opened with error 115" ) {
    Bench b( drawing5mA );
    b.tell( "ROUT:NULL ON", 400'000 );
    b.tell( "ROUT:LEAD CLOS", 200'000 );  // into both: still off the shunt's whole current
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "both" );
    CHECK( std::stoi( field( b.ask( "ROUT:STAT?" ), "hold_s" ) ) > 100 );
    b.tell( "ROUT:BYP OFF", 200'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "hold_s" ), "-" );
    b.tell( "ROUT:BYP ON", 200'000 );
    b.world().run( 121'000'000 );
    const std::string state = b.ask( "ROUT:STAT?" );
    CHECK_EQ( field( state, "path" ), "shunt" );
    CHECK_EQ( field( state, "hold_released" ), "1" );
    CHECK_CONTAINS( b.errors(),
                    "115,\"Bypass opened;the hold opened the bypass after 120 s "
                    "(ROUTe:BYPass ON again)" );
    CHECK( b.world().circuit.disconnects == 0 );
}

TEST( "route", "the hold recloses an open lead under a running capture, and marks it" ) {
    // A ring that outlasts the hold: TRACE fills 2^20 samples in 293 s.
    Bench b( []( sim::World& w ) {
        drawing5mA( w );
        w.ringSamples = std::size_t{ 1 } << 20;
    } );
    b.tell( "ROUT:LEAD OPEN,FORC", 200'000 );
    b.tell( "INIT", 50'000 );
    // The capture's own marks fill TRACe:MARK's table: the hold's still fit.
    for ( int i = 0; i < 16; ++i ) {
        b.tell( "TRAC:MARK \"m\"", 1'000 );
    }
    b.tell( "TRAC:MARK \"m\"", 1'000 );
    CHECK_CONTAINS( b.errors(), "105,\"Marks full" );
    (void)b.drain();
    b.world().run( 100'000'000 );
    CHECK( !b.world().dutFed() );
    b.world().run( 21'000'000 );
    CHECK( b.world().dutFed() );
    CHECK( b.world().circuit.disconnects == 1 );
    const std::string marks = b.drain();
    CHECK_CONTAINS( marks, "METER_MARK mark=hold:k1:closed n=" );
    const std::string state = b.ask( "ROUT:STAT?" );
    CHECK_EQ( field( state, "path" ), "shunt" );
    CHECK_EQ( field( state, "hold_s" ), "-" );
    CHECK_EQ( field( state, "hold_released" ), "1" );
    CHECK_CONTAINS( b.errors(),
                    "114,\"Lead reclosed;the hold reclosed the lead after 120 s under a capture, "
                    "marked hold: (ROUTe:LEAD OPEN again)" );
    // The capture says so while it runs, after its close, and in its dump.
    CHECK( ( std::stoi( b.ask( "STAT:QUES:COND?" ) ) & 1024 ) != 0 );
    b.tell( "ABOR", 100'000 );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "hold_teardowns" ), "1" );
    CHECK( ( std::stoi( b.ask( "STAT:QUES:COND?" ) ) & 1024 ) != 0 );
    const std::size_t at = marks.find( "hold:k1:closed n=" ) + 17;
    const long        n  = std::stol( marks.substr( at ) );
    b.tell( "TRAC:DATA? RAW," + std::to_string( n - 4 ) + ",8", 200'000 );
    CHECK_CONTAINS( b.drain(), "K n=" + std::to_string( n ) + " label=hold:k1:closed" );
    // The next capture starts clean.
    b.tell( "INIT", 50'000 );
    CHECK( ( std::stoi( b.ask( "STAT:QUES:COND?" ) ) & 1024 ) == 0 );
    b.tell( "ABOR", 100'000 );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "hold_teardowns" ), "0" );
}

TEST( "route", "the hold releases the null path under a streamed capture, make-before-break" ) {
    Bench b( drawing5mA );
    b.tell( "ROUT:NULL ON", 400'000 );
    b.tell( "SENS:PROF SLEEP;:INIT:FORC", 50'000 );
    b.world().run( 121'000'000 );
    const std::string out = b.drain();
    CHECK_CONTAINS( out, "METER_MARK mark=hold:k1:closed" );
    CHECK_CONTAINS( out, "METER_MARK mark=hold:k2:open" );
    CHECK( out.find( "hold:k1:closed" ) < out.find( "hold:k2:open" ) );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK( b.world().circuit.disconnects == 0 );
    CHECK_CONTAINS( b.errors(),
                    "113,\"Null path released;the hold released the null path after "
                    "120 s under a capture, marked hold:" );
    b.tell( "ABOR", 100'000 );
    const std::string stats = b.ask( "FETC:STAT?" );
    CHECK_EQ( field( stats, "hold_teardowns" ), "1" );
    CHECK_EQ( field( stats, "q" ), "invalid" );
}

TEST( "route",
      "an order that changes nothing re-arms the hold; a finished capture is not marked" ) {
    Bench b( drawing5mA );
    b.tell( "ROUT:LEAD OPEN,FORC", 200'000 );
    b.tell( "SENS:CAPT:LIM ON;:SAMP:COUN 10;:INIT", 50'000 );
    b.world().run( 100'000'000 );
    // A host still there keeps its open lead by saying so again.
    b.tell( "ROUT:LEAD OPEN,FORC", 50'000 );
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "moved" ), "-" );
    b.world().run( 100'000'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "none" );
    (void)b.drain();
    // The capture ended itself long ago and awaits ABORt: the hold recloses
    // the lead and marks nothing in it.
    b.world().run( 21'000'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK( b.drain().find( "hold:" ) == std::string::npos );
    CHECK_CONTAINS( b.errors(),
                    "114,\"Lead reclosed;the hold reclosed the lead after 120 s "
                    "(ROUTe:LEAD OPEN again)" );
    b.tell( "ABOR", 100'000 );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "hold_teardowns" ), "0" );
}

TEST( "route", "a hold release with every mark slot taken is still counted" ) {
    Bench b( drawing5mA );
    b.tell( "ROUT:NULL ON", 400'000 );
    b.tell( "SENS:PROF SLEEP;:SENS:CAPT STAT;:INIT:FORC", 50'000 );
    for ( int i = 0; i < 16; ++i ) {
        b.tell( "TRAC:MARK \"m\"", 1'000 );
    }
    b.world().run( 121'000'000 );
    CHECK_CONTAINS( b.drain(), "mark=hold:k2:open" );  // the two kept slots
    // A LIVE order finds TRACe:MARK's table full and moves unmarked; the
    // hold's next release finds its own slots taken too.
    b.tell( "ROUT:LEAD OPEN,FORC,LIVE", 100'000 );
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "marked" ), "0" );
    (void)b.drain();
    b.world().run( 121'000'000 );
    CHECK( b.drain().find( "hold:" ) == std::string::npos );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    b.tell( "ABOR", 100'000 );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "hold_teardowns" ), "2" );
    CHECK( b.world().circuit.disconnects == 1 );
}

TEST( "route", "an acquisition holds the console past the hold, which runs right after it" ) {
    Bench b( drawing5mA );
    b.tell( "ROUT:LEAD OPEN,FORC", 200'000 );
    // 1,900 SLEEP samples are 128 s: the hold runs out during the acquisition,
    // which reads nothing but ABORt, so the next unit still finds the lead open.
    const std::string reply =
        b.ask( "SENS:PROF SLEEP;:SAMP:COUN 1900;:MEAS:CURR?;:ROUT:STAT?", 140'000'000 );
    CHECK_EQ( field( reply, "path" ), "none" );
    CHECK_EQ( field( reply, "hold_s" ), "0" );
    b.world().run( 200'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK_CONTAINS( b.errors(),
                    "114,\"Lead reclosed;the hold reclosed the lead after 120 s "
                    "(ROUTe:LEAD OPEN again)" );
}

TEST( "route", "a sampling null window suspends the hold; once it has ended, the hold runs" ) {
    Bench b( drawing5mA );
    b.tell( "ROUT:NULL ON", 400'000 );
    b.tell( "SENS:PROF SLEEP;:CAL:ZERO 200", 50'000 );
    b.world().run( 150'000'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "bypass" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "hold_s" ), "120" );
    // The window ends by itself at 200 s and its whole hold starts there.
    b.world().run( 60'000'000 );
    CHECK_EQ( field( b.ask( "CAL:ZERO:RES?" ), "running" ), "1" );
    CHECK( std::stoi( field( b.ask( "ROUT:STAT?" ), "hold_s" ) ) > 100 );
    b.world().run( 121'000'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK_CONTAINS( b.errors(),
                    "113,\"Null path released;the hold released the null path after "
                    "120 s (ROUTe:NULLpath ON again)" );
    CHECK( b.world().circuit.disconnects == 0 );
    // Its null was taken on the null path, which the result still says.
    b.tell( "ABOR", 300'000 );
    const std::string result = b.ask( "CAL:ZERO:RES?" );
    CHECK_EQ( field( result, "path" ), "bypass" );
    CHECK_EQ( field( result, "stored" ), "1" );
}

TEST( "route", "a reset that released energized coils is reported at the next boot" ) {
    using kleidos::meter::relay::detail::resetRecord;
    kleidos::meter::relay::ResetRecord left = {};
    {
        Bench b( drawing5mA );
        b.tell( "ROUT:NULL ON", 400'000 );
        left = resetRecord();  // what RTC memory holds when the watchdog fires
    }
    Bench b( [&]( sim::World& w ) {
        w.resetReason = 6;
        resetRecord() = left;
    } );
    CHECK_CONTAINS( b.errors(),
                    "116,\"Relays released by reset;a reset (task_wdt) released "
                    "k1,k2 (path=bypass)" );
    CHECK_EQ( field( b.ask( "SYST:HEAL?" ), "reset_released" ), "k1,k2" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK( b.world().dutFed() );
    // A power-on finds noise there, and a warm boot after a clean one nothing.
    Bench cold( [&]( sim::World& ) { resetRecord() = left; } );
    CHECK( cold.clean() );
    CHECK_EQ( field( cold.ask( "SYST:HEAL?" ), "reset_released" ), "-" );
    Bench warm( []( sim::World& w ) { w.resetReason = 4; } );
    CHECK( warm.clean() );
}

TEST( "route", "an open lead is reclosed by the hold, with error 114" ) {
    Bench b( drawing5mA );
    b.tell( "ROUT:LEAD OPEN,FORC", 200'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "none" );
    CHECK( std::stoi( field( b.ask( "ROUT:STAT?" ), "hold_s" ) ) > 100 );
    CHECK( b.clean() );
    b.world().run( 121'000'000 );
    const std::string state = b.ask( "ROUT:STAT?" );
    CHECK_EQ( field( state, "path" ), "shunt" );
    CHECK_EQ( field( state, "hold_released" ), "1" );
    CHECK_CONTAINS( b.errors(), "114,\"Lead reclosed;the hold reclosed the lead after 120 s" );
    CHECK( b.world().circuit.disconnects == 1 );
}

TEST( "route", "a lead cycle unpowers the DUT once, for its time, and recloses itself" ) {
    Bench b( drawing5mA );
    b.tell( "ROUT:LEAD:CYCL 500,FORC", 1'000 );
    const sim::Micros opened = b.world().now();
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "none" );
    b.world().run( 495'000 - ( b.world().now() - opened ) );
    CHECK( !b.world().dutFed() );
    b.world().run( 10'000 );  // within the loop's own work of 500 ms
    CHECK( b.world().dutFed() );
    CHECK( b.world().circuit.disconnects == 1 );
    b.world().run( 100'000 );
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "moved" ), "k1" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK( b.clean() );
    // FORCe is not optional, the time is bounded, the path must be the shunt.
    b.tell( "ROUT:LEAD:CYCL 500" );
    CHECK_CONTAINS( b.errors(), "109," );
    b.tell( "ROUT:LEAD:CYCL 0,FORC;:ROUT:LEAD:CYCL 60001,FORC" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "2" );
    b.tell( "*CLS;:ROUT:LEAD:CYCL x,FORC;:ROUT:LEAD:CYCL 10,FORC,NOW" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "2" );
    b.tell( "*CLS;:ROUT:NULL ON", 400'000 );
    b.tell( "ROUT:LEAD:CYCL 100,FORC" );
    CHECK_CONTAINS( b.errors(), "-221,\"Settings conflict;a lead cycle starts from path=shunt" );
    b.tell( "ROUT:NULL OFF", 400'000 );
    // An explicit order replaces a pending cycle; so does *RST.
    b.tell( "ROUT:LEAD:CYCL 5000,FORC", 100'000 );
    b.tell( "ROUT:LEAD CLOS", 100'000 );
    b.world().run( 6'000'000 );
    CHECK( b.world().circuit.disconnects == 2 );
    b.tell( "ROUT:LEAD:CYCL 5000,FORC;*RST", 500'000 );
    CHECK( b.world().dutFed() );
    b.world().run( 6'000'000 );
    CHECK( b.world().circuit.disconnects == 3 );
    CHECK( b.clean() );
}

TEST( "route", "a null window cannot start under a pending lead cycle" ) {
    // A null window refuses every contact move, so a window opened during a
    // cycle would refuse its reclose and leave the DUT without its battery
    // for the hold's 120 s instead of the cycle's time.
    Bench b( drawing5mA );
    b.tell( "ROUT:LEAD:CYCL 300,FORC", 1'000 );
    b.tell( "CAL:ZERO 2", 1'000 );
    CHECK_CONTAINS( b.errors(), "-221,\"Settings conflict;a lead cycle is pending" );
    b.world().run( 400'000 );
    CHECK( b.world().dutFed() );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK( b.world().circuit.disconnects == 1 );
    // Once the lead is back, a null window starts as before.
    b.tell( "CAL:ZERO 1", 1'500'000 );
    CHECK( b.clean() );
}

TEST( "route", "a lead cycle under a capture needs LIVE and marks both moves" ) {
    Bench b( drawing5mA );
    b.tell( "SENS:PROF STAT;:INIT", 20'000 );
    b.tell( "ROUT:LEAD:CYCL 50,FORC" );
    CHECK_CONTAINS( b.errors(), "-221," );
    b.drain();
    b.tell( "ROUT:LEAD:CYCL 50,FORC,LIVE", 200'000 );
    const std::string out = b.drain();
    CHECK_CONTAINS( out, "METER_MARK mark=k1:open" );
    CHECK_CONTAINS( out, "METER_MARK mark=k1:closed" );
    // A LIVE order that moves nothing, and one that recloses without FORCe.
    b.tell( "ROUT:BYP OFF,LIVE", 50'000 );
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "live" ), "0" );
    b.tell( "ROUT:LEAD OPEN,FORC,LIVE", 100'000 );
    b.tell( "ROUT:LEAD CLOS,LIVE", 100'000 );
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "live" ), "1" );
    b.tell( "ABOR" );
    CHECK( b.clean() );
}

TEST( "route", "orders under a capture: LIVE, FORCe, marks, and the null window" ) {
    Bench b( drawing5mA );
    b.tell( "SENS:PROF STATE;:INIT" );
    b.tell( "ROUT:BYP ON" );
    CHECK_CONTAINS( b.errors(), "-221," );
    b.tell( "ROUT:BYP ON,LIVE", 100'000 );
    CHECK_CONTAINS( b.errors(), "110," );
    b.drain();
    b.tell( "ROUT:BYP ON,LIVE,FORC", 100'000 );
    CHECK_CONTAINS( b.drain(), "METER_MARK mark=k2:closed" );
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "live" ), "1" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "q" ), "invalid" );
    b.tell( "ROUT:LEAD OPEN,LIVE", 100'000 );  // the capture is already invalid
    b.tell( "ROUT:NULL OFF,LIVE", 300'000 );
    b.tell( "ROUT:NULL ON,LIVE", 300'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "check" ), "skipped" );
    b.tell( "ROUT:NULL OFF,LIVE", 300'000 );
    b.tell( "FORM CSV" );
    b.tell( "ABOR" );
    b.tell( "FORM CSV;:INIT;:ROUT:BYP ON,LIVE,FORC", 100'000 );
    CHECK_CONTAINS( b.drain(), "K," );
    b.tell( "ABOR;:ROUT:BYP OFF", 200'000 );
    // The null window takes no order, not even LIVE.
    b.tell( "SENS:PROF SLEEP;:CAL:ZERO 5", 1'000'000 );
    b.tell( "ROUT:BYP ON,LIVE" );
    CHECK_CONTAINS( b.errors(), "112," );
    b.tell( "ABOR", 300'000 );
}

TEST( "route", "the options each order accepts" ) {
    Bench b;
    b.tell( "ROUT:REL3 ON,FORC" );
    CHECK_CONTAINS( b.errors(), "-241,\"Hardware missing;k3 is not wired on this rig" );
    b.tell( "ROUT:NULL ON,FORC" );
    CHECK_CONTAINS( b.errors(), "-224," );
    b.tell( "ROUT:REL1 ON;:ROUT:REL2 ON;:ROUT:REL5 ON;:ROUT:REL0 ON" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "4" );
    b.tell( "*CLS;:ROUT:REL3 SOMETIMES;:ROUT:NULL MAYBE" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "2" );
    b.tell( "ROUT:REL4 ON", 200'000 );
    CHECK_CONTAINS( b.errors(), "-241,\"Hardware missing;k4 is not wired on this rig" );
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "moved" ), "-" );
}

TEST( "route", "relays that failed at boot refuse every order" ) {
    Bench b( []( sim::World& w ) {
        w.faults.failGpioConfig    = true;
        w.faults.failGpioConfigPin = 7;
    } );
    (void)b.errors();
    b.tell( "ROUT:STAT?;:ROUT:RES?;:ROUT:REL3 ON" );
    const std::string errors = b.errors();
    CHECK_CONTAINS( errors, "100," );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "relays" ), "0" );
}
