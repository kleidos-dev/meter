// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_averaging.cpp
 * @brief The averaging override (0.18.0, FW-06): off is each profile's own
 *        word; every count and conversion time of every profile at two bus
 *        clocks, taken with the word and the period the datasheet gives or
 *        refused by name and changing nothing; per profile, cleared by `*RST`
 *        and by a boot; and the captures that run on it, paced, reported and
 *        dumped at the period they ran with.
 */
#include "bench.h"
#include "ina228_regs.h"
#include "scope.h"
#include "unit.h"

#include <array>
#include <cstdint>
#include <cstdio>
#include <string>

using namespace kleidos::meter;
using bench::Bench;
using bench::field;
using bench::units;

namespace {

/// The INA228's eight counts and eight conversion times, written out here as
/// the datasheet's Table 7-6 lists them (not the firmware's tables).
constexpr std::array<std::uint32_t, 8> kCounts = { 1, 4, 16, 64, 128, 256, 512, 1024 };
constexpr std::array<std::uint32_t, 8> kTimes  = { 50, 84, 150, 280, 540, 1052, 2074, 4120 };

/// The same times as the aperture takes and answers them, in seconds.
constexpr std::array<const char*, 8> kSeconds = { "0.00005", "0.000084", "0.00015",  "0.00028",
                                                  "0.00054", "0.001052", "0.002074", "0.00412" };

/// One profile at one bus clock: its own word less the two fields the
/// override sets, the conversion time of the inputs that are not the shunt,
/// and the shortest period an override of it may give there.
struct Row {
    const char*   profile;
    std::uint32_t sclHz;
    std::uint32_t word;      ///< ADC_CONFIG with VSHCT and AVG 0.
    std::uint32_t othersUs;  ///< Bus and die conversion times of its mode.
    std::uint32_t shortestUs;
};

constexpr std::array<Row, 6> kRows = { {
    { "TRAC", 400000, 0xA000, 0, 191 },     // twice a 95 us read, and 1
    { "TRAC", 1000000, 0xA000, 0, 100 },    // BURST's period
    { "STAT", 400000, 0xF000, 100, 1051 },  // twice 525 us of reads, and 1
    { "STAT", 1000000, 0xF000, 100, 421 },
    { "SLE", 400000, 0xF000, 100, 1051 },
    { "SLE", 1000000, 0xF000, 100, 421 },
} };

constexpr std::uint32_t kLongestUs = 200000;

/// @p word as a report writes it.
std::string hex4( std::uint32_t word ) {
    std::array<char, 8> text = {};
    (void)std::snprintf( text.data(), text.size(), "0x%04X", static_cast<unsigned>( word ) );
    return text.data();
}

/// The one error queued by @p message, which answers nothing.
std::string only( Bench& b, const std::string& message ) {
    CHECK( b.clean() );
    b.tell( message );
    return b.errors();
}

/// @p text as the `-221` the queue holds.
std::string conflict( const std::string& text ) {
    return "-221,\"Settings conflict;" + text + "\"";
}

/// The refusal of an override whose @p period is under @p row's limit or over
/// the longest.
std::string refusal( const Row& row, std::uint32_t count, std::uint32_t time,
                     std::uint32_t period ) {
    const std::string gives = "averaging " + std::to_string( count ) + " x "
                              + std::to_string( time ) + " us gives " + std::to_string( period )
                              + " us a sample, ";
    if ( period < row.shortestUs ) {
        return conflict( gives + "under the " + std::to_string( row.shortestUs )
                         + " us its reads need at " + std::to_string( row.sclHz ) + " Hz" );
    }
    return conflict( gives + "over the " + std::to_string( kLongestUs ) + " us its pacing allows" );
}

/// Every command that sets the override.
constexpr std::array<const char*, 3> kSets = { "SENS:AVER ON", "SENS:AVER:COUN 4",
                                               "SENS:CURR:APER 0.00028" };

}  // namespace

TEST( "averaging", "off on every profile at boot: the profile's own word, count and time" ) {
    Bench b;
    b.tell( "SYST:COMM:I2C:FREQ 1000000" );
    struct Own {
        const char* profile;
        const char* report;
    };
    constexpr std::array<Own, 4> kOwn = { {
        { "TRAC",
          "profile=TRACE adc_config=0xA0C0 period_us=280 vshct_us=280 avg=1 avg_state=off "
          "vbusct_us=0" },
        { "STAT",
          "profile=STATE adc_config=0xF140 period_us=1152 vshct_us=1052 avg=1 "
          "avg_state=off vbusct_us=50" },
        { "SLE",
          "profile=SLEEP adc_config=0xF1C2 period_us=67520 vshct_us=4120 avg=16 "
          "avg_state=off vbusct_us=50" },
        { "BURS",
          "profile=BURST adc_config=0xA000 period_us=100 vshct_us=50 avg=1 avg_state=off "
          "vbusct_us=0" },
    } };
    for ( const Own& own : kOwn ) {
        b.tell( std::string( "SENS:PROF " ) + own.profile );
        CHECK_EQ( b.ask( "SENS:AVER?" ), "0" );
        CHECK_EQ( b.ask( "SENSE:AVERAGE:STATE?" ), "0" );
        CHECK_EQ( b.ask( "SENS:PROF:REP?" ), own.report );
        b.tell( "INIT", 800'000 );  // SLEEP's start takes nine of its periods
        const std::string header = b.ask( "ABOR;:FETC:HEAD?" );
        CHECK_EQ( field( header, "adc_config" ), field( own.report, "adc_config" ) );
        CHECK_EQ( field( header, "period_us" ), field( own.report, "period_us" ) );
        CHECK_EQ( field( header, "avg_state" ), "off" );
    }
    CHECK( b.clean() );
}

TEST( "averaging", "every count and time of every profile, taken with its word or refused" ) {
    int taken   = 0;
    int refused = 0;
    for ( const Row& row : kRows ) {
        Bench b;
        b.tell( "SYST:COMM:I2C:FREQ " + std::to_string( row.sclHz ) );
        b.tell( std::string( "SENS:PROF " ) + row.profile );
        const std::string own = b.ask( "SENS:PROF:REP?" );
        for ( std::size_t t = 0; t < kTimes.size(); ++t ) {
            for ( std::size_t c = 0; c < kCounts.size(); ++c ) {
                // Set while off, both are kept and change nothing.
                b.tell( "SENS:AVER:COUN " + std::to_string( kCounts[c] ) + ";:SENS:CURR:APER "
                        + kSeconds[t] );
                CHECK_EQ( b.ask( "SENS:PROF:REP?" ), own );
                const std::uint32_t period = ( row.othersUs + kTimes[t] ) * kCounts[c];
                b.tell( "SENS:AVER ON" );
                if ( period < row.shortestUs || period > kLongestUs ) {
                    CHECK_EQ( b.errors(), refusal( row, kCounts[c], kTimes[t], period ) );
                    CHECK_EQ( b.ask( "SENS:AVER?" ), "0" );
                    CHECK_EQ( b.ask( "SENS:PROF:REP?" ), own );
                    ++refused;
                    continue;
                }
                CHECK( b.clean() );
                const std::string   report = b.ask( "SENS:PROF:REP?" );
                const std::uint32_t word   = row.word | ( static_cast<std::uint32_t>( t ) << 6 )
                                             | static_cast<std::uint32_t>( c );
                CHECK_EQ( field( report, "adc_config" ), hex4( word ) );
                CHECK_EQ( field( report, "period_us" ), std::to_string( period ) );
                CHECK_EQ( field( report, "vshct_us" ), std::to_string( kTimes[t] ) );
                CHECK_EQ( field( report, "avg" ), std::to_string( kCounts[c] ) );
                CHECK_EQ( field( report, "avg_state" ), "on" );
                CHECK_EQ( field( report, "vbusct_us" ), field( own, "vbusct_us" ) );
                CHECK_EQ( b.ask( "SENS:AVER?;:SENS:AVER:COUN?;:SENS:CURR:APER?" ),
                          "1;" + std::to_string( kCounts[c] ) + ";" + kSeconds[t] );
                b.tell( "SENS:AVER OFF" );
                CHECK_EQ( b.ask( "SENS:PROF:REP?" ), own );
                ++taken;
            }
        }
        CHECK( b.clean() );
    }
    // Both kinds were met: the sum is every combination of every row.
    CHECK( taken > 0 && refused > 0 );
    CHECK( taken + refused == 6 * 64 );
}

TEST( "averaging", "a refused change of an override that is on leaves it as it was" ) {
    Bench b;
    b.tell( "SENS:PROF STAT;:SENS:AVER:COUN 16;:SENS:AVER ON" );
    const std::string on = b.ask( "SENS:PROF:REP?" );
    CHECK_EQ( field( on, "period_us" ), "18432" );
    CHECK_EQ( only( b, "SENS:AVER:COUN 256" ),
              conflict( "averaging 256 x 1052 us gives 294912 us a sample, over the 200000 us its "
                        "pacing allows" ) );
    CHECK_EQ( only( b, "SENS:AVER:COUN 1;:SENS:CURR:APER 0.00054" ),
              conflict( "averaging 1 x 540 us gives 640 us a sample, under the 1051 us its reads "
                        "need at 400000 Hz" ) );
    // The first of those two was taken: 1 x 1052 us is STATE's own.
    CHECK_EQ( b.ask( "SENS:AVER:COUN?;:SENS:CURR:APER?;:SENS:AVER?" ), "1;0.001052;1" );
    CHECK_EQ( only( b, "SENS:AVER:COUN 16" ), "0,\"No error\"" );
    CHECK_EQ( b.ask( "SENS:PROF:REP?" ), on );
    // The limits themselves, either side of the longest: 512 x (100 + 280) is
    // 194,560 us and 64 x (100 + 4120) is 270,080 us.
    CHECK_EQ( only( b, "SENS:CURR:APER 0.00028;:SENS:AVER:COUN 512" ), "0,\"No error\"" );
    CHECK_EQ( b.ask( "SAMP:TIM?" ), "0.19456" );
}

TEST( "averaging", "a parameter the INA228 does not take is refused by the sets' own words" ) {
    Bench             b;
    const std::string count = "use SENSe:AVERage:COUNt 1|4|16|64|128|256|512|1024";
    const std::string time  = "use SENSe:CURRent:APERture <seconds> (0.00005..0.00412)";
    const std::string own   = b.ask( "SENS:PROF:REP?" );
    CHECK_EQ( only( b, "SENS:AVER MAYBE" ),
              "-224,\"Illegal parameter value;use SENSe:AVERage[:STATe] ON|OFF\"" );
    CHECK_EQ( only( b, "SENS:AVER:COUN 6.4" ), "-104,\"Data type error;" + count + "\"" );
    for ( const char* wrong : { "0", "2", "32", "1025", "65552" } ) {
        CHECK_EQ( only( b, std::string( "SENS:AVER:COUN " ) + wrong ),
                  "-224,\"Illegal parameter value;" + count + "\"" );
    }
    CHECK_EQ( only( b, "SENS:CURR:APER LONG" ), "-104,\"Data type error;" + time + "\"" );
    for ( const char* wrong : { "0", "-1", "-0.00028", "0.0003", "0.004121", "99999999999" } ) {
        CHECK_EQ( only( b, std::string( "SENS:CURR:APER " ) + wrong ),
                  "-224,\"Illegal parameter value;" + time + "\"" );
    }
    // Rounded to the microsecond, as every time the console takes is.
    CHECK_EQ( only( b, "SENS:CURR:APER 280.4E-6" ), "0,\"No error\"" );
    CHECK_EQ( b.ask( "SENS:PROF:REP?;:SENS:AVER?" ), own + ";0" );
}

TEST( "averaging", "BURST has no override: each set refused with the reason, nothing changed" ) {
    Bench b;
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF BURS" );
    const std::string own = b.ask( "SENS:PROF:REP?" );
    for ( const char* set : kSets ) {
        CHECK_EQ( only( b, set ),
                  conflict( "BURSt has no averaging override: its 50 us conversion is its "
                            "definition" ) );
    }
    // A value no profile takes is refused for what it is.
    CHECK_EQ( only( b, "SENS:AVER:COUN 3" ),
              "-224,\"Illegal parameter value;use SENSe:AVERage:COUNt "
              "1|4|16|64|128|256|512|1024\"" );
    CHECK_EQ( b.ask( "SENS:PROF:REP?;:SENS:AVER?" ), own + ";0" );
    b.tell( "SENS:PROF TRAC" );
    CHECK_EQ( b.ask( "SENS:AVER:COUN?;:SENS:AVER?" ), "1;0" );
}

TEST( "averaging", "each profile has its own, and *RST and a boot return every one to off" ) {
    Bench b;
    b.tell( "SENS:AVER:COUN 4;:SENS:AVER ON" );
    b.tell( "SENS:PROF STAT;:SENS:AVER:COUN 64;:SENS:AVER ON" );
    b.tell( "SENS:PROF SLE" );
    CHECK_EQ( b.ask( "SENS:AVER?;:SENS:AVER:COUN?" ), "0;16" );  // SLEEP's own
    b.tell( "SENS:PROF TRAC" );
    CHECK_EQ( b.ask( "SENS:AVER?;:SENS:AVER:COUN?;:SAMP:TIM?" ), "1;4;0.00112" );
    b.tell( "SENS:PROF STAT" );
    CHECK_EQ( b.ask( "SENS:AVER?;:SENS:AVER:COUN?;:SAMP:TIM?" ), "1;64;0.073728" );

    b.tell( "*RST", 200'000 );
    for ( const char* profile : { "TRAC", "STAT", "SLE" } ) {
        b.tell( std::string( "SENS:PROF " ) + profile );
        CHECK_EQ( b.ask( "SENS:AVER?" ), "0" );
        // Its count and time too: turned on again, it is the profile's own.
        const std::string own = b.ask( "SENS:PROF:REP?" );
        b.tell( "SENS:AVER ON" );
        const std::string on = b.ask( "SENS:PROF:REP?" );
        CHECK_EQ( field( on, "adc_config" ), field( own, "adc_config" ) );
        CHECK_EQ( field( on, "period_us" ), field( own, "period_us" ) );
        CHECK_EQ( field( on, "avg_state" ), "on" );
    }
    CHECK( b.clean() );

    Bench again;  // nothing is stored: a boot is off
    CHECK_EQ( again.ask( "SENS:PROF STAT;:SENS:AVER?;:SENS:AVER:COUN?" ), "0;1" );
}

TEST( "averaging", "a STATE stream at AVG 16: paced, reported and closed at its period" ) {
    Bench b;
    b.tell( "SENS:PROF STAT;:SENS:AVER:COUN 16;:SENS:AVER ON;:SENS:CURR:THR 0" );
    b.tell( "INIT", 500'000 );
    CHECK( b.world().ina.regs[ina228::kRegAdcConfig] == 0xF142U );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "period_us" ), "18432" );
    // Refused by the table while the window is open, and changes nothing.
    for ( const char* set : kSets ) {
        CHECK_EQ( only( b, set ), conflict( "capture running (ABORt first)" ) );
    }
    b.world().run( 1'000'000 );
    b.tell( "ABOR" );
    const std::string stats = b.ask( "FETC:STAT?" );
    const int         taken = std::stoi( field( stats, "samples" ) );
    CHECK( taken >= 70 );
    CHECK_EQ( field( stats, "dropped" ), "0" );
    CHECK_EQ( field( stats, "read_err" ), "0" );
    CHECK_EQ( field( stats, "stale_edges" ), "0" );
    CHECK_EQ( field( stats, "pace_fallback" ), "0" );
    CHECK( std::abs( std::stoi( field( stats, "pace_err_ppm" ) ) ) < 1000 );
    // Off again, the window's own period is still what is reported of it.
    b.tell( "SENS:AVER OFF" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "above_us" ),
              std::to_string( std::stoi( field( stats, "above_n" ) ) * 18432 ) );
    const std::string header = b.ask( "FETC:HEAD?" );
    CHECK_EQ( field( header, "adc_config" ), "0xF142" );
    CHECK_EQ( field( header, "vshct_us" ), "1052" );
    CHECK_EQ( field( header, "vbusct_us" ), "50" );
    CHECK_EQ( field( header, "vtct_us" ), "50" );
    CHECK_EQ( field( header, "avg" ), "16" );
    CHECK_EQ( field( header, "period_us" ), "18432" );
    CHECK_EQ( field( header, "pace" ), "alert" );
    CHECK_EQ( field( header, "pace_edges" ), "8" );
    CHECK_EQ( field( header, "avg_state" ), "on" );
    CHECK_EQ( field( b.ask( "SENS:PROF:REP?" ), "period_us" ), "1152" );
    b.tell( "INIT", 200'000 );
    CHECK( b.world().ina.regs[ina228::kRegAdcConfig] == 0xF140U );
    CHECK_EQ( field( b.ask( "ABOR;:FETC:HEAD?" ), "avg_state" ), "off" );
    CHECK( b.clean() );
}

TEST( "averaging", "the longest sample allowed is paced by ALERT and stopped in time" ) {
    Bench b;
    b.tell( "SENS:PROF SLE;:SENS:CURR:APER 0.00028;:SENS:AVER:COUN 512;:SENS:AVER ON" );
    b.tell( "INIT", 4'000'000 );
    b.tell( "ABOR", 600'000 );
    CHECK( b.clean() );
    const std::string header = b.ask( "FETC:HEAD?" );
    CHECK_EQ( field( header, "period_us" ), "194560" );
    CHECK_EQ( field( header, "pace" ), "alert" );
    CHECK( std::stoi( field( b.ask( "FETC:STAT?" ), "samples" ) ) >= 8 );

    // The wire dead: the loop's own wait for an edge is the stop's longest.
    b.world().ina.alertPin = sim::Ina228Model::AlertPin::Pulses;
    b.tell( "INIT", 2'500'000 );
    b.world().ina.alertPin = sim::Ina228Model::AlertPin::Absent;
    b.world().run( 250'000 );
    b.tell( "ABOR", 600'000 );
    CHECK( b.clean() );
}

TEST( "averaging", "a TRACE ring at AVG 4: the timer at its period, dumped at it after a change" ) {
    Bench b;
    b.tell( "SENS:AVER:COUN 4;:SENS:AVER ON" );
    b.tell( "INIT", 500'000 );
    // The boundary's triggered temperature puts the override's word back.
    CHECK( b.world().ina.regs[ina228::kRegAdcConfig] == 0xA0C1U );
    b.tell( "ABOR" );
    const std::string header = b.ask( "FETC:HEAD?" );
    CHECK_EQ( field( header, "pace" ), "timer" );
    CHECK_EQ( field( header, "period_us" ), "1120" );
    CHECK_EQ( field( header, "avg" ), "4" );
    const int taken = std::stoi( field( b.ask( "FETC:STAT?" ), "samples" ) );
    CHECK( taken >= 400 && taken <= 500 );
    b.tell( "SENS:AVER OFF;:SENS:PROF STAT" );
    b.world().send( "TRAC:DATA? SCAL,0,2" );
    b.world().run( 500'000 );
    CHECK_CONTAINS( b.drain(), "period_us=1120" );
}

TEST( "averaging", "a bus clock lowered under an override that is on refuses the capture" ) {
    Bench b;
    b.tell( "SYST:COMM:I2C:FREQ 1000000" );
    b.tell( "SENS:PROF STAT;:SENS:CURR:APER 0.00054;:SENS:AVER ON" );
    CHECK( b.clean() );
    b.tell( "SYST:COMM:I2C:FREQ 400000" );
    CHECK_EQ( only( b, "INIT" ),
              conflict( "averaging 1 x 540 us gives 640 us a sample, under the 1051 us its reads "
                        "need at 400000 Hz" ) );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "sink" ), "idle" );
    b.tell( "SENS:AVER OFF;:INIT" );
    CHECK( b.clean() );
    b.tell( "ABOR" );
}

TEST( "averaging", "the null, the acquisition's limit and the self-test under an override" ) {
    Bench b( []( sim::World& w ) { w.circuit.dutNa = 5'000'000; } );
    b.tell( "SENS:PROF STAT;:SENS:AVER:COUN 128;:SENS:AVER ON" );
    CHECK( b.sampler().zeroSampleCount( 10 ) == 67U );  // 10 s of 147,456 us
    CHECK_EQ( only( b, "SAMP:COUN 5000;:MEAS:CURR?" ),
              "-222,\"Data out of range;acquisition of 5000 x 147456 us exceeds 600 s (lower "
              "SAMPle:COUNt, or INITiate a capture)\"" );
    CHECK( b.ask( "SAMP:COUN 5;:MEAS:CURR?", 4'000'000 ).starts_with( "0.00" ) );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "avg_state" ), "on" );
    // The self-test is STATE's own word, whatever the override.
    CHECK_EQ( b.ask( "*TST?", 2'000'000 ), "0" );
    CHECK( b.world().ina.regs[ina228::kRegAdcConfig] == 0xF140U );
}

TEST( "averaging", "the sampler itself refuses a change under an open capture" ) {
    Bench       b;
    const char* error = nullptr;
    b.tell( "SENS:PROF STAT;:INIT" );
    CHECK(
        !b.sampler().setAveraging( Averaging{ true, ina228::kAvg4, ina228::kCt1052us }, error ) );
    CHECK_EQ( std::string( error ), "capture running (ABORt first)" );
    CHECK( !b.sampler().averaging().on );
    b.tell( "ABOR" );
}
