// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_trigger.cpp
 * @brief 0.11.0 over the line: the level trigger with its pre-trigger in a
 *        circular ring, threshold marks and `TRACe:MARK` in conversion order,
 *        a capture that ends by itself with `*OPC`, and the dump's CRC-32.
 */
#include "bench.h"
#include "crc32.h"
#include "unit.h"

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <sstream>
#include <string>
#include <vector>

using bench::Bench;
using bench::field;

namespace {

/// The lines of a block of output, without their newlines.
std::vector<std::string> lines( const std::string& text ) {
    std::vector<std::string> out;
    std::istringstream       in( text );
    std::string              line;
    while ( std::getline( in, line ) ) {
        out.push_back( line );
    }
    return out;
}

/// Reads a `TRACe:DATA?` block whole: the response's block, from `#`.
std::string dump( Bench& b, const std::string& query ) {
    b.drain();
    b.world().send( query );
    b.world().run( 3'000'000 );
    const std::string out   = b.drain();
    const std::size_t start = out.find( '#' );
    if ( start == std::string::npos ) {
        return {};
    }
    const std::size_t digits = static_cast<std::size_t>( out[start + 1] - '0' );
    const std::size_t length = std::stoul( out.substr( start + 2, digits ) );
    return out.substr( start + 2 + digits, length );
}

/// A DUT that draws 1 mA and 100 mA for 1 ms at @p atUs.
bench::Bench::Setup burstAt( sim::Micros atUs ) {
    return [atUs]( sim::World& w ) {
        w.ringSamples     = 4096;
        w.circuit.dutNaAt = [atUs]( sim::Micros now ) -> std::int64_t {
            return ( now >= atUs && now < atUs + 1000 ) ? 100'000'000 : 1'000'000;
        };
    };
}

}  // namespace

TEST( "trigger", "the trigger, limit and mark settings: set, query, bounds, reset" ) {
    Bench b( []( sim::World& w ) { w.ringSamples = 4096; } );
    CHECK_EQ( b.ask( "TRIG:SOUR?;:TRIG:LEV?;:TRIG:SLOP?;:TRIG:PRET?" ), "IMM;0.0;POS;0" );
    b.tell( "TRIG:SOUR LEV;:TRIG:LEV 0.05;:TRIG:SLOP NEG;:TRIG:PRET 1000" );
    CHECK_EQ( b.ask( "TRIG:SOUR?;:TRIG:LEV?;:TRIG:SLOP?;:TRIG:PRET?" ), "LEV;0.05;NEG;1000" );
    b.tell( "TRIG:LEV -0.01;:TRIG:SLOP POS" );
    CHECK_EQ( b.ask( "TRIG:LEV?;:TRIG:SLOP?" ), "-0.01;POS" );
    CHECK( b.clean() );
    b.tell( "TRIG:LEV 2.0" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "TRIG:LEV -2.0" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "TRIG:LEV HIGH" );
    CHECK_CONTAINS( b.errors(), "-1" );
    b.tell( "TRIG:SLOP UP" );
    CHECK_CONTAINS( b.errors(), "-224," );
    b.tell( "TRIG:PRET 4096" );
    CHECK_CONTAINS( b.errors(), "-222,\"Data out of range;use TRIGger:PRETrigger <0..4095>\"" );
    b.tell( "TRIG:PRET x" );
    CHECK_CONTAINS( b.errors(), "-1" );
    b.tell( "TRIG:SOUR BUS" );
    CHECK_CONTAINS( b.errors(), "-224," );

    CHECK_EQ( b.ask( "SENS:CAPT:LIM?;:SENS:CURR:THR:MARK?" ), "0;0" );
    b.tell( "SENS:CAPT:LIM ON;:SENS:CURR:THR:MARK ON" );
    CHECK_EQ( b.ask( "SENS:CAPT:LIM?;:SENS:CURR:THR:MARK?" ), "1;1" );
    b.tell( "SENS:CAPT:LIM MAYBE" );
    CHECK_CONTAINS( b.errors(), "-224," );
    b.tell( "SENS:CURR:THR:MARK 7" );
    CHECK( !b.clean() );
    b.tell( "TRIG:SOUR IMM;:SENS:PROF STAT;:INIT" );
    b.tell( "TRIG:LEV 0;:TRIG:SLOP POS;:TRIG:PRET 0;:SENS:CAPT:LIM OFF;:SENS:CURR:THR:MARK OFF" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "5" );
    b.tell( "ABOR;*CLS;*RST" );
    CHECK_EQ( b.ask( "TRIG:SOUR?;:TRIG:LEV?;:TRIG:SLOP?;:TRIG:PRET?;:SENS:CAPT:LIM?;"
                     ":SENS:CURR:THR:MARK?" ),
              "IMM;0.0;POS;0;0;0" );
}

TEST( "trigger", "a level trigger keeps its pre-trigger samples and ends the capture itself" ) {
    constexpr sim::Micros kBurstUs = 2'000'000;
    Bench                 b( burstAt( kBurstUs ) );
    b.tell(
        "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:TRIG:SOUR LEV;:TRIG:LEV 0.05;"
        ":TRIG:PRET 1000;:INIT",
        10'000 );
    const std::string header = b.ask( "FETC:HEAD?" );
    CHECK_EQ( field( header, "trig_source" ), "level" );
    CHECK_EQ( field( header, "trig_level_na" ), "50000000" );
    CHECK_EQ( field( header, "trig_slope" ), "pos" );
    CHECK_EQ( field( header, "trig_pre" ), "1000" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "trig" ), "armed" );
    // Before the burst the 4096-sample ring has wrapped about twice.
    b.world().run( 1'900'000 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "running" ), "1" );
    // The burst, then the 3096 post-trigger samples (0.87 s at 280 us).
    b.world().run( 1'200'000 );
    const std::string status = b.ask( "SYST:STAT?" );
    CHECK_EQ( field( status, "running" ), "0" );
    CHECK_EQ( field( status, "trig" ), "fired" );
    CHECK_EQ( b.ask( "TRAC:POIN?" ), "4096" );

    const std::string block = dump( b, "TRAC:DATA? RAW" );
    const auto        rows  = lines( block );
    CHECK( !rows.empty() );
    const std::string open  = rows.front();
    const long        trigN = std::stol( field( open, "trig_n" ) );
    const long        first = std::stol( field( open, "first" ) );
    const long long   t0    = std::stoll( field( open, "t0_us" ) );
    CHECK( first == trigN - 1000 );
    // The implicit timestamp of the trigger sample is the burst's.
    const long long trigT = t0 + static_cast<long long>( trigN ) * 280;
    CHECK( std::llabs( trigT - kBurstUs ) < 3 * 280 );
    // The rows start at the first held index and run on without a gap.
    long expect     = first;
    bool sawTrigger = false;
    for ( const std::string& row : rows ) {
        if ( row.rfind( "R n=", 0 ) == 0 ) {
            CHECK( std::stol( row.substr( 4 ) ) == expect );
            expect += 16;
        } else if ( row.rfind( "T n=", 0 ) == 0 ) {
            CHECK( std::stol( row.substr( 4 ) ) == trigN );
            sawTrigger = true;
        } else if ( row.rfind( "A n=", 0 ) == 0 ) {
            const long index = std::stol( row.substr( 4 ) );
            CHECK( index >= first && index < first + 4096 );
            // An anchor's real time agrees with the implicit one.
            const long long t = std::stoll( field( row, "t" ) );
            CHECK( std::llabs( t - ( t0 + static_cast<long long>( index ) * 280 ) ) < 3 * 280 );
        }
    }
    CHECK( sawTrigger );
    CHECK( expect == first + 4096 );

    b.tell( "ABOR" );
    const std::string stats = b.ask( "FETC:STAT?" );
    CHECK_EQ( field( stats, "trig_n" ), std::to_string( trigN ) );
    CHECK( std::llabs( std::stoll( field( stats, "trig_t_us" ) ) - kBurstUs ) < 3 * 280 );
    // A partial dump from the trigger on starts at its index.
    const auto part = lines( dump( b, "TRAC:DATA? RAW,1000,32" ) );
    CHECK_CONTAINS( part.front(), "count=32" );
    CHECK( part.size() > 2 );
    CHECK_CONTAINS( block, "METER_DUMP_END crc32=" );
    // A scaled dump stamps the burst's sample at the burst.
    const std::string scaled = dump( b, "TRAC:DATA? SCAL,1000,1" );
    const auto        line   = lines( scaled );
    CHECK( line.size() >= 3 );
    CHECK( b.clean() );
}

TEST( "trigger", "a falling level, a current already beyond it, a trigger that never fires" ) {
    // 100 mA from the start, 1 mA from t = 1 s: falling through 50 mA.
    Bench b( []( sim::World& w ) {
        w.ringSamples     = 4096;
        w.circuit.dutNaAt = []( sim::Micros now ) -> std::int64_t {
            return now < 1'000'000 ? 100'000'000 : 1'000'000;
        };
    } );
    b.tell(
        "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:TRIG:SOUR LEV;:TRIG:LEV 0.05;"
        ":TRIG:SLOP NEG;:INIT",
        2'500'000 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "trig" ), "fired" );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "trig_slope" ), "neg" );
    b.tell( "ABOR" );
    CHECK( std::llabs( std::stoll( field( b.ask( "FETC:STAT?" ), "trig_t_us" ) ) - 1'000'000 )
           < 3 * 280 );

    // Rising through 50 mA while already at 100 mA: nothing primes it, so it
    // waits; the ring wraps, and ABORt closes a capture that never fired.
    Bench high( []( sim::World& w ) {
        w.ringSamples   = 4096;
        w.circuit.dutNa = 100'000'000;
    } );
    high.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:TRIG:SOUR LEV;:TRIG:LEV 0.05;:INIT",
               3'000'000 );
    CHECK_EQ( field( high.ask( "SYST:STAT?" ), "trig" ), "armed" );
    high.tell( "ABOR" );
    const std::string stats = high.ask( "FETC:STAT?" );
    CHECK_EQ( field( stats, "trig_n" ), "-" );
    CHECK( std::stoul( field( stats, "samples" ) ) > 4096U );
    const auto rows  = lines( dump( high, "TRAC:DATA? RAW,0,16" ) );
    const long first = std::stol( field( rows.front(), "first" ) );
    CHECK( first > 0 );
    CHECK( rows.front().find( "trig_n=" ) == std::string::npos );
    CHECK( rows[1].rfind( "A n=", 0 ) == 0 || rows[1].rfind( "R n=", 0 ) == 0 );
    CHECK_EQ( field( high.ask( "SYST:STAT?" ), "trig" ), "-" );
}

TEST( "trigger", "a level trigger refuses a stream and a statistics-only capture" ) {
    Bench b;
    b.tell( "TRIG:SOUR LEV;:SENS:PROF STAT;:INIT" );
    CHECK_CONTAINS( b.errors(), "-221,\"Settings conflict;a level trigger needs a ring profile" );
    b.tell( "SENS:PROF TRAC;:SENS:CAPT STAT;:INIT" );
    CHECK_CONTAINS( b.errors(), "a level trigger needs a ring capture" );
    // MEASure ignores the trigger: an acquisition counts its own samples.
    b.tell( "SENS:CAPT DATA;:SENS:PROF STAT;:SAMP:COUN 10" );
    CHECK_EQ( b.ask( "MEAS:CURR?", 1'000'000 ), "0.05000625" );  // 50 mA and the offset
    CHECK( b.clean() );
}

TEST( "trigger", "a limited capture ends itself, and *OPC waits for its end" ) {
    Bench b;
    b.tell( "*ESR?" );
    b.tell( "SENS:CAPT:LIM ON;:SAMP:COUN 50;:SENS:PROF STAT;:INIT;*OPC", 1'000 );
    CHECK_EQ( b.ask( "*ESR?" ), "0" );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "limit" ), "50" );
    b.world().run( 200'000 );
    const std::string status = b.ask( "SYST:STAT?" );
    CHECK_EQ( field( status, "running" ), "0" );
    CHECK_EQ( field( status, "sink" ), "stream" );
    CHECK_EQ( b.ask( "*ESR?" ), "1" );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "samples" ), "50" );
    // A statistics-only capture ends itself the same way.
    b.tell( "SENS:CAPT STAT;:INIT", 200'000 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "running" ), "0" );
    b.tell( "ABOR;:SENS:CAPT DATA" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "samples" ), "50" );
    // *OPC with nothing open, or under a capture that never ends by itself,
    // completes at once; *CLS forgets a pending one.
    b.tell( "*OPC" );
    CHECK_EQ( b.ask( "*ESR?" ), "1" );
    b.tell( "SENS:CAPT:LIM OFF;:INIT;*OPC" );
    CHECK_EQ( b.ask( "*ESR?" ), "1" );
    b.tell( "ABOR;:SENS:CAPT:LIM ON;:INIT;*OPC;*CLS", 1'000 );
    b.world().run( 200'000 );
    CHECK_EQ( b.ask( "*ESR?" ), "0" );
    b.tell( "ABOR" );
    // A null window ends itself too.
    b.tell( "CAL:ZERO 1;*OPC", 1'000 );
    CHECK_EQ( b.ask( "*ESR?" ), "0" );
    b.world().run( 1'500'000 );
    CHECK_EQ( b.ask( "*ESR?" ), "1" );
    b.tell( "ABOR" );
}

TEST( "trigger", "threshold marks follow the samples they were seen at, and are capped" ) {
    // 80 mA for 10 ms of every 50 ms, 2 mA otherwise.
    Bench b( []( sim::World& w ) {
        w.circuit.dutNaAt = []( sim::Micros now ) -> std::int64_t {
            return ( now % 50'000 ) < 10'000 ? 80'000'000 : 2'000'000;
        };
    } );
    b.tell( "SENS:CURR:THR 0.04;:SENS:CURR:THR:MARK ON;:SENS:PROF STAT;:INIT" );
    b.world().run( 300'000 );
    b.tell( "ABOR" );
    const std::string out   = b.drain();
    long long         lastT = -1;
    int               marks = 0;
    for ( const std::string& line : lines( out ) ) {
        if ( line.rfind( "METER t=", 0 ) == 0 ) {
            lastT = std::stoll( field( line, "t" ) );
        } else if ( line.rfind( "METER_MARK ", 0 ) == 0 ) {
            // The sample the crossing was seen at came first.
            CHECK( std::stoll( field( line, "t" ) ) == lastT );
            ++marks;
        }
    }
    CHECK( marks >= 10 );
    CHECK_CONTAINS( out, "mark=thr_rise" );
    CHECK_CONTAINS( out, "mark=thr_fall" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "thr_marks" ), std::to_string( marks ) );
    // A level inside the noise, with no hysteresis, chatters: the cap holds.
    Bench noisy( []( sim::World& w ) {
        w.ina.noiseCounts = 10;
        w.circuit.dutNa   = 20'000'000;
    } );
    noisy.tell( "SENS:CURR:THR 0.0200048;:SENS:CURR:THR:MARK ON;:SENS:PROF STAT;:INIT" );
    noisy.world().run( 4'000'000 );
    noisy.tell( "ABOR" );
    noisy.drain();
    CHECK_EQ( field( noisy.ask( "FETC:STAT?" ), "thr_marks" ), "1024" );
    // An acquisition prints none.
    noisy.tell( "SAMP:COUN 200" );
    noisy.drain();
    // 20 mA and the offset, within the +/-10 counts of noise (31.25 uA).
    CHECK( std::abs( std::stod( noisy.ask( "MEAS:CURR?", 2'000'000 ) ) - 0.0200048 ) < 31.25e-6 );
    CHECK( noisy.drain().find( "METER_MARK" ) == std::string::npos );
}

TEST( "trigger", "TRACe:MARK on a stream is written after every sample before it" ) {
    Bench b;
    b.tell( "SENS:PROF STAT;:INIT", 20'000 );
    b.tell( "TRAC:MARK first", 20'000 );
    b.tell( "ABOR" );
    long long lastT = -1;
    bool      seen  = false;
    for ( const std::string& line : lines( b.drain() ) ) {
        if ( line.rfind( "METER t=", 0 ) == 0 ) {
            const long long t = std::stoll( field( line, "t" ) );
            CHECK( !seen || t > lastT );
            lastT = t;
        } else if ( line.rfind( "METER_MARK ", 0 ) == 0 ) {
            CHECK( std::stoll( field( line, "t" ) ) >= lastT );
            seen = true;
        }
    }
    CHECK( seen );
    // A full queue has no room: the mark is printed at once instead. The
    // message is on the line before the queue fills, so the console reads it
    // before it next drains the queue. The capture's setup is let finish
    // first: its transactions take their bus time (FW-14's register-level
    // bus, where ESP-IDF's fake took none).
    b.tell( "INIT", 20'000 );
    b.drain();
    b.world().send( "TRAC:MARK late" );
    kleidos::meter::Sample filler = {};
    while ( b.sampler().queueRecord( filler ) ) {}
    b.world().run( 1'000 );
    CHECK_CONTAINS( b.drain(), "METER_MARK mark=late" );
    b.tell( "ABOR" );
    // A crossing with threshold marks off writes none.
    Bench quiet( []( sim::World& w ) {
        w.circuit.dutNaAt = []( sim::Micros now ) -> std::int64_t {
            return ( now % 50'000 ) < 10'000 ? 80'000'000 : 2'000'000;
        };
    } );
    quiet.tell( "SENS:CURR:THR 0.04;:SENS:PROF STAT;:INIT", 200'000 );
    quiet.tell( "ABOR" );
    CHECK( quiet.drain().find( "METER_MARK" ) == std::string::npos );
    CHECK( std::stoul( field( quiet.ask( "FETC:STAT?" ), "rises" ) ) >= 3U );
}

TEST( "trigger",
      "the sampler refuses marks under an open capture and a pre-trigger past its ring" ) {
    Bench                       b( []( sim::World& w ) { w.ringSamples = 4096; } );
    const char*                 error = nullptr;
    kleidos::meter::CaptureInfo info  = {};
    CHECK(
        !b.sampler().startTriggered( kleidos::meter::Profile::Trace, 0, true, 4096, info, error ) );
    CHECK_CONTAINS( std::string( error ), "pre-trigger must be under the ring" );
    b.tell( "SENS:PROF STAT;:INIT" );
    CHECK( !b.sampler().setThresholdMarks( true, error ) );
    b.tell( "ABOR" );
    // A ring capture is a window that ends by itself: *OPC waits for it.
    b.tell( "*ESR?" );
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:INIT;*OPC", 1'000 );
    CHECK_EQ( b.ask( "*ESR?" ), "0" );
    b.world().run( 1'500'000 );
    CHECK_EQ( b.ask( "*ESR?" ), "1" );
    b.tell( "ABOR" );
    // A ring stopped before it filled dumps from its first sample.
    b.tell( "INIT", 100'000 );
    b.tell( "ABOR" );
    CHECK_CONTAINS( dump( b, "TRAC:DATA? RAW,0,16" ), "R n=0 c=" );
}

TEST( "trigger", "the dump's CRC-32 covers every byte before its closing row" ) {
    Bench b( []( sim::World& w ) {
        w.ringSamples     = 4096;
        w.ina.noiseCounts = 5;
    } );
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:INIT", 1'500'000 );
    b.tell( "TRAC:MARK mid", 10'000 );
    b.world().run( 1'000'000 );
    b.tell( "ABOR" );
    for ( const char* query : { "TRAC:DATA? RAW", "TRAC:DATA? SCAL,100,50" } ) {
        const std::string block = dump( b, query );
        const std::size_t end   = block.rfind( "METER_DUMP_END " );
        CHECK( end != std::string::npos );
        const std::uint32_t crc =
            kleidos::meter::crc32::update( kleidos::meter::crc32::kInitial, block.data(), end )
            ^ kleidos::meter::crc32::kInitial;
        char expect[16] = {};
        (void)std::snprintf( expect, sizeof( expect ), "%08x", static_cast<unsigned>( crc ) );
        const std::string closing = block.substr( end, block.find( '\n', end ) - end );
        CHECK_EQ( field( closing, "crc32" ), std::string( expect ) );
        CHECK_EQ( field( closing, "bytes" ), std::to_string( end ) );
    }
}
