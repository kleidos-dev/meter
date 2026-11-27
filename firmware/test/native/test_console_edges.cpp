// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_console_edges.cpp
 * @brief The console's remaining decisions, one condition at a time: odd
 *        tokens, partial data, the diagnostic commands and the edges of a dump.
 */
#include "bench.h"
#include "unit.h"

#include <string>

using bench::Bench;
using bench::field;

namespace {

/// Sends @p message and runs the world until @p lines response lines (stream
/// records aside) have come or @p timeoutUs passed; returns them, newline
/// separated, and the moment the last arrived in @p at.
std::string collect( Bench& b, const std::string& message, int lines, sim::Micros timeoutUs,
                     sim::Micros* at = nullptr ) {
    b.world().send( message );
    std::string out;
    auto        count = [&] {
        int         n      = 0;
        std::size_t cursor = 0;
        std::size_t end    = 0;
        while ( ( end = out.find( '\n', cursor ) ) != std::string::npos ) {
            const std::string line = out.substr( cursor, end - cursor );
            if ( line.rfind( "METER ", 0 ) != 0 && line.rfind( "S,", 0 ) != 0 ) {
                ++n;
            }
            cursor = end + 1;
        }
        return n;
    };
    (void)b.world().runUntil(
        [&] {
            out += b.world().takeOutput();
            return count() >= lines;
        },
        timeoutUs );
    if ( at != nullptr ) {
        *at = b.world().now();
    }
    return out;
}

}  // namespace

TEST( "edges", "DIAGnostic: alert, scan and registers, with their refusals" ) {
    Bench             b;
    const std::string alert = b.ask( "DIAG:ALER?" );
    CHECK_EQ( field( alert, "memstat" ), "1" );
    CHECK_EQ( field( alert, "cnvrf" ), "1" );
    CHECK_EQ( b.ask( "DIAG:I2C:SCAN?" ), "expected=0x40 found=0x40 n=1" );
    CHECK_EQ( b.ask( "DIAG:REG? #H3E" ), "addr=0x3E bytes=2 raw=0x5449" );
    CHECK_EQ( field( b.ask( "DIAG:REG? #H0A" ), "bytes" ), "5" );
    b.tell( "DIAG:REG? #H100;:DIAG:REG? x;:DIAG:REG? #H20" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
    b.tell( "*CLS" );
    b.world().ina.failCount = 1;
    b.tell( "DIAG:REG? #H00" );
    b.world().ina.failCount = 1;
    b.tell( "DIAG:ALER?" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "2" );
    b.world().ina.present = false;
    CHECK_EQ( b.ask( "DIAG:I2C:SCAN?" ), "expected=0x40 found=- n=0" );
}

TEST( "edges", "a scan from the console finds the part alone and leaves it readable" ) {
    // 0.16.4: the console is on Core 0 and the I2C master's interrupt on
    // Core 1. Probed from Core 0, every silent address left its STOP's
    // completion latched, and the next transaction took it for its own: the
    // bench listed 0x40-0x4F at random, then read every register as 0x0000
    // with no error, at 400, 100 and 50 kHz (2026-11-21).
    Bench b;
    for ( const char* hz : { "400000", "100000", "50000", "400000" } ) {
        b.tell( std::string( "SYST:COMM:I2C:FREQ " ) + hz );
        for ( int i = 0; i < 5; ++i ) {
            CHECK_EQ( b.ask( "DIAG:I2C:SCAN?" ), "expected=0x40 found=0x40 n=1" );
            CHECK_EQ( b.ask( "DIAG:REG? #H3E" ), "addr=0x3E bytes=2 raw=0x5449" );
            CHECK_EQ( b.ask( "DIAG:REG? #H3F" ), "addr=0x3F bytes=2 raw=0x2281" );
        }
    }
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "0" );
    CHECK( b.world().ina.probes > 0 );
    CHECK( b.world().i2c.offCoreCalls == 0 );
    CHECK( b.clean() );
}

TEST( "edges", "a bus the bus's core cannot take is left alone, and so reported" ) {
    // 0.16.5: no transaction is made off the bus's core. Refused there, the
    // scan probes nothing and a register read reads nothing: both say so.
    Bench     b;
    const int probes         = b.world().ina.probes;
    const int diagReads      = b.world().ina.diagReads;
    b.world().faults.failIpc = true;
    CHECK_EQ( b.ask( "DIAG:I2C:SCAN?" ), "expected=0x40 found=- n=0" );
    CHECK( b.world().ina.probes == probes );
    b.tell( "DIAG:REG? #H3E" );
    b.tell( "DIAG:ALER?" );
    CHECK( b.world().ina.diagReads == diagReads );
    CHECK_EQ( b.errors(),
              "-240,\"Hardware error;i2c ESP_ERR_INVALID_STATE\","
              "-240,\"Hardware error;i2c ESP_ERR_INVALID_STATE\"" );
    b.world().faults.failIpc = false;
    CHECK_EQ( b.ask( "DIAG:I2C:SCAN?" ), "expected=0x40 found=0x40 n=1" );
    CHECK_EQ( b.ask( "DIAG:REG? #H3E" ), "addr=0x3E bytes=2 raw=0x5449" );
}

TEST( "edges", "a failed read from the console leaves the next one right" ) {
    // 0.16.5: a NACK the console met from Core 0 left its STOP's completion
    // latched, as a probe's did up to 0.16.3, and the next transaction took it
    // for its own: ESP_OK at once and zeros (`docs/architecture.md`
    // SS 2.2). Each path the console reads by, after a failure.
    Bench b;
    for ( int i = 0; i < 3; ++i ) {
        b.world().ina.failCount = 1;
        b.tell( "DIAG:REG? #H3E" );
        CHECK_EQ( b.ask( "DIAG:REG? #H3E" ), "addr=0x3E bytes=2 raw=0x5449" );
        b.world().ina.failCount = 1;
        b.tell( "DIAG:ALER?" );
        CHECK_EQ( field( b.ask( "DIAG:ALER?" ), "memstat" ), "1" );
        b.world().ina.failCount = 1;
        b.tell( "SENS:CHAR?" );
        CHECK_EQ( b.ask( "DIAG:REG? #H3F" ), "addr=0x3F bytes=2 raw=0x2281" );
        b.world().ina.present = false;
        b.tell( "DIAG:REG? #H02" );
        b.world().ina.present = true;
        CHECK_EQ( b.ask( "*TST?", 500'000 ), "0" );
    }
    CHECK_CONTAINS( b.errors(), "-240," );
    CHECK( b.world().i2c.offCoreCalls == 0 );
}

TEST( "edges", "every console path makes its transactions on the bus's core" ) {
    // The console runs on Core 0 and the bus is Core 1's: every command that
    // touches the bus, and every capture's setup and close, has its
    // transactions made on Core 1 (0.16.5; the own master refuses any other
    // since FW-14). The Bench checks the register calls at the end of every
    // case; this one walks each path by name.
    Bench b;
    CHECK_EQ( b.ask( "DIAG:REG? #H3E" ), "addr=0x3E bytes=2 raw=0x5449" );
    CHECK_EQ( field( b.ask( "DIAG:ALER?" ), "memstat" ), "1" );
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "0" );
    CHECK_EQ( b.ask( "DIAG:I2C:SCAN?" ), "expected=0x40 found=0x40 n=1" );
    b.tell( "SENS:CURR:RANG:IND 1", 100'000 );
    b.tell( "SENS:CURR:RANG:IND 0", 100'000 );
    // The switch back cleared the accumulator, which has counted the 50 mA
    // for the 100 ms since: 5 mC, 1,600 counts of the wide range's 3.125 uC.
    CHECK_EQ( field( b.ask( "SENS:CHAR?" ), "raw" ), "1600" );
    b.tell( "SENS:CHAR:RES" );
    b.tell( "SENS:PROF STATE;:INIT", 100'000 );
    b.tell( "ABOR", 100'000 );
    b.tell( "SENS:PROF TRACE;:INIT", 100'000 );
    b.tell( "ABOR", 100'000 );
    CHECK_EQ( b.ask( "MEAS:CURR?", 2'000'000 ), "0.05000625" );  // 50 mA and the offset
    b.tell( "ROUT:NULL ON", 300'000 );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "bypass" );
    b.tell( "SENS:PROF STATE;:CAL:ZERO 1", 1'500'000 );
    b.tell( "ABOR", 100'000 );
    // The bypass carries the DUT: the shunt sees the 0.48 uV offset alone,
    // two counts of 312.5 nV, from one second of STATE's 868 samples.
    const std::string zero = b.ask( "CAL:ZERO:RES?" );
    CHECK_EQ( field( zero, "offset_pv" ), "625000" );
    CHECK_EQ( field( zero, "n" ), "868" );
    CHECK_EQ( field( zero, "path" ), "bypass" );
    CHECK_EQ( field( zero, "stored" ), "1" );
    b.tell( "SYST:COMM:I2C:FREQ 100000", 50'000 );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "100000" );
    b.tell( "*RST", 400'000 );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "400000" );
    CHECK( b.clean() );
    CHECK( b.world().i2c.offCoreCalls == 0 );
}

TEST( "edges", "tags and labels: every unsafe character, quotes of both kinds" ) {
    Bench b;
    b.tell( "CAL:TAG 'single'" );
    CHECK_EQ( b.ask( "CAL:TAG?" ), "\"single\"" );
    const char* unsafe[] = { "\"\"", "\"a=b\"", "\"a,b\"", "\"a;b\"", "\"a\"\"b\"", "\"a'b\"" };
    for ( const char* tag : unsafe ) {
        b.tell( std::string( "*CLS;CAL:TAG " ) + tag, 20'000 );
        CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "1" );
    }
    // A byte outside printable ASCII never reaches the tag's own check: the
    // whole message is discarded with -101, *CLS included.
    b.tell( "*CLS" );
    b.tell( std::string( "*CLS;CAL:TAG \"a" ) + '\x7f' + "b\"", 20'000 );
    CHECK_EQ( b.errors(),
              "-101,\"Invalid character;byte 0x7F is not printable ASCII (program "
              "message discarded)\"" );
    CHECK_EQ( b.ask( "CAL:TAG?" ), "\"single\"" );
}

TEST( "edges", "a line that fails outright counts as dropped" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT" );
    b.world().stallTx();  // the line takes nothing
    b.world().run( 20'000 );
    b.world().resumeTx();
    b.drain();
    CHECK( std::stoi( field( b.ask( "SYST:STAT?" ), "dropped" ) ) > 0 );
    b.tell( "ABOR" );
}

TEST( "edges", "channels a sample did not convert are left out of the stream" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:SENS:FUNC:ON \"CURR\",\"VOLT\",\"POW\",\"TEMP\";:INIT" );
    b.world().ina.failRegister = kleidos::meter::ina228::kRegVBus;
    b.world().ina.failCount    = 5;
    b.world().run( 20'000 );
    b.world().ina.failRegister = kleidos::meter::ina228::kRegDieTemp;
    b.world().ina.failCount    = 5;
    b.world().run( 20'000 );
    b.world().ina.failRegister = -1;
    const std::string out      = b.drain();
    CHECK_CONTAINS( out, "ch=i_ua" );
    CHECK_CONTAINS( out, "ch=p_mw" );
    b.tell( "ABOR" );
}

TEST( "edges", "status enables refuse a word they cannot parse" ) {
    Bench b;
    b.tell( "STAT:QUES:ENAB x;:STAT:QUES:ENAB" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "2" );
}

TEST( "edges", "the null-path check's restore failing after a good reading" ) {
    Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
    b.world().ina.failAfter = 2;  // the check's word, the reading, then the restore fails
    b.tell( "ROUT:NULL ON", 400'000 );
    b.world().ina.failAfter = -1;
    CHECK_CONTAINS( b.errors(), "nothing moved" );
    // An order with nothing to move under a capture is answered, not refused.
    b.tell( "ROUT:NULL ON", 400'000 );
    b.tell( "ROUT:NULL ON", 50'000 );  // already on: nothing to check
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "moved" ), "-" );
    b.tell( "INIT:FORC;:ROUT:REL3 OFF,LIVE", 100'000 );
    CHECK_EQ( field( b.ask( "ROUT:RES?" ), "live" ), "0" );
    b.tell( "ABOR" );
    // A null through the relays that read clean is stored.
    b.world().ina.offsetPv = 300'000;
    b.tell( "SENS:PROF SLEEP;:CAL:ZERO 1", 2'000'000 );
    b.tell( "ABOR", 300'000 );
    CHECK_EQ( field( b.ask( "CAL:ZERO:RES?" ), "stored" ), "1" );
    CHECK_EQ( field( b.ask( "CAL:ZERO:RES?" ), "path" ), "bypass" );
    b.tell( "ROUT:NULL OFF", 300'000 );
}

TEST( "edges", "relays that failed at boot leave the hold unarmed through a capture" ) {
    Bench b( []( sim::World& w ) {
        w.faults.failGpioConfig    = true;
        w.faults.failGpioConfigPin = 17;
    } );
    b.tell( "SENS:PROF STATE;:INIT" );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "running" ), "0" );
}

TEST( "edges", "range and dump keywords in odd cases and odd characters" ) {
    Bench b( []( sim::World& w ) { w.ringSamples = 4096; } );
    b.tell( "SENS:CURR:RANG max" );
    CHECK_EQ( b.ask( "SENS:CURR:RANG:IND?" ), "0" );
    b.tell( "*CLS;SENS:CURR:RANG [1;:SENS:CURR:RANG {1;:SENS:CURR:RANG _1" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
    b.tell( "*CLS;SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:INIT;:TRAC:MARK early", 2'000'000 );
    // A ring profile is paced by the timer without a probe, and says so.
    const std::string header = b.ask( "FETC:HEAD?" );
    CHECK_EQ( field( header, "pace" ), "timer" );
    CHECK_EQ( field( header, "pace_edges" ), "0" );
    CHECK_EQ( field( header, "pace_expect" ), "0" );
    b.tell( "ABOR" );
    b.drain();
    b.world().send( "TRAC:DATA?" );  // the whole ring, scaled
    b.world().run( 3'000'000 );
    CHECK_CONTAINS( b.drain(), "METER_DUMP count=4096" );
    b.world().send( "TRAC:DATA? raw;:TRAC:DATA? RAW,100,3" );
    b.world().run( 3'000'000 );
    const std::string dump = b.drain();
    CHECK_CONTAINS( dump, "fmt=raw20" );
    CHECK_CONTAINS( dump, "A n=0" );  // the anchors before a dump's start
    b.world().send( "TRAC:DATA? SCAL,100,5" );
    b.world().run( 200'000 );
    CHECK( b.drain().find( "label=early" ) == std::string::npos );
    // A mark later in the ring than the rows before it.
    b.tell( "INIT", 20'000 );
    b.tell( "TRAC:MARK late", 2'000'000 );
    b.tell( "ABOR" );
    b.drain();
    b.world().send( "TRAC:DATA? RAW,0,400" );
    b.world().run( 500'000 );
    CHECK_CONTAINS( b.drain(), "label=late" );
    b.tell( "*CLS;TRAC:DATA? 0,x;:TRAC:DATA? [;:TRAC:DATA? {" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "3" );
}

TEST( "edges", "a reset after INITiate leaves the closed window's own sums behind it" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 100'000 );
    b.tell( "*RST", 300'000 );
    // The sums are the sampler's (0.10.0), from the window the reset closed:
    // the DUT's voltage is the 3.9 V bus less the burden.
    CHECK_CONTAINS( b.ask( "FETC:VOLT?" ), "3.89" );
    CHECK_EQ( b.ask( "FETC:TEMP?" ), "25.0" );  // the bench's die
    CHECK( b.clean() );
}

TEST( "edges", "a TRACE measurement bounded by its ring, and a CHARGE past the correction" ) {
    Bench b( []( sim::World& w ) { w.ringSamples = 4096; } );
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:SAMP:COUN 10000" );
    // The ring is the sampler's from boot (0.7.0), so the first capture is
    // already capped at it: SAMP:COUN 10000 takes the 4096 the ring holds.
    CHECK_EQ( b.ask( "MEAS:CURR?", 5'000'000 ), "0.05000625" );
    CHECK_EQ( b.errors(), "0,\"No error\"" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "samples" ), "4096" );
    // A window of 2^37 counts, either way: past the int64 room of the shunt
    // correction, so the nominal figure is reported uncorrected.
    b.tell( "SENS:PROF STATE;:SAMP:COUN 5" );
    for ( const std::int64_t jump : { 1LL << 37, -( 1LL << 37 ) } ) {
        b.world().ina.chargeCounts = 1LL << 38;
        b.world().ina.chargeJump   = jump;
        CHECK_EQ( b.ask( "MEAS:CURR?", 2'000'000 ), "0.05000625" );
        // 2^37 counts of the 3.125 uC LSB, uncorrected: 429,496.7296 C.
        CHECK_EQ( b.ask( "FETC:CHAR?" ), jump > 0 ? "429496.7296" : "-429496.7296" );
    }
    b.world().ina.chargeCounts = -1;
    b.world().ina.chargeJump   = 0;
}

// --- Bounds, ABORt and the line (0.6.0) -------------------------------------

TEST( "edges", "an acquisition longer than its window is refused; SAMPle:COUNt is not" ) {
    Bench b;
    b.tell( "SENS:PROF SLEEP;:SAMP:COUN 1000000" );
    CHECK( b.clean() );
    CHECK_EQ( b.ask( "MEAS:CURR?" ), "" );
    CHECK_EQ( b.errors(),
              "-222,\"Data out of range;acquisition of 1000000 x 67520 us exceeds 600 s "
              "(lower SAMPle:COUNt, or INITiate a capture)\"" );
    // STATE at the edge: 520834 periods of 1152 us are over the window.
    b.tell( "SENS:PROF STATE;:SAMP:COUN 520834" );
    CHECK_EQ( b.ask( "READ?" ), "" );
    CHECK_CONTAINS( b.errors(), "-222,\"Data out of range;acquisition of 520834 x 1152 us" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "running" ), "0" );
}

TEST( "edges", "ABORt ends a running acquisition within one period, and only it" ) {
    Bench b;
    b.tell( "SENS:PROF SLEEP;:SAMP:COUN 444" );  // 30 s of 67.52 ms periods
    b.world().send( "MEAS:CURR?;:SYST:ERR:ALL?" );
    b.world().run( 1'000'000 );
    const sim::Micros sent = b.world().now();
    sim::Micros       at   = 0;
    const std::string out  = collect( b, "ABOR;:SYST:ERR:ALL?", 2, 2'000'000, &at );
    CHECK( at - sent < 67'520 );  // inside one SLEEP period
    CHECK_CONTAINS( out, "-230,\"Data corrupt or stale;the acquisition was aborted after " );
    CHECK_CONTAINS( out, " of 444 samples (ABORt)\"\n0,\"No error\"\n" );
    // The ABORt unit was taken by the acquisition: nothing is left to refuse.
    CHECK( b.clean() );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "running" ), "0" );
}

TEST( "edges", "an acquisition reads only ABORt; what came before it runs after, in order" ) {
    Bench b;
    b.tell( "SENS:PROF SLEEP;:SAMP:COUN 444" );
    b.world().send( "MEAS:CURR?" );
    b.world().run( 1'000'000 );  // past the pace probe: acquiring
    // A partial line is no message yet, and these are not ABORt.
    b.world().sendRaw( "A" );
    b.world().run( 200'000 );
    b.world().sendRaw( "B" );
    const char* notAbort[] = { "OR?",     "",        "  ",     ":",          "*IDN?",
                               "ABORTED", "ABORt 1", "ABOR:X", ":SYST:STAT?" };
    for ( const char* line : notAbort ) {
        b.world().send( line );
        b.world().run( 100'000 );
    }
    CHECK_EQ( field( b.ask( "SYST:STAT?", 100'000 ), "running" ), "" );  // still deaf
    const std::string out = collect( b, " \t:abort \t", 3, 2'000'000 );
    // The acquisition answered nothing; ABOR? and the rest ran after it.
    CHECK_CONTAINS( out, "KLEIDOS,KLEIDOS-METER," );
    CHECK_CONTAINS( out, "running=0" );
    CHECK_CONTAINS( b.errors(), "-230," );
    b.world().send( "SENS:PROF STATE;:SAMP:COUN 30000;:MEAS:CURR?" );
    b.world().run( 100'000 );
    b.world().sendRaw( "ABOR\r" );
    b.world().run( 50'000 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "running" ), "0" );
    CHECK_CONTAINS( b.errors(), "aborted after" );
}

TEST( "edges", "a sampler that does not acknowledge a stop: 107, then it starts again" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    b.world().ina.stallNextUs = 2'000'000;
    b.world().run( 5'000 );
    b.drain();
    const std::string abort = collect( b, "ABOR;:SYST:ERR:ALL?", 1, 1'000'000 );
    CHECK_CONTAINS( abort, "107,\"Sampler not running;the sampler did not acknowledge the stop" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "sink" ), "idle" );
    b.tell( "INIT" );
    CHECK_CONTAINS( b.errors(), "107,\"Sampler not running;sampler engine has not acknowledged" );
    b.world().run( 2'000'000 );
    b.tell( "INIT", 20'000 );
    b.tell( "ABOR" );
    CHECK( b.clean() );
    CHECK( std::stoi( field( b.ask( "FETC:STAT?" ), "samples" ) ) > 0 );
    // A null window the same way: closed with no result.
    b.tell( "SENS:PROF SLEEP;:CAL:ZERO 5", 1'500'000 );  // past the pace probe
    b.world().ina.stallNextUs = 2'000'000;
    b.world().run( 100'000 );
    b.tell( "ABOR", 1'000'000 );
    CHECK_CONTAINS( b.errors(), "107,\"Sampler not running;the sampler did not acknowledge" );
    b.world().run( 2'000'000 );
    b.tell( "CAL:ZERO 1", 2'000'000 );
    b.tell( "ABOR" );
    CHECK( b.clean() );
    // An acquisition that cannot stop is the same failure.
    b.tell( "SENS:PROF STATE;:SAMP:COUN 30000" );
    b.world().send( "MEAS:CURR?" );
    b.world().run( 100'000 );
    b.world().ina.stallNextUs = 3'000'000;
    b.world().run( 5'000 );
    b.tell( "ABOR", 1'000'000 );
    CHECK_CONTAINS( b.errors(), "107,\"Sampler not running;the acquisition did not stop" );
    b.world().run( 3'000'000 );
}

TEST( "edges", "a finished window refuses what its closing reads rest on until ABORt" ) {
    Bench b( []( sim::World& w ) { w.ringSamples = 4096; } );
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:INIT" );
    b.world().run( 2'000'000 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "running" ), "0" );  // full, awaiting ABORt
    b.tell( "SENS:CHAR:RES;:SENS:CHAR?;:DIAG:ALER?;:SENS:CURR:RANG:IND 1;:SENS:PROF STAT" );
    const std::string refused = b.errors();
    CHECK_CONTAINS( refused, "-221,\"Settings conflict;capture ended, not closed (ABORt first)\"" );
    CHECK( bench::units( refused ).size() == 1U );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "0" );
    b.tell( "*CLS;SENS:CHAR:RES;:SENS:CHAR?;:DIAG:ALER?;:SENS:CURR:RANG:IND 1;:SENS:PROF STAT" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "5" );
    b.tell( "*CLS;:ABOR;:SENS:CHAR:RES" );
    CHECK( b.clean() );
    // A null window that ended by itself: 112 until it is closed.
    b.tell( "SENS:PROF SLEEP;:CAL:ZERO 1", 2'000'000 );
    b.tell( "SENS:CHAR:RES" );
    CHECK_EQ( b.errors(),
              "112,\"Null window running;null window ended, not closed (ABORt first)\"" );
    b.tell( "ABOR" );
}

TEST( "edges", "a byte outside printable ASCII discards its message with -101" ) {
    Bench b;
    b.world().send( std::string( "\x01*IDN?" ) );
    b.world().run( 50'000 );
    CHECK_EQ( b.drain(), "" );
    CHECK_EQ( b.errors(),
              "-101,\"Invalid character;byte 0x01 is not printable ASCII (program "
              "message discarded)\"" );
    b.world().send( std::string( "*IDN?\x80\x7f" ) );
    b.world().run( 50'000 );
    CHECK_CONTAINS( b.errors(), "byte 0x80 is not" );
    // A tab is whitespace, and the line after a discarded one is whole.
    CHECK_EQ( b.ask( "SENS:PROF\tSTAT;:SENS:PROF?" ), "STATE" );
    CHECK( b.clean() );
}

TEST( "edges",
      "the receive ring and the FIFO behind it hold 1,152 bytes the console has not read" ) {
    // A burst the console cannot read while it comes: the receive ring takes
    // 1,024 bytes, then its interrupt leaves the rest in the hardware FIFO,
    // which takes 128 more. 1,151 and a newline fit: nothing is lost, and the
    // line is only too long.
    Bench b;
    b.world().send( std::string( 1151, 'x' ) );
    b.world().run( 50'000 );
    CHECK( b.world().uart.lostBytes == 0 );
    CHECK_EQ( b.errors(), "-363,\"Input buffer overrun;program message longer than 255 bytes\"" );
}

TEST( "edges", "input lost to a full receive FIFO is -363 once, and its message is discarded" ) {
    // One byte more and the newline finds the FIFO full: lost, the overflow
    // latched and the FIFO emptied, as ESP-IDF's driver emptied it. The line
    // that lost it ends at the next newline, and is discarded with it.
    Bench b;
    b.world().send( std::string( 1152, 'x' ) );
    b.world().run( 50'000 );
    CHECK( b.world().uart.lostBytes == 1 );
    CHECK( b.world().uart.rxFifoResets == 2 );  // begin()'s, and the overflow's
    b.world().send( "*IDN?" );  // ends the line that lost its newline: discarded with it
    b.world().run( 50'000 );
    CHECK_EQ( b.drain(), "" );
    CHECK_EQ( b.errors(),
              "-363,\"Input buffer overrun;receive overrun: input lost while the "
              "console was busy (message discarded)\"" );
    CHECK_CONTAINS( b.ask( "*IDN?" ), "KLEIDOS," );
    // A longer burst loses more, and still answers one error: the overflow is
    // one latch the console takes once.
    b.world().send( std::string( 1500, 'x' ) );
    b.world().run( 50'000 );
    CHECK( b.world().uart.lostBytes > 1 );
    CHECK_EQ( b.errors(),
              "-363,\"Input buffer overrun;receive overrun: input lost while the "
              "console was busy (message discarded)\"" );
    CHECK_CONTAINS( b.ask( "*IDN?" ), "KLEIDOS," );
}

TEST( "edges", "two overflows inside one discarded message queue one -363" ) {
    // A dump reads nothing from the line for as long as it runs and looks
    // for a loss at every row: a host that keeps sending overflows the FIFO
    // again and again, and all of it falls in one message, discarded once.
    Bench b( []( sim::World& w ) { w.ringSamples = 4096; } );
    b.tell( "SENS:PROF TRAC;:INIT", 1'500'000 );
    b.tell( "ABOR" );
    REQUIRE( b.clean() );
    b.world().clockReadUs   = 20;  // a dump of 4,096 rows that lasts 80 ms or more
    const sim::Micros start = b.world().now();
    for ( const sim::Micros atUs : { start + 10'000, start + 40'000 } ) {
        const std::size_t index = b.world().addInterrupt( {} );
        sim::Interrupt&   once  = b.world().interrupt( index );
        once.handler            = [&b, index] {
            b.world().interrupt( index ).enabled = false;
            b.world().sendRaw( std::string( 1300, 'x' ) );
        };
        once.enabled = true;
        once.period  = sim::kForever / 4;
        once.next    = atUs;
    }
    b.world().send( "TRAC:DATA? SCAL" );
    b.world().run( 2'000'000 );
    b.world().clockReadUs = 1;
    CHECK( b.world().uart.lostBytes > 1 );
    CHECK_CONTAINS( b.drain(), "METER_DUMP_END" );
    b.world().send( "" );  // ends the message the losses fell in
    b.world().run( 50'000 );
    CHECK_EQ( b.errors(),
              "-363,\"Input buffer overrun;receive overrun: input lost while the "
              "console was busy (message discarded)\"" );
}

TEST( "edges", "*RST rewrites the I2C clock only when it is not 400 kHz" ) {
    Bench       b;
    const int   before = b.world().i2c.timingWrites;
    std::size_t ledger = b.world().idfLedger.size();
    b.tell( "*RST" );
    CHECK( b.world().i2c.timingWrites == before );
    b.tell( "SYST:COMM:I2C:FREQ 100000;:*RST" );
    CHECK( b.world().i2c.timingWrites == before + 2 );
    CHECK_EQ( b.ask( "SYST:COMM:I2C:FREQ?" ), "400000" );
    CHECK( b.world().i2c.sclHz() == 400000 );
    // Neither asked ESP-IDF for anything that allocates (FW-14).
    CHECK( b.world().idfLedger.size() == ledger );
}

TEST( "edges", "an error's detail never carries a byte outside printable ASCII" ) {
    kleidos::meter::scpi::ErrorQueue queue;
    queue.push( -113,
                "a\x01"
                "b\x7f"
                "\"c" );
    CHECK_EQ( std::string( queue.pop().detail ), "a?b?'c" );
}

TEST( "edges", "branches: the inbox keeps what an acquisition reads, full or not" ) {
    Bench b;
    b.tell( "SENS:PROF SLEEP;:SAMP:COUN 40" );  // 2.7 s, past the pace probe
    // Two lines in one burst: the second is still unread when the acquisition
    // reads more, so the inbox moves it to its front.
    b.world().send( "MEAS:CURR?\n*IDN?" );
    b.world().run( 1'000'000 );
    // More than the inbox holds arrives while it runs: the rest waits in the
    // receive ring, nothing is lost, and all of it runs after, in order.
    std::string burst;
    for ( int i = 0; i < 30; ++i ) {
        burst += ( i == 0 ? "" : "\n" ) + std::string( "*OPC;*OPC;*OPC;*OPC" );
    }
    for ( int round = 0; round < 3; ++round ) {
        b.world().send( burst );
        b.world().run( 100'000 );
    }
    b.world().run( 3'000'000 );
    const std::string out = b.drain();
    CHECK_CONTAINS( out, "\n" );  // the measurement's own answer
    CHECK_CONTAINS( out, "KLEIDOS,KLEIDOS-METER," );
    CHECK( b.world().uart.lostBytes == 0 );
    CHECK( b.clean() );
}

TEST( "edges", "a timer-paced loop that came a period late re-bases, and says so" ) {
    Bench b;
    b.tell( "SENS:PROF TRAC;:INIT", 20'000 );
    b.world().ina.stallNextUs = 5'000;  // one read holds the bus ~18 periods
    b.world().run( 20'000 );
    b.tell( "ABOR" );
    const std::string stats = b.ask( "FETC:STAT?" );
    CHECK_EQ( field( stats, "rebased" ), "1" );
    // No catch-up burst: the samples stay one per period of the span, less
    // the ones the stall swallowed.
    const auto samples  = std::stoll( field( stats, "samples" ) );
    const auto duration = std::stoll( field( stats, "dur_us" ) );
    CHECK( samples * 280 <= duration + 280 );
    CHECK( b.clean() );
    // An undisturbed capture re-bases nothing.
    b.tell( "INIT", 20'000 );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "rebased" ), "0" );
}
