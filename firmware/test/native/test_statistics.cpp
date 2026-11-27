// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_statistics.cpp
 * @brief The capture statistics of 0.10.0 (standard deviation, the maximum's
 *        place, the DUT's voltage and power extremes, the CHARGE mean, the
 *        threshold counts, the batch means) and the statistics-only capture,
 *        over the line against the modeled bench; and the 128-bit arithmetic
 *        they rest on.
 */
#include "bench.h"
#include "unit.h"
#include "wide.h"

#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <string>

using bench::Bench;
using bench::field;
namespace wide = kleidos::meter::wide;

namespace {

/// A report value in thousandths, as a double, for tolerance checks.
double number( const std::string& text ) {
    return std::strtod( text.c_str(), nullptr );
}

/// Every comma-separated value of a list.
std::vector<double> numbers( const std::string& list ) {
    std::vector<double> out;
    std::size_t         start = 0;
    while ( start <= list.size() ) {
        const std::size_t end = list.find( ',', start );
        out.push_back( number( list.substr( start, end - start ) ) );
        if ( end == std::string::npos ) {
            break;
        }
        start = end + 1;
    }
    return out;
}

}  // namespace

TEST( "stats", "wide: add carries, multiply is exact, compare, subtract clamps" ) {
    const wide::U128 carried = wide::add( wide::U128{ 0, UINT64_MAX }, 1 );
    CHECK( ( carried.hi ) == ( 1U ) );
    CHECK( ( carried.lo ) == ( 0U ) );
    const wide::U128 plain = wide::add( wide::U128{ 2, 5 }, 7 );
    CHECK( ( plain.hi ) == ( 2U ) );
    CHECK( ( plain.lo ) == ( 12U ) );

    const wide::U128 square = wide::multiply( UINT64_MAX, UINT64_MAX );
    CHECK( ( square.hi ) == ( UINT64_MAX - 1U ) );
    CHECK( ( square.lo ) == ( 1U ) );
    const wide::U128 wider = wide::multiply( wide::U128{ 2, 1ULL << 63U }, 4 );
    CHECK( ( wider.hi ) == ( 10U ) );
    CHECK( ( wider.lo ) == ( 0U ) );
    const wide::U128 small = wide::multiply( 3, 7 );
    CHECK( ( small.hi ) == ( 0U ) );
    CHECK( ( small.lo ) == ( 21U ) );

    CHECK( wide::less( wide::U128{ 0, 5 }, wide::U128{ 1, 0 } ) );
    CHECK( wide::less( wide::U128{ 1, 4 }, wide::U128{ 1, 5 } ) );
    CHECK( !wide::less( wide::U128{ 1, 5 }, wide::U128{ 1, 5 } ) );
    CHECK( !wide::less( wide::U128{ 2, 0 }, wide::U128{ 1, 9 } ) );

    const wide::U128 borrowed = wide::subtractClamped( wide::U128{ 1, 0 }, wide::U128{ 0, 1 } );
    CHECK( ( borrowed.hi ) == ( 0U ) );
    CHECK( ( borrowed.lo ) == ( UINT64_MAX ) );
    const wide::U128 clean = wide::subtractClamped( wide::U128{ 3, 9 }, wide::U128{ 1, 4 } );
    CHECK( ( clean.hi ) == ( 2U ) );
    CHECK( ( clean.lo ) == ( 5U ) );
    const wide::U128 clamped = wide::subtractClamped( wide::U128{ 0, 1 }, wide::U128{ 0, 2 } );
    CHECK( ( clamped.hi ) == ( 0U ) );
    CHECK( ( clamped.lo ) == ( 0U ) );
}

TEST( "stats", "wide: divide by a 64-bit divisor, and mulDiv with its saturation" ) {
    // (2^64 - 1)^2 / (2^64 - 1) = 2^64 - 1: every remainder step carries.
    const wide::U128 back = wide::divide( wide::multiply( UINT64_MAX, UINT64_MAX ), UINT64_MAX );
    CHECK( ( back.hi ) == ( 0U ) );
    CHECK( ( back.lo ) == ( UINT64_MAX ) );
    const wide::U128 upper = wide::divide( wide::U128{ 10, 0 }, 3 );
    CHECK( ( upper.hi ) == ( 3U ) );
    CHECK( ( upper.lo ) == ( 0x5555555555555555ULL ) );
    const wide::U128 none = wide::divide( wide::U128{ 1, 1 }, 0 );
    CHECK( ( none.hi ) == ( 0U ) );
    CHECK( ( none.lo ) == ( 0U ) );

    CHECK( ( wide::mulDiv( 7, 6, 4 ) ) == ( 10 ) );
    CHECK( ( wide::mulDiv( -7, 6, 4 ) ) == ( -10 ) );
    CHECK( ( wide::mulDiv( INT64_MAX, 1000000000ULL, 1000000000ULL ) ) == ( INT64_MAX ) );
    CHECK( ( wide::mulDiv( INT64_MAX, 4, 2 ) ) == ( INT64_MAX ) );
    CHECK( ( wide::mulDiv( -INT64_MAX, 4, 2 ) ) == ( INT64_MIN ) );
    CHECK( ( wide::mulDiv( 1, 1ULL << 63U, 1 ) ) == ( INT64_MAX ) );
    CHECK( ( wide::mulDiv( INT64_MAX, 4, 1 ) ) == ( INT64_MAX ) );
    CHECK( ( wide::mulDiv( INT64_MAX, 1, 1 ) ) == ( INT64_MAX ) );
    CHECK( ( wide::mulDiv( 5, 5, 0 ) ) == ( 0 ) );
}

TEST( "stats", "SENSe:CAPTure: set, query, refused while open, reset" ) {
    Bench b;
    CHECK_EQ( b.ask( "SENS:CAPT?" ), "DATA" );
    b.tell( "SENS:CAPT STAT" );
    CHECK_EQ( b.ask( "SENS:CAPT?" ), "STAT" );
    b.tell( "SENS:CAPT RING" );
    CHECK_CONTAINS( b.errors(), "-224," );
    b.tell( "SENS:PROF STAT;:INIT" );
    b.tell( "SENS:CAPT DATA" );
    CHECK_CONTAINS( b.errors(), "-221," );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "sink" ), "stats" );
    b.tell( "ABOR" );
    b.tell( "SENS:CAPT DATA;:SENS:CAPT STAT;:*RST" );
    CHECK_EQ( b.ask( "SENS:CAPT?" ), "DATA" );
    CHECK( b.clean() );
}

TEST( "stats", "a statistics-only capture streams nothing and leaves the ring empty" ) {
    Bench b;
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:CAPT STAT;:SENS:PROF TRAC;:INIT", 10'000 );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "sink" ), "stats" );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "ring" ), "0" );
    b.world().run( 2'000'000 );
    const std::string seen = b.drain();
    CHECK( seen.find( "METER t=" ) == std::string::npos );
    b.tell( "ABOR" );
    const std::string stats = b.ask( "FETC:STAT?" );
    // TRACE at 280 us: two seconds is about 7,100 samples, more than any ring
    // the host tests hand the sampler would take before it stopped itself.
    CHECK( std::stoul( field( stats, "samples" ) ) > 7000U );
    CHECK_EQ( field( stats, "dropped" ), "0" );
    CHECK_EQ( b.ask( "TRAC:POIN?" ), "0" );
    CHECK_CONTAINS( b.ask( "TRAC:DATA?;:SYST:ERR?" ), "no trace captured" );
    // A channel choice is no refusal on a ring profile when nothing streams.
    b.tell( "SENS:FUNC:ON \"CURR\";:INIT", 100'000 );
    b.tell( "ABOR;:SENS:FUNC:ON:DEF" );
    // STATE too: no stream line, the statistics in full.
    b.tell( "SENS:PROF STAT;:SENS:FUNC:ON \"CURR\",\"VOLT\";:INIT", 1'000'000 );
    CHECK( b.drain().find( "METER t=" ) == std::string::npos );
    b.tell( "ABOR" );
    CHECK( std::stoul( field( b.ask( "FETC:STAT?" ), "samples" ) ) > 800U );
    // In CSV too, where a sample with no channel is still a row of its own.
    b.tell( "FORM CSV;:INIT", 200'000 );
    CHECK( b.drain().find( "S," ) == std::string::npos );
    b.tell( "ABOR;:FORM TEXT" );
    CHECK( b.clean() );
}

TEST( "stats", "a capture whose shunt reads 0 has no figure to divide by it" ) {
    // The store takes no shunt outside its window from the line or the flash
    // (calibration.h), so a 0 is planted in memory, past both: the current
    // and its deviation read 0 rather than divide by it.
    Bench b( []( sim::World& w ) { w.ina.noiseCounts = 5; } );
    b.calibration().setShuntMicroOhm( 0 );
    b.tell( "SENS:PROF STATE;:SENS:CAPT STAT;:INIT", 200'000 );
    b.tell( "ABOR" );
    const std::string stats = b.ask( "FETC:STAT?" );
    CHECK( std::stoul( field( stats, "samples" ) ) > 50U );
    CHECK_EQ( field( stats, "mean_na" ), "0.000" );
    CHECK_EQ( field( stats, "sd_na" ), "0.000" );
    CHECK( b.clean() );
}

TEST( "stats", "the standard deviation matches the simulated noise" ) {
    // Uniform noise of +/-10 counts: sd = sqrt(((2 x 10 + 1)^2 - 1) / 12) =
    // 6.055 counts; at range 0's 312.5 nV and 0.1 ohm that is 18.92 uA.
    Bench b( []( sim::World& w ) {
        w.ina.noiseCounts = 10;
        w.circuit.dutNa   = 20'000'000;
    } );
    b.tell( "SENS:CAPT STAT;:SENS:PROF STAT;:INIT", 3'000'000 );
    b.tell( "ABOR" );
    const std::string stats = b.ask( "FETC:STAT?" );
    const double      sdNa  = number( field( stats, "sd_na" ) );
    // Within 2 %: the variance keeps six decimals of a count squared before
    // its root (truncated to whole counts, this read 18.750 uA, 0.9 % low).
    CHECK( sdNa > 18920.0 * 0.98 && sdNa < 18920.0 * 1.02 );
    // The mean is the simulated DUT within the noise's own standard error.
    const double meanNa = number( field( stats, "mean_na" ) );
    // (and the sense loop's 4.8 uA offset, which no null has removed).
    CHECK( std::fabs( meanNa - 20'004'800.0 ) < 2'000.0 );
    // A quiet DUT has none.
    Bench quiet;
    quiet.tell( "SENS:CAPT STAT;:SENS:PROF STAT;:INIT", 200'000 );
    quiet.tell( "ABOR" );
    CHECK_EQ( field( quiet.ask( "FETC:STAT?" ), "sd_na" ), "0.000" );
}

TEST( "stats", "the batches: between 32 and 63, equal, their mean the capture's" ) {
    Bench b( []( sim::World& w ) { w.ina.noiseCounts = 4; } );
    b.tell( "SENS:CAPT STAT;:SENS:PROF STAT;:INIT", 2'000'000 );
    b.tell( "ABOR" );
    const std::string stats   = b.ask( "FETC:STAT?" );
    const unsigned    batches = static_cast<unsigned>( std::stoul( field( stats, "batches" ) ) );
    const unsigned    size = static_cast<unsigned>( std::stoul( field( stats, "batch_samples" ) ) );
    CHECK( batches >= 32U && batches < 64U );
    const unsigned samples = static_cast<unsigned>( std::stoul( field( stats, "samples" ) ) );
    CHECK( batches * size <= samples );
    CHECK( ( batches + 1U ) * size > samples );

    const std::string report = b.ask( "FETC:STAT:BATC?" );
    CHECK_EQ( field( report, "batches" ), field( stats, "batches" ) );
    CHECK_EQ( field( report, "batch_samples" ), field( stats, "batch_samples" ) );
    const std::vector<double> means = numbers( field( report, "means_na" ) );
    CHECK( ( means.size() ) == ( static_cast<std::size_t>( batches ) ) );
    double sum = 0;
    for ( const double mean : means ) {
        sum += mean;
    }
    CHECK( std::fabs( sum / batches - number( field( stats, "mean_na" ) ) ) < 200.0 );

    // A constant current: every batch mean is the capture's mean, exactly.
    Bench flat;
    flat.tell( "SENS:CAPT STAT;:SENS:PROF STAT;:INIT", 500'000 );
    flat.tell( "ABOR" );
    const std::string flatMean = field( flat.ask( "FETC:STAT?" ), "mean_na" );
    for ( const double mean : numbers( field( flat.ask( "FETC:STAT:BATC?" ), "means_na" ) ) ) {
        CHECK( ( mean ) == ( number( flatMean ) ) );
    }
}

TEST( "stats", "FETCh:STATistics:BATCh? before, during, and with no sample" ) {
    Bench b;
    b.tell( "FETC:STAT:BATC?" );
    CHECK_CONTAINS( b.errors(), "no capture closed since boot" );
    b.tell( "SENS:PROF STAT;:INIT" );
    b.tell( "FETC:STAT:BATC?" );
    CHECK_CONTAINS( b.errors(), "capture running" );
    b.tell( "ABOR" );
    // Every shunt read fails: a closed window with no sample in it.
    b.world().ina.failRegister = kleidos::meter::ina228::kRegVShunt;
    b.world().ina.failCount    = 1'000'000;
    b.tell( "INIT", 200'000 );
    b.tell( "ABOR" );
    b.world().ina.failCount = 0;
    CHECK_EQ( b.ask( "FETC:STAT:BATC?" ), "batches=0 batch_samples=1 means_na=-" );
    const std::string stats = b.ask( "FETC:STAT?" );
    CHECK_EQ( field( stats, "samples" ), "0" );
    CHECK_EQ( field( stats, "sd_na" ), "-" );
    CHECK_EQ( field( stats, "max_n" ), "-" );
    CHECK_EQ( field( stats, "max_t_us" ), "-" );
    CHECK_EQ( field( stats, "vmin_uv" ), "-" );
}

TEST( "stats", "the maximum's index and time, on a burst at a known instant" ) {
    // 1 mA, and 100 mA for 600 us at t = 1.2 s.
    constexpr sim::Micros kBurstUs = 1'200'000;
    Bench                 b( [&]( sim::World& w ) {
        w.circuit.dutNaAt = [&]( sim::Micros now ) -> std::int64_t {
            return ( now >= kBurstUs && now < kBurstUs + 600 ) ? 100'000'000 : 1'000'000;
        };
    } );
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:CAPT STAT;:SENS:PROF TRAC;:INIT", 10'000 );
    const std::string header = b.ask( "FETC:HEAD?" );
    b.world().run( 1'500'000 );
    b.tell( "ABOR" );
    const std::string stats = b.ask( "FETC:STAT?" );
    CHECK( number( field( stats, "max_na" ) ) > 99'000'000.0 );
    const long long maxT = std::stoll( field( stats, "max_t_us" ) );
    CHECK( maxT >= kBurstUs && maxT < kBurstUs + 600 + 280 );
    // The index counts periods from the capture's start.
    const long long t0    = std::stoll( field( header, "t0_us" ) );
    const long long index = std::stoll( field( stats, "max_n" ) );
    CHECK( std::llabs( t0 + index * 280 - maxT ) < 3 * 280 );
}

TEST( "stats", "the threshold: level, hysteresis, OFF, bounds, refused while open" ) {
    Bench b;
    CHECK_EQ( b.ask( "SENS:CURR:THR?" ), "OFF" );
    b.tell( "SENS:CURR:THR 0.05" );
    CHECK_EQ( b.ask( "SENS:CURR:THR?" ), "0.05" );
    b.tell( "SENS:CURR:THR:HYST 0.001" );
    CHECK_EQ( b.ask( "SENS:CURR:THR:HYST?" ), "0.001" );
    b.tell( "SENS:CURR:THR -0.002" );
    CHECK_EQ( b.ask( "SENS:CURR:THR?" ), "-0.002" );
    b.tell( "SENS:CURR:THR OFF" );
    CHECK_EQ( b.ask( "SENS:CURR:THR?" ), "OFF" );
    b.tell( "SENS:CURR:THR 0.003;:sens:curr:thr off" );
    CHECK_EQ( b.ask( "SENS:CURR:THR?" ), "OFF" );
    CHECK( b.clean() );
    b.tell( "SENS:CURR:THR 2.0" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "SENS:CURR:THR -2.0" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "SENS:CURR:THR ON" );
    CHECK_CONTAINS( b.errors(), "-224," );
    b.tell( "SENS:CURR:THR" );
    CHECK_CONTAINS( b.errors(), "-109," );
    b.tell( "SENS:CURR:THR:HYST -0.1" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "SENS:CURR:THR:HYST 2.0" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "SENS:CURR:THR:HYST OFF" );
    CHECK_CONTAINS( b.errors(), "-1" );
    b.tell( "SENS:PROF STAT;:INIT" );
    b.tell( "SENS:CURR:THR 0.01;:SENS:CURR:THR:HYST 0" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "2" );
    b.tell( "ABOR;*CLS;:SENS:CURR:THR 0.01;:*RST" );
    CHECK_EQ( b.ask( "SENS:CURR:THR?;:SENS:CURR:THR:HYST?" ), "OFF;0.0" );
}

TEST( "stats", "the threshold counts a square wave's rises and its time above" ) {
    // 80 mA for 10 ms out of every 50 ms, 2 mA otherwise, with noise that
    // would chatter across a bare level: the hysteresis holds one rise per
    // burst.
    Bench b( []( sim::World& w ) {
        w.ina.noiseCounts = 3;
        w.circuit.dutNaAt = []( sim::Micros now ) -> std::int64_t {
            return ( now % 50'000 ) < 10'000 ? 80'000'000 : 2'000'000;
        };
    } );
    b.tell( "SENS:CURR:THR 0.04;:SENS:CURR:THR:HYST 0.01;:SENS:CAPT STAT;:SENS:PROF STAT" );
    b.tell( "INIT", 1'000'000 );
    b.tell( "ABOR" );
    const std::string stats = b.ask( "FETC:STAT?" );
    CHECK_EQ( field( stats, "thr_na" ), "40000000" );
    const long rises = std::stol( field( stats, "rises" ) );
    CHECK( rises >= 19 && rises <= 21 );
    const double aboveUs = number( field( stats, "above_us" ) );
    const double durUs   = number( field( stats, "dur_us" ) );
    CHECK( aboveUs / durUs > 0.18 && aboveUs / durUs < 0.22 );
    CHECK( std::stoul( field( stats, "above_us" ) )
           == std::stoul( field( stats, "above_n" ) ) * 1152U );
    // With the threshold off the keys are there and empty.
    b.tell( "SENS:CURR:THR OFF;:INIT", 100'000 );
    b.tell( "ABOR" );
    const std::string off = b.ask( "FETC:STAT?" );
    CHECK_EQ( field( off, "thr_na" ), "-" );
    CHECK_EQ( field( off, "rises" ), "-" );
}

TEST( "stats", "voltage and power extremes, the energy over the duration, the charge mean" ) {
    Bench b( []( sim::World& w ) { w.circuit.dutNa = 50'000'000; } );
    b.tell( "SENS:PROF STAT;:SENS:FUNC:ON \"CURR\";:INIT", 1'000'000 );
    b.tell( "ABOR" );
    const std::string stats = b.ask( "FETC:STAT?" );
    // 3.9 V at the source less the 5 mV burden of 50 mA in 0.1 ohm.
    const double vmin = number( field( stats, "vmin_uv" ) );
    const double vmax = number( field( stats, "vmax_uv" ) );
    CHECK( vmin > 3'894'000.0 && vmax < 3'896'000.0 && vmin <= vmax );
    const double pmin = number( field( stats, "pmin_uw" ) );
    const double pmax = number( field( stats, "pmax_uw" ) );
    CHECK( pmin > 194'000.0 && pmax < 196'000.0 && pmin <= pmax );
    // The hardware integral agrees with the sample mean.
    const double chargeMean = number( field( stats, "mean_charge_na" ) );
    CHECK( std::fabs( chargeMean - 50'004'800.0 ) < 50'000.0 );
    CHECK( number( field( stats, "charge_us" ) ) > number( field( stats, "dur_us" ) ) );
    // Energy is the mean power over the capture's duration.
    const double power  = number( b.ask( "FETC:POW?" ) );
    const double energy = number( b.ask( "FETC:ENER?" ) );
    const double durS   = number( field( stats, "dur_us" ) ) / 1e6;
    CHECK( std::fabs( energy - power * durS ) < 1e-6 );
    CHECK( number( b.ask( "FETC:VOLT?" ) ) > 3.894 );
    // A ring profile has no bus: the keys are empty and the functions refuse.
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:INIT", 200'000 );
    b.tell( "ABOR" );
    const std::string ring = b.ask( "FETC:STAT?" );
    CHECK_EQ( field( ring, "vmax_uv" ), "-" );
    CHECK_EQ( field( ring, "pmin_uw" ), "-" );
    CHECK( field( ring, "mean_charge_na" ) != "-" );
    b.tell( "FETC:ENER?" );
    CHECK_CONTAINS( b.errors(), "converted no bus voltage" );
    // A CHARGE boundary that failed leaves its mean empty.
    b.tell( "SENS:PROF STAT;:INIT", 50'000 );
    b.world().ina.failRegister = kleidos::meter::ina228::kRegCharge;
    b.world().ina.failCount    = 1;
    b.tell( "ABOR" );
    b.world().ina.failRegister = -1;
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "mean_charge_na" ), "-" );
}

TEST( "stats", "the sampler refuses a threshold under an open capture or a negative hysteresis" ) {
    Bench       b;
    const char* error = nullptr;
    CHECK( !b.sampler().setThreshold( true, 1000, -1, error ) );
    CHECK_EQ( std::string( error ), "the hysteresis is 0 or more" );
    b.tell( "SENS:PROF STAT;:INIT" );
    CHECK( !b.sampler().setThreshold( true, 1000, 0, error ) );
    CHECK_EQ( std::string( error ), "capture running (ABORt first)" );
    b.tell( "ABOR" );
    CHECK( b.sampler().setThreshold( true, 1000, 0, error ) );
}
