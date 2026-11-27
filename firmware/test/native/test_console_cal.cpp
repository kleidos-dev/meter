// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_console_cal.cpp
 * @brief CALibration over the line: the null, the tag, the VBUS side, the
 *        stored data, and the shunt measured against a reference resistor.
 */
#include "bench.h"
#include "unit.h"

#include <string>

using bench::Bench;
using bench::field;

namespace {

/// The calibration rig: the DUT replaced by the 100 Ohm reference, VBUS on
/// its top lead, a 4.1 V cell, and a shunt of @p trueMicroOhm.
void referenceRig( sim::World& w, std::int64_t trueMicroOhm ) {
    const std::int64_t cellUv        = 4'100'000;
    const std::int64_t totalMicroOhm = 100'000'000 + trueMicroOhm;
    w.circuit.dutNa                  = cellUv * 1'000'000'000 / totalMicroOhm;  // uV/uOhm = A
    w.ina.busUv                      = w.circuit.dutNa * 100'000'000 / 1'000'000'000;
    w.ina.trueShuntMicroOhm          = trueMicroOhm;
}

}  // namespace

TEST( "cal", "a null: started, reported while running, stored at its end" ) {
    Bench b;
    b.world().circuit.dutNa = 0;
    b.tell( "CAL:ZERO:RES?" );
    CHECK_CONTAINS( b.errors(), "no null taken since boot" );
    b.tell( "SENS:PROF SLEEP;:CAL:ZERO 2", 800'000 );
    const std::string running = b.ask( "CAL:ZERO:RES?" );
    CHECK_EQ( field( running, "running" ), "1" );
    CHECK_EQ( field( running, "n_expect" ), "29" );
    CHECK_EQ( b.ask( "STAT:OPER:COND?" ), "1" );
    b.tell( "SENS:PROF STATE" );
    CHECK_CONTAINS( b.errors(), "112," );
    b.world().run( 2'500'000 );
    b.tell( "ABOR", 300'000 );
    const std::string result = b.ask( "CAL:ZERO:RES?" );
    CHECK_EQ( field( result, "stored" ), "1" );
    CHECK_EQ( field( result, "n" ), "29" );
    CHECK_EQ( field( b.ask( "CAL:DATA?" ), "nulled_r0" ), "1" );
}

TEST( "cal", "a null is refused on path=both, and bad windows" ) {
    Bench b;
    b.tell( "ROUT:BYP ON", 100'000 );
    b.tell( "CAL:ZERO 5" );
    CHECK_CONTAINS( b.errors(), "101," );
    b.tell( "ROUT:BYP OFF;:CAL:ZERO 0;:CAL:ZERO 601;:CAL:ZERO", 100'000 );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
}

TEST( "cal", "a relay null beyond 50 uV is not stored; an empty window takes nothing" ) {
    Bench b( []( sim::World& w ) {
        w.circuit.dutNa           = 5'000'000;
        w.circuit.leadStuckClosed = true;
    } );
    b.tell( "ROUT:BYP ON;:ROUT:LEAD OPEN", 300'000 );  // the check is not run: plain orders
    b.tell( "SENS:PROF SLEEP;:CAL:ZERO 1", 2'000'000 );
    b.tell( "ABOR", 300'000 );
    CHECK_CONTAINS( b.errors(), "102," );
    CHECK_EQ( field( b.ask( "CAL:ZERO:RES?" ), "stored" ), "0" );
    b.tell( "ROUT:NULL OFF", 300'000 );
    b.tell( "CAL:ZERO 10;:ABOR", 1'500'000 );
    CHECK_CONTAINS( b.errors(), "108," );
}

TEST( "cal", "tag, VBUS side and the stored data; a flash that will not write" ) {
    Bench b;
    b.tell( "CAL:TAG \"R100_test\";:CAL:VBUS SOUR" );
    CHECK_EQ( b.ask( "CAL:TAG?;:CAL:VBUS?" ), "\"R100_test\";SOURCE" );
    b.tell( "CAL:TAG plain;:CAL:VBUS UNKN" );
    CHECK_EQ( b.ask( "CAL:TAG?;:CAL:VBUS?" ), "\"plain\";UNKNOWN" );
    const std::string data = b.ask( "CAL:DATA?" );
    CHECK_EQ( field( data, "cal_tag" ), "plain" );
    CHECK_EQ( field( data, "off_die_mc_r1" ), "0" );
    b.tell( "CAL:TAG \"has space\";:CAL:TAG \"a_tag_that_is_far_too_long\";:CAL:VBUS LOAD" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
    b.tell( "*CLS" );
    // The flash does not take the record: the tag is live until reboot.
    b.world().flash.failProgram = true;
    b.tell( "CAL:TAG unsaved" );
    CHECK_CONTAINS( b.errors(),
                    "106,\"Calibration not saved;calibration write failed (live until reboot)" );
    CHECK_EQ( b.ask( "CAL:TAG?" ), "\"unsaved\"" );
}

TEST( "cal", "the shunt value: set within its window, refused outside it" ) {
    Bench b;
    b.tell( "SENS:CURR:SHUN 0.100123" );
    CHECK_EQ( b.ask( "SENS:CURR:SHUN?" ), "0.100123" );
    CHECK_EQ( field( b.ask( "CAL:DATA?" ), "shunt_measured" ), "1" );
    b.tell( "SENS:CURR:SHUN 0.3;:SENS:CURR:SHUN 0.01;:SENS:CURR:SHUN ohm" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
}

TEST( "cal", "each range's own shunt: set, read, refused, and cleared by the common value" ) {
    Bench b;
    b.tell( "SENS:CURR:SHUN 0.100116" );
    CHECK_EQ( b.ask( "SENS:CURR:SHUN:RANG0?;RANG1?" ), "0.100116;0.100116" );
    b.tell( "SENS:CURR:SHUN:RANG1 0.100144" );
    CHECK_EQ( b.ask( "SENS:CURR:SHUN:RANG0?;RANG1?" ), "0.100116;0.100144" );
    CHECK_EQ( b.ask( "SENS:CURR:SHUN?" ), "0.100116" );
    std::string data = b.ask( "CAL:DATA?" );
    CHECK_EQ( field( data, "shunt_uohm" ), "100116" );
    CHECK_EQ( field( data, "shunt_uohm_r0" ), "100116" );
    CHECK_EQ( field( data, "shunt_uohm_r1" ), "100144" );
    // A range is always named, and only the two there are.
    b.tell( "SENS:CURR:SHUN:RANG 0.1;:SENS:CURR:SHUN:RANG2 0.1;:SENS:CURR:SHUN:RANG3?" );
    std::string errors = b.errors();
    CHECK_CONTAINS( errors, "-114," );
    CHECK_CONTAINS( errors, "name the range" );
    b.tell(
        "SENS:CURR:SHUN:RANG0 0.3;:SENS:CURR:SHUN:RANG0 0.01;:SENS:CURR:SHUN:RANG0 ohm;"
        ":SENS:CURR:SHUN:RANG0?" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
    b.tell( "*CLS" );
    // The common value replaces both.
    b.tell( "SENS:CURR:SHUN 0.100120" );
    data = b.ask( "CAL:DATA?" );
    CHECK_EQ( field( data, "shunt_uohm_r1" ), "100120" );
    CHECK( b.clean() );
}

TEST( "cal", "a capture converts with its range's own shunt" ) {
    Bench b;
    b.tell( "SENS:CURR:SHUN 0.100116;:SENS:CURR:SHUN:RANG1 0.100144" );
    b.tell( "SENS:CURR:RANG:IND 1;:SENS:PROF SLEEP;:INIT", 600'000 );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "shunt_uohm" ), "100144" );
    b.tell( "ABOR", 300'000 );
    CHECK_EQ( field( b.ask( "SENS:CHAR?" ), "shunt_uohm" ), "100144" );
    b.tell( "SENS:CURR:RANG:IND 0;:INIT", 600'000 );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "shunt_uohm" ), "100116" );
    b.tell( "ABOR", 300'000 );
    CHECK_EQ( field( b.ask( "SENS:CHAR?" ), "shunt_uohm" ), "100116" );
    (void)b.drain();
}

TEST( "cal", "the calibration date: the host's, checked, stored, echoed" ) {
    Bench b;
    CHECK_EQ( b.ask( "CAL:DATE?" ), "0,0,0" );
    CHECK_EQ( field( b.ask( "CAL:DATA?" ), "cal_date" ), "-" );
    b.tell( "CAL:DATE 2026,11,20" );
    CHECK_EQ( b.ask( "CAL:DATE?" ), "2026,11,20" );
    CHECK_EQ( field( b.ask( "CAL:DATA?" ), "cal_date" ), "2026-11-20" );
    b.tell( "CAL:DATE 2026,2,30;:CAL:DATE 2025,1,1;:CAL:DATE 2026,11;:CAL:DATE 2026,11,20,1" );
    const std::string errors = b.errors();
    CHECK_CONTAINS( errors, "-222," );
    CHECK_CONTAINS( errors, "-109," );
    CHECK_CONTAINS( errors, "-108," );
    b.tell( "CAL:DATE 2026,x,1;:CAL:DATE 2026,1,x;:CAL:DATE y,1,1" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
    CHECK_EQ( b.ask( "CAL:DATE?" ), "2026,11,20" );
}

TEST( "cal", "the reference resistor: default, set, refused" ) {
    Bench b;
    CHECK_EQ( b.ask( "CAL:SHUN:REF?" ), "100.0" );
    b.tell( "CAL:SHUN:REF 99.9987" );
    CHECK_EQ( b.ask( "CAL:SHUN:REF?" ), "99.9987" );
    b.tell( "CAL:SHUN:REF 0.5;:CAL:SHUN:REF 5000;:CAL:SHUN:REF big" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
    b.tell( "*RST", 100'000 );
    CHECK_EQ( b.ask( "CAL:SHUN:REF?" ), "100.0" );
}

TEST( "cal", "the shunt against the reference, on both ranges, agrees with the truth" ) {
    Bench b( []( sim::World& w ) { referenceRig( w, 100'123 ); } );
    b.tell( "CAL:SHUN:RES?" );
    CHECK_CONTAINS( b.errors(), "no shunt point" );
    b.tell( "SENS:PROF STATE;:SAMP:COUN 200" );
    b.tell( "CAL:SHUN" );
    CHECK_CONTAINS( b.errors(), "range 0 has no null" );
    // The null: no current crosses the shunt.
    const std::int64_t current = b.world().circuit.dutNa;
    b.world().circuit.dutNa    = 0;
    b.tell( "CAL:ZERO 1", 1'200'000 );
    b.tell( "ABOR;:SENS:CURR:RANG:IND 1;:CAL:ZERO 1", 1'200'000 );
    b.tell( "ABOR;:SENS:CURR:RANG:IND 0", 100'000 );
    b.world().circuit.dutNa = current;
    b.tell( "CAL:SHUN", 1'000'000 );
    const std::string r0 = b.ask( "CAL:SHUN:RES?" );
    CHECK_EQ( field( r0, "check" ), "ok" );
    CHECK_EQ( field( r0, "range" ), "0" );
    const long long nano0 = std::stoll( field( r0, "r_nohm" ) );
    CHECK( nano0 > 100'100'000 && nano0 < 100'150'000 );
    CHECK_EQ( field( r0, "n" ), "200" );
    b.tell( "SENS:CURR:RANG:IND 1;:CAL:SHUN", 1'000'000 );
    const std::string r1 = b.ask( "CAL:SHUN:RES?" );
    CHECK_EQ( field( r1, "check" ), "ok" );
    const long long nano1 = std::stoll( field( r1, "r_nohm" ) );
    CHECK( nano1 > 100'110'000 && nano1 < 100'135'000 );
    CHECK( b.clean() );
}

TEST( "cal", "a shunt point on a ring profile or with the VBUS lead left off is refused" ) {
    Bench b( []( sim::World& w ) { referenceRig( w, 100'000 ); } );
    b.calibration().setOffset( kleidos::meter::ina228::ShuntRange::Wide163mV, 480'000, 25'000 );
    b.tell( "SAMP:COUN 100;:CAL:SHUN", 1'000'000 );
    CHECK_CONTAINS( b.errors(), "converted no bus voltage" );
    b.world().ina.busUv = 3'000;  // the VBUS lead not moved to the reference
    b.tell( "SENS:PROF STATE;:CAL:SHUN", 1'000'000 );
    CHECK_EQ( field( b.ask( "CAL:SHUN:RES?" ), "check" ), "reference_low" );
    b.tell( "ROUT:BYP ON;:CAL:SHUN", 200'000 );
    CHECK_CONTAINS( b.errors(), "101," );
}
