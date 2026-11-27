// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_console_measure.cpp
 * @brief SENSe, SAMPle, FORMat, TRIGger, CONFigure, MEASure, READ, FETCh,
 *        INITiate, ABORt and TRACe over the line, against the modeled bench.
 */
#include "bench.h"
#include "unit.h"

#include <cmath>
#include <cstdlib>
#include <string>

using bench::Bench;
using bench::field;
using bench::units;

TEST( "measure", "profiles: set, query, report, refused while open" ) {
    Bench b;
    CHECK_EQ( b.ask( "SENS:PROF?" ), "TRACE" );
    b.tell( "SENS:PROF SLE" );
    CHECK_EQ( b.ask( "SENS:PROF?;:SAMP:TIM?" ), "SLEEP;0.06752" );
    CHECK_CONTAINS( b.ask( "SENS:PROF:REP?" ), "adc_config=0xF1C2" );
    b.tell( "SENS:PROF BURS" );
    CHECK_EQ( b.ask( "SENS:PROF?" ), "BURST" );
    b.tell( "SENS:PROF FAST" );
    CHECK_CONTAINS( b.errors(), "-224," );
    b.tell( "SENS:PROF STAT;:INIT" );
    b.tell( "SENS:PROF SLE" );
    CHECK_CONTAINS( b.errors(), "-221,\"Settings conflict;capture running (ABORt first)\"" );
    b.tell( "ABOR" );
}

TEST( "measure", "ranges: amps, keywords, index, report, failures" ) {
    Bench b;
    CHECK_EQ( b.ask( "SENS:CURR:RANG?" ), "1.6384" );
    b.tell( "SENS:CURR:RANG 0.2" );
    CHECK_EQ( b.ask( "SENS:CURR:RANG?;RANG:IND?" ), "0.4096;1" );
    const std::string report = b.ask( "SENS:CURR:RANG:REP?" );
    CHECK_EQ( field( report, "rstacc" ), "1" );
    CHECK_EQ( field( report, "cnvrf" ), "1" );
    b.tell( "SENS:CURR:RANG MAX" );
    CHECK_EQ( b.ask( "SENS:CURR:RANG:IND?" ), "0" );
    b.tell( "SENS:CURR:RANG MIN" );
    CHECK_EQ( b.ask( "SENS:CURR:RANG:IND?" ), "1" );
    b.tell( "SENS:CURR:RANG DEF;:SENS:CURR:RANG 1.0" );
    CHECK_EQ( b.ask( "SENS:CURR:RANG:IND?" ), "0" );
    b.tell( "SENS:CURR:RANG:IND 1;:SENS:CURR:RANG:IND 0" );
    CHECK( b.clean() );
    b.tell( "SENS:CURR:RANG 2.0" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "SENS:CURR:RANG -0.1" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "SENS:CURR:RANG HUGE" );
    CHECK_CONTAINS( b.errors(), "-224," );
    b.tell( "SENS:CURR:RANG #H10" );
    CHECK_CONTAINS( b.errors(), "-1" );
    b.tell( "SENS:CURR:RANG" );
    CHECK_CONTAINS( b.errors(), "-109," );
    b.tell( "SENS:CURR:RANG:IND 2" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "SENS:CURR:RANG:IND x" );
    CHECK_CONTAINS( b.errors(), "-1" );
    b.world().ina.failCount = 1;
    b.tell( "SENS:CURR:RANG:IND 1" );
    CHECK_CONTAINS( b.errors(), "-240," );
    b.world().ina.failRegister = kleidos::meter::ina228::kRegDiagAlrt;
    b.world().ina.failCount    = 2;
    b.tell( "SENS:CURR:RANG:IND 1" );
    b.world().ina.failRegister = -1;
    CHECK_EQ( field( b.ask( "SENS:CURR:RANG:REP?" ), "cnvrf" ), "0" );
}

TEST( "measure", "apertures, average count, sample count, format and trigger" ) {
    Bench b;
    b.tell( "SENS:PROF STATE" );
    CHECK_EQ( b.ask( "SENS:CURR:APER?;:SENS:VOLT:APER?;:SENS:TEMP:APER?;:SENS:AVER:COUN?" ),
              "0.001052;0.00005;0.00005;1" );
    b.tell( "SAMP:COUN 5" );
    CHECK_EQ( b.ask( "SAMP:COUN?" ), "5" );
    b.tell( "SAMP:COUN 0;:SAMP:COUN 2000000;:SAMP:COUN x" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
    b.tell( "*CLS;:FORM CSV" );
    CHECK_EQ( b.ask( "FORM?" ), "CSV" );
    b.tell( "FORM:DATA TEXT;:FORM XML" );
    CHECK_EQ( b.ask( "FORM?;:SYST:ERR:COUN?" ), "TEXT;1" );
    b.tell( "*CLS;:TRIG:SOUR IMM" );
    CHECK_EQ( b.ask( "TRIG:SOUR?" ), "IMM" );
    b.tell( "TRIG:SOUR BUS" );
    CHECK_CONTAINS( b.errors(), "-224," );
}

TEST( "measure", "the stream's channels: chosen, defaulted, refused" ) {
    Bench b;
    b.tell( "SENS:PROF SLEEP" );
    CHECK_EQ( b.ask( "SENS:FUNC:ON?" ), "\"CURR\",\"VOLT\",\"TEMP\"" );
    b.tell( "SENS:PROF STATE" );
    CHECK_EQ( b.ask( "SENS:FUNC:ON?" ), "\"CURR\"" );
    b.tell( "SENS:FUNC:ON \"VOLT\",\"POW\",TEMP" );
    CHECK_EQ( b.ask( "SENS:FUNC:ON?" ), "\"VOLT\",\"POW\",\"TEMP\"" );
    b.tell( "SENS:FUNC:ON:DEF" );
    CHECK_EQ( b.ask( "SENS:FUNC:ON?" ), "\"CURR\"" );
    b.tell( "SENS:FUNC:ON \"FREQ\"" );
    CHECK_CONTAINS( b.errors(), "-224," );
    b.tell( "SENS:FUNC:ON" );
    CHECK_CONTAINS( b.errors(), "-109," );
    b.tell( "SENS:FUNC:ON VOLTAGEVOLTAGEVOLTAGE" );
    CHECK_CONTAINS( b.errors(), "-" );
    b.tell( "SENS:FUNC:ON \"unterminated" );
    CHECK_CONTAINS( b.errors(), "-" );
    b.tell( "SENS:PROF TRAC" );
    CHECK_EQ( b.ask( "SENS:FUNC:ON?" ), "\"\"" );
}

TEST( "measure", "CONFigure picks the function and its channels" ) {
    Bench b;
    b.tell( "SENS:PROF STATE" );
    const char* functions[] = { "VOLT", "POW", "TEMP", "ENER", "CURR" };
    const char* replies[] = { "\"VOLT\"", "\"POW\"", "\"TEMP\"", "\"CURR\",\"VOLT\"", "\"CURR\"" };
    for ( int i = 0; i < 5; ++i ) {
        b.tell( std::string( "CONF:" ) + functions[i] );
        CHECK_EQ( b.ask( "SENS:FUNC:ON?" ), replies[i] );
    }
    CHECK_EQ( b.ask( "CONF?" ), "\"CURR\"" );
    b.tell( "CONF:CURR 0.1" );
    CHECK_EQ( b.ask( "SENS:CURR:RANG:IND?" ), "1" );
    b.tell( "CONF:CURR BAD" );
    CHECK_CONTAINS( b.errors(), "-224," );
    b.tell( "CONF:VOLT 1" );
    CHECK_CONTAINS( b.errors(), "-108," );
    b.tell( "SENS:PROF TRAC;:CONF:VOLT" );
    CHECK_EQ( b.ask( "CONF?" ), "\"VOLT\"" );
}

TEST( "measure", "MEASure, READ and FETCh of every function" ) {
    // The circuit decides every value, with no noise: 40 mA through the
    // nominal 0.1 ohm, plus the sense loop's 0.48 uV offset (4.8 uA), is
    // 12,801.5 counts of the 312.5 nV LSB, read as 12,802: 40.00625 mA. The
    // DUT sees the 4.0 V bus less that drop, 3.995999 V, and draws
    // 0.159864 W. The die is at 30 C.
    Bench b;
    b.world().circuit.dutNa = 40'000'000;
    b.world().ina.busUv     = 4'000'000;
    b.world().ina.dieMc     = 30'000;
    b.tell( "SENS:PROF STATE;:SAMP:COUN 20" );
    CHECK_EQ( b.ask( "MEAS:CURR?" ), "0.04000625" );
    CHECK_EQ( b.ask( "MEAS:VOLT?" ), "3.995999" );
    CHECK_EQ( b.ask( "MEAS:POW?" ), "0.159864" );
    // The energy is the mean power over the window's own duration.
    const std::string energy = b.ask( "MEAS:ENER?" );
    const long long   window = std::stoll( field( b.ask( "FETC:STAT?" ), "dur_us" ) );
    CHECK( std::abs( std::stod( energy ) - 0.159864 * static_cast<double>( window ) / 1e6 )
           < 1e-9 );
    CHECK_EQ( b.ask( "MEAS:TEMP?" ), "30.0" );
    CHECK_EQ( b.ask( "CONF?" ), "\"TEMP\"" );
    // READ? measures what CONFigure last chose, the temperature here; each
    // READ:<function>? its own function, from a window of its own.
    CHECK_EQ( b.ask( "READ?" ), "30.0" );
    CHECK_EQ( b.ask( "READ:CURR?" ), "0.04000625" );
    CHECK_EQ( b.ask( "READ:VOLT?" ), "3.995999" );
    CHECK_EQ( b.ask( "READ:POW?" ), "0.159864" );
    CHECK_EQ( b.ask( "READ:ENER?" ), energy );
    CHECK_EQ( b.ask( "READ:TEMP?" ), "30.0" );
    // FETCh answers from the last window, without a new one.
    CHECK_EQ( b.ask( "FETC?" ), "30.0" );
    CHECK_EQ( b.ask( "FETC:CURR?;:FETC:VOLT?;:FETC:POW?;:FETC:ENER?;:FETC:TEMP?" ),
              "0.04000625;3.995999;0.159864;" + energy + ";30.0" );
    const std::string stats = b.ask( "FETC:STAT?" );
    CHECK_EQ( field( stats, "samples" ), "20" );
    CHECK_EQ( field( stats, "stale_edges" ), "0" );
    CHECK_EQ( field( stats, "q" ), "uncal" );
    // The charge is CHARGE's two boundary reads apart, in its 3.125 uC LSB,
    // and its mean over their span is the 40 mA the DUT draws.
    const long long counts = std::stoll( field( stats, "charge_raw_end" ) )
                             - std::stoll( field( stats, "charge_raw_start" ) );
    CHECK( std::abs( std::stod( b.ask( "FETC:CHAR?" ) ) - static_cast<double>( counts ) * 3.125e-6 )
           < 1e-12 );
    const double span = std::stod( field( stats, "charge_us" ) ) / 1e6;
    CHECK( std::abs( std::stod( b.ask( "FETC:CHAR?" ) ) - 0.040 * span ) < 3.125e-6 );
    CHECK( b.clean() );
    b.tell( "MEAS:CURR? 0.1" );
    b.tell( "MEAS:VOLT? 1" );
    CHECK_CONTAINS( b.errors(), "-108," );
    b.tell( "MEAS:CURR? HUGE" );
    CHECK_CONTAINS( b.errors(), "-224," );
}

TEST( "measure", "the charge is corrected by the capture's shunt, and left nominal by none" ) {
    // FETCh:CHARge? scales the INA228's nominal charge by the nominal shunt
    // over the measured one. Each condition of the guard decides on its own
    // (MC/DC): the nominal charge's bounds (a 40 mA capture is far inside
    // them) and a shunt above 0. The store takes no shunt outside its window
    // from the line or from the flash (calibration.h), so a 0 is planted in
    // memory, past both, to show the guard leaves the nominal value rather
    // than divide by it.
    Bench b;
    b.world().circuit.dutNa = 40'000'000;
    b.tell( "SENS:PROF STATE;:SAMP:COUN 20" );
    REQUIRE( b.ask( "MEAS:CURR?" ) == "0.04000625" );
    b.calibration().setShuntMicroOhm( kleidos::meter::kNominalShuntMicroOhm );
    const double nominal = std::stod( b.ask( "FETC:CHAR?" ) );
    b.calibration().setShuntMicroOhm( 2 * kleidos::meter::kNominalShuntMicroOhm );
    const double halved = std::stod( b.ask( "FETC:CHAR?" ) );
    b.calibration().setShuntMicroOhm( 0 );
    const double uncorrected = std::stod( b.ask( "FETC:CHAR?" ) );
    CHECK( nominal > 0.0 );
    CHECK( halved > nominal * 0.49 && halved < nominal * 0.51 );
    CHECK( uncorrected == nominal );
    CHECK( b.clean() );
}

TEST( "measure", "what a profile did not convert is refused, not reported as zero" ) {
    Bench b;
    // Two statements: the operands of `+` are unsequenced, and GCC asks the
    // queue before the query that fills it.
    CHECK_EQ( b.ask( "FETC:STAT?" ), "" );
    CHECK_CONTAINS( b.errors(), "-230," );
    b.tell( "FETC:CURR?" );
    CHECK_CONTAINS( b.errors(), "no acquisition yet" );
    b.tell( "SENS:PROF TRAC;:SAMP:COUN 100" );
    // The bench's 50 mA, plus the offset's 4.8 uA, in the 3.125 uA LSB.
    CHECK_EQ( b.ask( "MEAS:CURR?", 2'000'000 ), "0.05000625" );
    b.tell( "FETC:VOLT?" );
    CHECK_CONTAINS( b.errors(), "converted no bus voltage" );
    CHECK_EQ( b.ask( "FETC:TEMP?" ), "25.0" );  // the boundary's triggered conversion
    b.world().ina.failRegister = kleidos::meter::ina228::kRegCharge;
    b.world().ina.failCount    = 4;
    CHECK_EQ( b.ask( "MEAS:CURR?", 2'000'000 ), "0.05000625" );
    b.world().ina.failRegister = -1;
    b.tell( "FETC:CHAR?" );
    CHECK_CONTAINS( b.errors(), "no CHARGE boundary" );
    // No die temperature at all: every temperature read fails.
    b.world().ina.failRegister = kleidos::meter::ina228::kRegDieTemp;
    b.world().ina.failCount    = 1000;
    b.ask( "MEAS:CURR?", 2'000'000 );
    b.world().ina.failCount    = 0;
    b.world().ina.failRegister = -1;
    b.tell( "FETC:TEMP?" );
    CHECK_CONTAINS( b.errors(), "no die temperature" );
}

TEST( "measure", "an acquisition that cannot complete says why" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:SAMP:COUN 20" );
    b.world().ina.failAfter = 0;
    b.tell( "MEAS:CURR?", 300'000 );
    b.world().ina.failAfter = -1;
    CHECK_CONTAINS( b.errors(), "-240," );
    // Every sample read fails: the window closes short.
    b.world().ina.failRegister = kleidos::meter::ina228::kRegVShunt;
    b.world().ina.failCount    = 100000;
    b.tell( "MEAS:CURR?", 3'000'000 );
    b.world().ina.failCount    = 0;
    b.world().ina.failRegister = -1;
    CHECK_CONTAINS( b.errors(), "-230," );
    // A shunt that is bypassed is never measured.
    b.tell( "ROUT:BYP ON", 100'000 );
    b.tell( "MEAS:CURR?" );
    CHECK_CONTAINS( b.errors(), "101," );
}

TEST( "measure", "a streamed capture in TEXT and in CSV, with its header" ) {
    Bench b;
    b.tell( "SENS:PROF SLEEP;:SENS:FUNC:ON \"CURR\",\"VOLT\",\"POW\",\"TEMP\";:INIT", 1'500'000 );
    std::string text = b.drain();
    CHECK_CONTAINS( text, "METER t=" );
    CHECK_CONTAINS( text, " ch=i_ua v=" );
    CHECK_CONTAINS( text, " ch=die_c v=" );
    const std::string header = b.ask( "FETC:HEAD?" );
    CHECK_EQ( field( header, "chip" ), "ina228" );
    CHECK_EQ( field( header, "sink" ), "stream" );
    CHECK_EQ( field( header, "path" ), "shunt" );
    CHECK_EQ( field( header, "format" ), "text" );
    CHECK_EQ( field( header, "pace" ), "alert" );  // SLEEP is paced by the pin
    CHECK_EQ( field( header, "pace_expect" ), "8" );
    CHECK_EQ( field( header, "alatch" ), "1" );  // the clip detector stays latched
    b.tell( "TRAC:MARK \"boot\"" );
    CHECK_CONTAINS( b.drain(), "METER_MARK mark=boot" );
    b.tell( "ABOR", 300'000 );
    b.tell( "FORM CSV;:INIT", 1'500'000 );
    text = b.drain();
    CHECK_CONTAINS( text, "S," );
    b.tell( "TRAC:MARK x1" );
    CHECK_CONTAINS( b.drain(), "K," );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "format" ), "csv" );
    b.tell( "ABOR" );
    CHECK( b.clean() );
    b.tell( "ABOR" );
    CHECK_CONTAINS( b.errors(), "111," );
}

TEST( "measure", "INITiate refuses a ring profile with channels and a bypassed shunt" ) {
    Bench b;
    b.tell( "SENS:FUNC:ON \"CURR\";:INIT" );
    CHECK_CONTAINS( b.errors(), "a ring profile takes no channels" );
    b.tell( "SENS:FUNC:ON:DEF;:SENS:PROF STATE;:ROUT:BYP ON", 100'000 );
    b.tell( "INIT" );
    CHECK_CONTAINS( b.errors(), "path=both" );
    b.tell( "ROUT:LEAD OPEN", 100'000 );
    b.tell( "INIT" );
    CHECK_CONTAINS( b.errors(), "path=bypass" );
    b.tell( "INIT:FORC", 50'000 );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "q" ), "invalid" );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "q" ), "invalid" );
    b.tell( "ROUT:NULL OFF", 200'000 );
    // BURST is a ring profile too.
    b.tell( "SENS:PROF BURST;:SENS:FUNC:ON \"CURR\",\"VOLT\";:INIT" );
    CHECK_CONTAINS( b.errors(), "-221," );
}

TEST( "measure", "TRACE: capture to RAM, dump scaled and raw, ranges and refusals" ) {
    Bench b( []( sim::World& w ) { w.ringSamples = 4096; } );
    b.tell( "TRAC:DATA?" );
    CHECK_CONTAINS( b.errors(), "no trace captured" );
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:INIT;:TRAC:MARK go;:TRAC:DATA?", 20'000 );
    CHECK_CONTAINS( b.errors(), "-221," );  // refused while the ring fills
    b.world().run( 2'000'000 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "sink" ), "ring" );  // full, awaiting ABORt
    b.tell( "ABOR" );
    CHECK_EQ( b.ask( "TRAC:POIN?;:TRAC:POIN:MAX?" ), "4096;4096" );
    b.drain();
    b.world().send( "TRAC:DATA? SCAL,1020,8" );
    b.world().run( 200'000 );
    std::string dump = b.drain();
    CHECK( dump.rfind( "#", 0 ) == 0 );
    CHECK_CONTAINS( dump, "METER_DUMP count=8" );
    CHECK_CONTAINS( dump, "A n=1024" );
    CHECK_CONTAINS( dump, "METER_DUMP_END" );
    b.world().send( "TRAC:DATA? RAW,0,40;*OPC?" );
    b.world().run( 200'000 );
    dump = b.drain();
    CHECK_CONTAINS( dump, "fmt=raw20" );
    CHECK_CONTAINS( dump, "R n=32 c=" );
    CHECK_CONTAINS( dump, "K n=0 label=go" );
    CHECK_CONTAINS( dump, ";1\n" );
    b.world().send( "TRAC:DATA? 4090" );
    b.world().run( 200'000 );
    CHECK_CONTAINS( b.drain(), "count=6" );
    b.tell( "TRAC:DATA? 5000" );
    b.tell( "TRAC:DATA? RAW,0,0" );
    b.tell( "TRAC:DATA? BOTH" );
    b.tell( "TRAC:DATA? RAW,x" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "4" );
}

TEST( "measure", "TRACe:MARK: labels, the capture it needs, a full table" ) {
    Bench b;
    b.tell( "TRAC:MARK ok" );
    CHECK_CONTAINS( b.errors(), "111," );
    b.tell( "TRAC:MARK \"has space\"" );
    b.tell( "TRAC:MARK \"seventeen_chars_x\"" );
    b.tell( "TRAC:MARK" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
    b.tell( "*CLS;:SENS:PROF STATE;:INIT" );
    for ( int i = 0; i < 17; ++i ) {
        b.tell( "TRAC:MARK m" + std::to_string( i ), 2'000 );
    }
    CHECK_CONTAINS( b.errors(), "105," );
    b.tell( "ABOR" );
}

TEST( "measure", "charge: read, reset, and a bus that fails" ) {
    Bench b;
    b.world().run( 100'000 );
    const std::string charge = b.ask( "SENS:CHAR?" );
    CHECK( std::stoll( field( charge, "raw" ) ) > 0 );
    CHECK_EQ( field( charge, "of" ), "0" );
    b.tell( "SENS:CHAR:RES" );
    CHECK( std::stoll( field( b.ask( "SENS:CHAR?" ), "dur_us" ) ) < 100'000 );
    b.world().ina.failCount = 1;
    b.tell( "SENS:CHAR?" );
    b.world().ina.failCount = 1;
    b.tell( "SENS:CHAR:RES" );
    b.world().ina.failRegister = kleidos::meter::ina228::kRegCharge;
    b.world().ina.failCount    = 1;
    b.tell( "SENS:CHAR?" );
    b.world().ina.failRegister = -1;
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
}

TEST( "measure", "a stream record waits behind a half-written response" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT" );
    std::string message;
    for ( int i = 0; i < 20; ++i ) {
        message += i == 0 ? "SYST:INF?" : ";:SYST:INF?";
    }
    b.world().send( message );
    b.world().run( 100'000 );
    const std::string out = b.drain();
    // The response line is never split by a stream record.
    const std::size_t start = out.find( "model=" );
    REQUIRE( start != std::string::npos );
    const std::size_t end = out.find( '\n', start );
    CHECK( out.substr( start, end - start ).find( "METER t=" ) == std::string::npos );
    b.tell( "ABOR" );
}
