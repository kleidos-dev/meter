// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_console.cpp
 * @brief The console's line, dispatcher, response buffer, error queue and
 *        status registers, and the common and SYSTem commands, over UART0.
 */
#include "bench.h"
#include "unit.h"

#include <string>

using bench::Bench;
using bench::field;
using bench::units;

TEST( "console", "IEEE 488.2 common commands" ) {
    Bench b;
    CHECK_EQ( b.ask( "*IDN?" ), "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0" );
    CHECK_EQ( b.ask( "*ESR?;*ESR?" ), "128;0" );
    b.tell( "*ESE 36;*SRE 48" );
    CHECK_EQ( b.ask( "*ESE?;*SRE?" ), "36;48" );
    CHECK_EQ( b.ask( "*OPC?" ), "1" );
    b.tell( "*OPC;*WAI" );
    CHECK_EQ( b.ask( "*ESR?" ), "1" );
    CHECK_EQ( b.ask( "*TST?" ), "0" );
    CHECK_EQ( b.ask( "SYST:VERS?" ), "1999.0" );
    b.tell( "*ESE 256;*SRE 300;*ESE -1;*SRE" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "4" );
    b.tell( "*CLS" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?;*ESR?" ), "0;0" );
}

TEST( "console", "the status byte and its summaries" ) {
    Bench b;
    // Uncalibrated from boot, so QUES:COND has the calibration bit, and its
    // rising edge at boot is latched in the event register.
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "256" );
    b.tell( "STAT:QUES:ENAB 256;:STAT:OPER:ENAB 16;*SRE 255;*ESE 255" );
    CHECK_EQ( b.ask( "STAT:QUES:ENAB?;:STAT:OPER:ENAB?" ), "256;16" );
    const std::string stb = b.ask( "*STB?" );
    CHECK( ( std::stoi( stb ) & 0x08 ) != 0 );  // questionable summary
    CHECK( ( std::stoi( stb ) & 0x40 ) != 0 );  // master summary
    CHECK( ( std::stoi( stb ) & 0x20 ) != 0 );  // the power-on event
    b.tell( "BOGUS" );
    CHECK( ( std::stoi( b.ask( "*STB?" ) ) & 0x24 ) == 0x24 );  // error and event summary
    CHECK_EQ( b.ask( "STAT:QUES?" ), "256" );
    CHECK_EQ( b.ask( "STAT:QUES:EVEN?" ), "0" );  // an event is read once
    b.tell( "*CLS;STAT:QUES:ENAB 0;*SRE 0;*ESE 0" );
    CHECK_EQ( b.ask( "*STB?" ), "0" );
    CHECK_EQ( b.ask( "STAT:OPER:COND?" ), "0" );
    b.tell( "SENS:PROF STATE;:INIT" );
    CHECK_EQ( b.ask( "STAT:OPER:COND?" ), "16" );
    CHECK_EQ( b.ask( "STAT:OPER?" ), "16" );
    CHECK( ( std::stoi( b.ask( "*STB?" ) ) & 0x80 ) == 0 );  // read clears the event
    b.tell( "ABOR" );
    b.tell( "STAT:PRES" );
    CHECK_EQ( b.ask( "STAT:QUES:ENAB?;:STAT:OPER:ENAB?" ), "0;0" );
    b.tell( "*CLS;STAT:QUES:ENAB 40000;:STAT:OPER:ENAB 40000;:STAT:OPER:ENAB x" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
}

TEST( "console", "the error queue: order, ALL, overflow to -350" ) {
    Bench b;
    CHECK_EQ( b.ask( "SYST:ERR?" ), "0,\"No error\"" );
    b.tell( "FOO;BAR" );
    CHECK_EQ( b.ask( "SYST:ERR:ALL?" ),
              "-113,\"Undefined header;FOO\",-113,\"Undefined header;BAR\"" );
    for ( int i = 0; i < 12; ++i ) {
        b.tell( "NOPE", 5'000 );
    }
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "10" );
    const std::string all = b.errors();
    CHECK_CONTAINS( all, "-350,\"Queue overflow\"" );
}

TEST( "console", "dispatch: parse errors, parameters where none belong, compound paths" ) {
    Bench b;
    b.tell( "*IDN? 1" );
    CHECK_CONTAINS( b.errors(), "-108," );
    b.tell( "; ;" );  // empty units are skipped
    CHECK( b.clean() );
    b.tell( "SAMP:COUN \"open" );
    CHECK_CONTAINS( b.errors(), "-1" );
    const std::string longHeader = "SYST:" + std::string( 120, 'X' );
    b.tell( longHeader );
    CHECK_CONTAINS( b.errors(), "header too long" );
    // A relative header resolves against the previous unit's path.
    CHECK_EQ( b.ask( "SENS:CURR:RANG:INDex?;REP?" ).substr( 0, 8 ), "0;range=" );
    // An empty line does nothing; CR ends a line like LF.
    b.world().sendRaw( "\r" );
    b.tell( "" );
    CHECK( b.clean() );
}

TEST( "console", "a program message too long for the line is refused with -363" ) {
    Bench b;
    b.tell( std::string( 300, 'A' ) );
    CHECK_CONTAINS( b.errors(), "-363," );
    CHECK_EQ( b.ask( "*OPC?" ), "1" );
}

TEST( "console", "a response is whole and unbroken at every length about the buffer's" ) {
    // The buffer holds 2,559 bytes of units and the newline; a byte more makes
    // a flush, which falls between two units or anywhere in one. Every length
    // from well under to well over, ended by two-byte units or by the identity
    // laid across the edge, is the units joined, byte for byte, once.
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 100'000 );
    b.tell( "ABOR" );
    (void)b.drain();
    const std::string header = b.ask( "FETC:HEAD?" );
    const std::string idn    = b.ask( "*IDN?" );
    REQUIRE( header.size() > 500U );
    for ( const bool identityLast : { false, true } ) {
        for ( std::size_t target = 2550U - idn.size(); target <= 2570U; ++target ) {
            // Headers and identities, then a unit of seven bytes where what is
            // left is odd, then two-byte ones (`;0`), so the joined length is
            // the target.
            std::string message;
            std::string expected;
            const auto  add = [&message, &expected]( const std::string& ask,
                                                     const std::string& said ) {
                message += ( message.empty() ? "" : ";" ) + ask;
                expected += ( expected.empty() ? "" : ";" ) + said;
            };
            while ( expected.size() + 1U + header.size() + 10U <= target ) {
                add( ":FETC:HEAD?", header );
            }
            while ( expected.size() + 1U + idn.size() + 10U <= target ) {
                add( "*IDN?", idn );
            }
            if ( ( target - expected.size() ) % 2U == 1U ) {
                add( ":SYST:VERS?", "1999.0" );
            }
            while ( expected.size() < target ) {
                add( "*ESE?", "0" );
            }
            REQUIRE( expected.size() == target );
            if ( identityLast ) {
                add( "*IDN?", idn );
            }
            REQUIRE( message.size() < 255U );
            CHECK_EQ( b.ask( message, 500'000 ), expected );
            CHECK_EQ( b.drain(), "" );
        }
    }
    CHECK( b.clean() );
}

TEST( "console", "a long response is flushed in pieces and still ends in one newline" ) {
    Bench       b;
    std::string message;
    for ( int i = 0; i < 20; ++i ) {
        message += i == 0 ? "SYST:INF?" : ";:SYST:INF?";
    }
    const std::string line = b.ask( message, 2'000'000 );
    CHECK( units( line ).size() == 20 );
    CHECK( line.find( '\n' ) == std::string::npos );
}

TEST( "console", "SYSTem:INFormation?, STATus? and LED?" ) {
    Bench             b;
    const std::string info = b.ask( "SYST:INF?" );
    CHECK_EQ( field( info, "model" ), "kleidos-meter" );
    CHECK_EQ( field( info, "chip" ), "ina228" );
    CHECK_EQ( field( info, "addr" ), "0x40" );
    CHECK_EQ( field( info, "cal_tag" ), "-" );
    CHECK_EQ( field( info, "q" ), "uncal" );
    CHECK_EQ( field( info, "relays" ), "1" );
    CHECK_EQ( field( info, "bypass_part" ), "relay2click" );
    const std::string status = b.ask( "SYST:STAT?" );
    CHECK_EQ( field( status, "running" ), "0" );
    CHECK_EQ( field( status, "sink" ), "idle" );
    CHECK_EQ( field( status, "die_mc" ), "unknown" );
    CHECK_EQ( field( status, "pace" ), "timer" );
    CHECK_CONTAINS( b.ask( "SYST:LED?" ), "blanked=" );
    b.world().appVersion = "";
    CHECK_EQ( b.ask( "*IDN?" ), "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,-" );
}

TEST( "console", "SYSTem:INFormation? names the image by its ELF hash, after the image" ) {
    Bench             b;
    const std::string info = b.ask( "SYST:INF?" );
    CHECK_EQ( field( info, "elf" ), "5e1f0ba47c2291d3" );
    // A key keeps its place: the hash follows the image, and the newest
    // keys (0.18.0's pin map) come after it, the last token of the report.
    CHECK_CONTAINS( info, " image=meter elf=5e1f0ba47c2291d3 pins_src=" );
    CHECK( info.ends_with( " pins_table=0x90072FE9" ) );
    b.world().noAppDescription = true;
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "elf" ), "-" );
    CHECK_EQ( b.ask( "*IDN?" ), "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,-" );
}

TEST( "console", "the I2C clock: set, refused and failed" ) {
    Bench b;
    b.tell( "SYST:COMM:I2C:FREQ 1000000" );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "1000000" );
    b.tell( "SYST:COMM:I2C:FREQ 10" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "SYST:COMM:I2C:FREQ 2000000" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "SYST:COMM:I2C:FREQ 1.5" );
    CHECK_CONTAINS( b.errors(), "-1" );
    // Every rate of the range is the bus's own, set in place (FW-14).
    for ( const char* hz : { "50000", "137000", "400000", "1000000" } ) {
        b.tell( std::string( "SYST:COMM:I2C:FREQ " ) + hz );
        CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), hz );
        CHECK( b.world().i2c.sclHz() > 0 );
    }
    CHECK( b.clean() );
    // Refused by the bus's core: reported, and the clock left as it was.
    b.world().faults.failIpc = true;
    b.tell( "SYST:COMM:I2C:FREQ 400000" );
    b.world().faults.failIpc = false;
    CHECK_EQ( b.errors(), "-240,\"Hardware error;i2c ESP_ERR_INVALID_STATE\"" );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "1000000" );
}

TEST( "console", "a reset restores the defaults and releases the relays" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:FORM CSV;:SAMP:COUN 7;:SENS:CURR:RANG:IND 1;:CONF:VOLT" );
    b.tell( "ROUT:NULL ON", 300'000 );
    b.tell( "*RST", 400'000 );
    CHECK_EQ( b.ask( "SENS:PROF?;:FORM?;:SAMP:COUN?;:SENS:CURR:RANG:IND?;:CONF?" ),
              "TRACE;TEXT;100;0;\"CURR\"" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "k3" ), "spare:unwired" );
    CHECK( b.world().circuit.disconnects == 0 );
}

TEST( "console", "a reset closes an open capture and an open null without storing" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT" );
    b.tell( "*RST", 100'000 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "running" ), "0" );
    b.tell( "SENS:PROF SLEEP;:CAL:ZERO 5", 1'000'000 );
    b.tell( "*RST", 1'000'000 );
    CHECK_EQ( field( b.ask( "CAL:DATA?" ), "nulled_r0" ), "0" );
}

TEST( "console", "a missing INA228 is announced and its commands refused by name" ) {
    Bench b( []( sim::World& w ) { w.ina.present = false; } );
    CHECK( b.bringUp() == kleidos::meter::BringUpResult::NoDevice );
    const std::string errors = b.errors();
    CHECK_CONTAINS( errors, "-241,\"Hardware missing;no device (scan found none)\"" );
    b.tell( "SENS:PROF STATE" );
    CHECK_CONTAINS( b.errors(), "-241," );
    CHECK_EQ( b.ask( "*TST?" ), "1" );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "chip" ), "none" );
    b.tell( "*RST", 100'000 );
    CHECK( b.clean() );
}

TEST( "console", "a sampler and relays that did not start are announced" ) {
    Bench             b( []( sim::World& w ) {
        w.faults.failTask          = true;
        w.faults.failTaskName      = "meter-sample";
        w.faults.failGpioConfig    = true;
        w.faults.failGpioConfigPin = 7;
    } );
    const std::string errors = b.errors();
    CHECK_CONTAINS( errors, "107," );
    CHECK_CONTAINS( errors, "100," );
    CHECK_EQ( b.ask( "*TST?" ), "6" );
    b.tell( "SENS:PROF STATE;:INIT" );
    CHECK_CONTAINS( b.errors(), "107," );
    b.tell( "*RST", 100'000 );
    CHECK_EQ( field( b.ask( "SYST:INF?" ), "relays" ), "0" );
}

TEST( "console", "a console UART that will not come up leaves no console" ) {
    for ( int step = 0; step < 3; ++step ) {
        Bench b( [step]( sim::World& w ) {
            w.uart.refuseBaud = step == 0;
            if ( step == 1 ) {
                // The sampler's four semaphores come first (Sampler::begin()).
                w.faults.failSemaphore   = true;
                w.faults.failSemaphoreAt = 4;
            }
            if ( step == 2 ) {
                for ( sim::CpuLine& line : w.cpus[0].lines ) {
                    line.handler = +[]( void* ) {};  // every line of Core 0 taken
                }
            }
        } );
        CHECK( !b.consoleReady() );
    }
}

TEST( "console", "the console is the main task, raised to its priority, with no task of its own" ) {
    // Every task the firmware would create is refused: the sampler's fails,
    // and the console, which creates none (FW-17), still comes up and answers
    // on ESP-IDF's main task.
    Bench b( []( sim::World& w ) { w.faults.failTask = true; } );
    CHECK( b.consoleReady() );
    CHECK( !b.samplerReady() );
    CHECK_EQ( b.ask( "*IDN?" ), "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0" );
    CHECK( b.world().taskPriorities.at( "main" ) == 5U );
    CHECK( b.world().taskPriorities.count( "meter-console" ) == 0 );
    // Its stack is the one SYSTem:HEALth? reports as the console's.
    b.world().stackFreeByName["main"] = 4321;
    CHECK_EQ( field( b.ask( "SYST:HEAL?" ), "stack_console" ), "4321" );
}

TEST( "console", "a short write on the line is counted as a dropped sample" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT" );
    b.world().uart.txBudget = 10;  // the line carries ten bytes, then stalls
    b.world().run( 30'000 );
    b.world().resumeTx();
    b.drain();
    const std::string status = b.ask( "SYST:STAT?" );
    CHECK( std::stoi( field( status, "dropped" ) ) > 0 );
    b.tell( "ABOR" );
}

TEST( "console", "input before the announcement is discarded" ) {
    // Sent once the UART is up and before the announcement, which discards
    // it: the console's loop runs only after it (FW-17).
    Bench b( {}, true, 20'000, []( sim::World& w ) { w.send( "*IDN?" ); } );
    CHECK_EQ( b.drain(), "" );
    CHECK_EQ( b.ask( "*IDN?" ), "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0" );
}
