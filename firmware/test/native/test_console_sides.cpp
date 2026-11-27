// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_console_sides.cpp
 * @brief The console's and its commands' decisions no other suite took
 *        (FW-28): each case takes the side of a branch the decision-coverage
 *        gate listed untaken, and says what the instrument does there, by
 *        value, so that a change to that side fails it.
 *
 * The status byte's OPERation summary and the QUEStionable CURRent bit, the
 * alert flags each way, a range switch whose conversion flag comes late, a
 * second part on the bus, the apertures of a profile that converts neither
 * the bus nor the die, the charge overflow, the -230 refusals, a die read
 * that fails at a capture's start, the relays unavailable, a refused relay
 * option, a stream without current, a stream and a mark behind a partial
 * response, `*RST` and a measurement with a stop the sampler is slow to
 * acknowledge, the pace that falls back, a lead cycle that falls due while
 * the loop works, the dump's clamp, the stored calibration from a blank
 * store to `q=cal` and then `q=stale`.
 */
#include "bench.h"
#include "ina228_regs.h"
#include "unit.h"

#include <cstdint>
#include <string>
#include <vector>

using bench::Bench;
using bench::field;
using bench::units;

namespace reg = kleidos::meter::ina228;

namespace {

/// @brief A DIAG_ALRT flag as the model's 16-bit register holds it.
/// @param[in] bits The flags, as ina228_regs.h spells them.
/// @return The same bits in DIAG_ALRT's width.
std::uint16_t diagBits( std::uint32_t bits ) {
    return static_cast<std::uint16_t>( bits );
}

/// @brief Counts the lines of @p text that start with @p prefix.
/// @param[in] text   What the line carried.
/// @param[in] prefix The start of the lines counted.
/// @return How many lines start with it.
int linesStartingWith( const std::string& text, const std::string& prefix ) {
    int         count  = 0;
    std::size_t cursor = 0;
    while ( cursor < text.size() ) {
        std::size_t end = text.find( '\n', cursor );
        if ( end == std::string::npos ) {
            end = text.size();
        }
        if ( text.compare( cursor, prefix.size(), prefix ) == 0 ) {
            ++count;
        }
        cursor = end + 1;
    }
    return count;
}

/// @brief The first line of @p text that starts with @p prefix.
/// @param[in] text   What the line carried.
/// @param[in] prefix The start of the line looked for.
/// @return The line without its newline, or "" when there is none.
std::string lineStartingWith( const std::string& text, const std::string& prefix ) {
    std::size_t cursor = 0;
    while ( cursor < text.size() ) {
        std::size_t end = text.find( '\n', cursor );
        if ( end == std::string::npos ) {
            end = text.size();
        }
        if ( text.compare( cursor, prefix.size(), prefix ) == 0 ) {
            return text.substr( cursor, end - cursor );
        }
        cursor = end + 1;
    }
    return "";
}

/// Four FETCh:HEADer? units: more than the response buffer holds, so the
/// console writes what fits out before the rest (a partial response).
constexpr const char* kFourHeaders = "FETC:HEAD?;:FETC:HEAD?;:FETC:HEAD?;:FETC:HEAD?";

}  // namespace

// --- Status -----------------------------------------------------------------

TEST( "sides", "the OPERation summary reaches *STB? while its event is enabled, and only then" ) {
    Bench b;
    b.tell( "*CLS;:STAT:OPER:ENAB 16" );
    CHECK_EQ( b.ask( "*STB?" ), "0" );
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    // The capture's rising edge is latched in the event register, which
    // *STB? reads without clearing.
    CHECK_EQ( b.ask( "*STB?" ), "128" );
    CHECK_EQ( b.ask( "*STB?" ), "128" );
    b.tell( "STAT:OPER:ENAB 0" );
    CHECK_EQ( b.ask( "*STB?" ), "0" );
    b.tell( "STAT:OPER:ENAB 16;*SRE 128" );
    CHECK_EQ( b.ask( "*STB?" ), "192" );  // and the master summary it enables
    CHECK_EQ( b.ask( "STAT:OPER?" ), "16" );
    CHECK_EQ( b.ask( "*STB?" ), "0" );  // the event read, the summary goes
    b.tell( "ABOR" );
    CHECK( b.clean() );
}

TEST( "sides", "a clip flag in a capture's window is the QUEStionable CURRent bit" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "256" );  // uncalibrated, nothing else
    b.world().ina.diagFlags = diagBits( reg::kDiagShntOl );
    b.world().run( 20'000 );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "258" );
    b.world().ina.diagFlags = 0;
    b.tell( "ABOR" );
    // The closed window's flags stand until the next capture starts.
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "258" );
    b.tell( "INIT", 20'000 );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "256" );
    b.tell( "ABOR" );
    // MATHOF and SHNTUL raise the same bit.
    for ( const std::uint32_t flag : { reg::kDiagMathOf, reg::kDiagShntUl } ) {
        b.tell( "INIT", 20'000 );
        b.world().ina.diagFlags = diagBits( flag );
        b.world().run( 20'000 );
        CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "258" );
        b.world().ina.diagFlags = 0;
        b.tell( "ABOR" );
    }
    CHECK( b.clean() );
}

TEST( "sides", "DIAGnostic:ALERt? reports every flag each way" ) {
    Bench b;
    b.world().ina.memStat = false;  // the trim memory's checksum failed
    b.world().ina.diagFlags =
        diagBits( reg::kDiagMathOf | reg::kDiagEnergyOf | reg::kDiagChargeOf );
    b.world().ina.convert = false;  // and no conversion completes
    const std::string set = b.ask( "DIAG:ALER?" );
    CHECK_EQ( field( set, "raw" ), "0x0E00" );
    CHECK_EQ( field( set, "memstat" ), "0" );
    CHECK_EQ( field( set, "mathof" ), "1" );
    CHECK_EQ( field( set, "energyof" ), "1" );
    CHECK_EQ( field( set, "chargeof" ), "1" );
    CHECK_EQ( field( set, "cnvrf" ), "0" );
    b.world().ina.memStat   = true;
    b.world().ina.diagFlags = 0;
    b.world().ina.convert   = true;
    b.world().run( 200'000 );  // a conversion completes since the last read
    const std::string clear = b.ask( "DIAG:ALER?" );
    CHECK_EQ( field( clear, "raw" ), "0x0003" );
    CHECK_EQ( field( clear, "memstat" ), "1" );
    CHECK_EQ( field( clear, "mathof" ), "0" );
    CHECK_EQ( field( clear, "energyof" ), "0" );
    CHECK_EQ( field( clear, "chargeof" ), "0" );
    CHECK_EQ( field( clear, "cnvrf" ), "1" );
    CHECK( b.clean() );
}

// --- The device -------------------------------------------------------------

TEST( "sides", "a range switch whose conversion flag comes after the discard says cnvrf=0" ) {
    Bench b;
    b.world().ina.cnvrfDelayUs = 100'000;  // the restarted conversion ends late
    b.tell( "SENS:CURR:RANG:IND 1", 300'000 );
    const std::string late = b.ask( "SENS:CURR:RANG:REP?" );
    CHECK_EQ( field( late, "rstacc" ), "1" );
    CHECK_EQ( field( late, "cnvrf" ), "0" );
    CHECK( b.clean() );  // reported, not refused: the range did change
    CHECK_EQ( b.ask( "SENS:CURR:RANG:IND?" ), "1" );
    b.world().ina.cnvrfDelayUs = 0;
    b.tell( "SENS:CURR:RANG:IND 0", 300'000 );
    CHECK_EQ( field( b.ask( "SENS:CURR:RANG:REP?" ), "cnvrf" ), "1" );
}

TEST( "sides", "a scan lists every part that answers, in address order" ) {
    Bench b;
    b.world().ina.alsoAnswers = 0x45;
    CHECK_EQ( b.ask( "DIAG:I2C:SCAN?" ), "expected=0x40 found=0x40,0x45 n=2" );
    // The INA228 is still the part the driver reads.
    CHECK_EQ( b.ask( "DIAG:REG? #H3E" ), "addr=0x3E bytes=2 raw=0x5449" );
    b.world().ina.alsoAnswers = 0x4F;
    b.world().ina.present     = false;
    CHECK_EQ( b.ask( "DIAG:I2C:SCAN?" ), "expected=0x40 found=0x4F n=1" );
}

TEST( "sides", "a profile that converts only the shunt has no bus or die aperture" ) {
    Bench b;
    b.tell( "SENS:PROF TRAC" );
    CHECK_EQ( b.ask( "SENS:CURR:APER?;:SENS:VOLT:APER?;:SENS:TEMP:APER?" ), "0.00028;0.0;0.0" );
    b.tell( "SENS:PROF BURS" );
    CHECK_EQ( b.ask( "SENS:CURR:APER?;:SENS:VOLT:APER?;:SENS:TEMP:APER?" ), "0.00005;0.0;0.0" );
    b.tell( "SENS:PROF STATE" );
    CHECK_EQ( b.ask( "SENS:VOLT:APER?;:SENS:TEMP:APER?" ), "0.00005;0.00005" );
}

TEST( "sides", "SENSe:CHARge? says when the accumulator overflowed" ) {
    Bench b;
    b.world().ina.diagFlags = diagBits( reg::kDiagChargeOf );
    CHECK_EQ( field( b.ask( "SENS:CHAR?" ), "of" ), "1" );
    b.world().ina.diagFlags = 0;
    CHECK_EQ( field( b.ask( "SENS:CHAR?" ), "of" ), "0" );
    CHECK( b.clean() );
}

TEST( "sides", "a die read that fails at a capture's start is written unknown" ) {
    Bench b;
    b.tell( "SENS:PROF STATE" );
    b.world().ina.failRegister = reg::kRegDieTemp;
    b.world().ina.failCount    = 1;
    b.tell( "INIT", 20'000 );
    b.world().ina.failRegister = -1;
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "die_mc_start" ), "unknown" );
    b.tell( "ABOR" );
    // The next capture reads it.
    b.tell( "INIT", 20'000 );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "die_mc_start" ), "25000" );
    b.tell( "ABOR" );
    CHECK( b.clean() );
}

TEST( "sides", "a DIAG_ALRT write that fails refuses the capture and leaves no header" ) {
    Bench b;
    b.tell( "SENS:PROF STATE" );
    b.world().ina.failRegister = reg::kRegDiagAlrt;
    b.world().ina.failCount    = 1;
    b.tell( "INIT", 20'000 );
    b.world().ina.failRegister = -1;
    CHECK_EQ( b.errors(), "-240,\"Hardware error;i2c write failed\"" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "running" ), "0" );
    b.tell( "FETC:HEAD?" );
    CHECK_EQ( b.errors(), "-230,\"Data corrupt or stale;no capture started since boot\"" );
    // Once a capture starts, ALATCH was written: the header says so.
    b.tell( "INIT", 20'000 );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "alatch" ), "1" );
    b.tell( "ABOR" );
}

// --- Refusals -----------------------------------------------------------------

TEST( "sides", "FETCh refuses while a capture runs and before any capture, with -230" ) {
    Bench b;
    b.tell( "FETC:HEAD?" );
    CHECK_EQ( b.errors(), "-230,\"Data corrupt or stale;no capture started since boot\"" );
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    b.tell( "FETC:CURR?" );
    CHECK_EQ( b.errors(), "-230,\"Data corrupt or stale;capture running (ABORt first)\"" );
    b.tell( "FETC:STAT?" );
    CHECK_EQ( b.errors(), "-230,\"Data corrupt or stale;capture running (ABORt first)\"" );
    b.tell( "ABOR" );
    CHECK( std::stoi( field( b.ask( "FETC:STAT?" ), "samples" ) ) > 0 );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "sink" ), "stream" );
    CHECK( b.clean() );
}

TEST( "sides", "relays that failed at boot refuse every order and read relays=0 in a header" ) {
    Bench b( []( sim::World& w ) {
        w.faults.failGpioConfig    = true;
        w.faults.failGpioConfigPin = 7;
    } );
    b.tell( "*CLS" );
    for ( const char* order :
          { "ROUT:LEAD OPEN,FORC", "ROUT:BYP ON", "ROUT:NULL ON", "ROUT:LEAD:CYCL 100,FORC" } ) {
        b.tell( order );
        CHECK_EQ( b.errors(),
                  "100,\"Relays unavailable;relays unavailable (gpio config failed at boot)\"" );
    }
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    const std::string header = b.ask( "FETC:HEAD?" );
    CHECK_EQ( field( header, "relays" ), "0" );
    b.tell( "ABOR;*RST", 300'000 );  // a reset with no relays to put back
    CHECK( b.clean() );
}

TEST( "sides", "a refused sampler call is queued by how its reason begins" ) {
    // The sampler's own texts. No line reaches "no device": every command that
    // asks the sampler is refused first by its flag D, with the same -241.
    namespace err = kleidos::meter::scpi::err;
    CHECK( kleidos::meter::samplerRefusalCode( "sampler engine failed to start (reboot)" )
           == err::kSamplerFailed );
    CHECK( kleidos::meter::samplerRefusalCode( "i2c write failed (DIAG_ALRT)" ) == err::kHardware );
    CHECK( kleidos::meter::samplerRefusalCode( "no device (scan found none)" )
           == err::kHardwareMissing );
    CHECK( kleidos::meter::samplerRefusalCode( "use CALibration:ZERO <seconds> (1..600)" )
           == err::kDataOutOfRange );
    CHECK( kleidos::meter::samplerRefusalCode( "capture running (ABORt first)" )
           == err::kSettingsConflict );
    CHECK( kleidos::meter::samplerRefusalCode( "" ) == err::kSettingsConflict );
}

TEST( "sides", "a relay order with an option it does not know is refused and moves nothing" ) {
    Bench b;
    b.tell( "ROUT:BYP ON,SOMETIMES" );
    CHECK_CONTAINS( b.errors(), "use ROUTe:BYPass ON|OFF[,FORCe][,LIVE]" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK( !b.world().bypassClosed() );
    b.tell( "ROUT:LEAD OPEN,NOW" );
    CHECK_CONTAINS( b.errors(), "use ROUTe:LEAD OPEN|CLOSe[,FORCe][,LIVE]" );
    CHECK( !b.world().leadOpen() );
}

// --- The stream -------------------------------------------------------------

TEST( "sides", "a stream without current leaves it out, in TEXT and in CSV" ) {
    Bench b;
    b.tell( "SENS:PROF SLEEP;:SENS:FUNC:ON \"VOLT\";:INIT", 1'500'000 );
    const std::string text = b.drain();
    CHECK( linesStartingWith( text, "METER t=" ) > 5 );
    CHECK( text.find( "ch=i_ua" ) == std::string::npos );
    const std::string record = lineStartingWith( text, "METER t=" );
    CHECK_EQ( record.substr( record.find( " ch=" ) ),
              " ch=v_mv v=3894.999" );  // 3.9 V less the burden
    b.tell( "ABOR", 300'000 );
    b.tell( "FORM CSV;:INIT", 1'500'000 );
    const std::string csv = b.drain();
    CHECK( linesStartingWith( csv, "S," ) > 5 );
    const std::string row = lineStartingWith( csv, "S," );
    // S,<t>,<i empty>,<v>,<p empty>,<die empty>
    const std::size_t afterTime = row.find( ',', 2 );
    REQUIRE( afterTime != std::string::npos );
    CHECK_EQ( row.substr( afterTime, 2 ), ",," );
    CHECK_CONTAINS( row, ",3894.999," );
    CHECK_EQ( row.substr( row.size() - 2 ), ",," );
    b.tell( "ABOR" );
    CHECK( b.clean() );
}

TEST( "sides", "samples wait in the queue while a response is half on the wire" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 100'000 );
    (void)b.drain();
    const std::string header = b.ask( "FETC:HEAD?" );
    // The fourth header does not fit whole beside the first three, so what
    // fits goes out first; the bypass's settle then pumps the stream, which
    // must not put a record inside the response line.
    b.world().send( std::string( kFourHeaders ) + ";:ROUT:BYP ON,FORC,LIVE" );
    b.world().run( 300'000 );
    const std::string out      = b.drain();
    const std::string response = lineStartingWith( out, "fw=" );
    REQUIRE( !response.empty() );
    const std::vector<std::string> headers = units( response );
    REQUIRE( headers.size() == 4U );
    for ( const std::string& unit : headers ) {
        CHECK_EQ( unit, header );  // whole: no record inside one, or after it
    }
    CHECK( linesStartingWith( out, "fw=" ) == 1 );
    // The samples the settle saw come after it, none lost.
    CHECK( linesStartingWith( out.substr( out.find( response ) + response.size() ), "METER t=" )
           > 10 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "dropped" ), "0" );
    b.tell( "ROUT:BYP OFF,FORC,LIVE", 200'000 );
    b.tell( "ABOR" );
    CHECK( b.clean() );
}

TEST( "sides", "a mark behind a partial response stays in the capture, off the line" ) {
    Bench b( []( sim::World& w ) { w.ringSamples = 4096; } );
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:INIT", 20'000 );
    b.tell( "TRAC:MARK shown" );  // a ring's mark is written at once
    CHECK_CONTAINS( b.drain(), "METER_MARK mark=shown" );
    b.world().send( std::string( kFourHeaders ) + ";:TRAC:MARK kept" );
    b.world().run( 100'000 );
    const std::string out = b.drain();
    CHECK( units( lineStartingWith( out, "fw=" ) ).size() == 4U );
    CHECK( out.find( "METER_MARK" ) == std::string::npos );
    b.world().run( 2'000'000 );  // the ring fills
    b.tell( "ABOR" );
    b.world().send( "TRAC:DATA? RAW" );
    b.world().run( 3'000'000 );
    CHECK_CONTAINS( b.drain(), "label=kept" );  // replayed by the dump
    CHECK( b.clean() );
}

// --- Stops the sampler is slow to acknowledge ---------------------------------

TEST( "sides", "*RST under a stop the sampler does not acknowledge keeps no statistics" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    b.world().ina.stallNextUs = 2'000'000;  // the sampler's next read holds the bus
    b.world().run( 5'000 );
    b.tell( "*RST", 1'000'000 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "sink" ), "idle" );
    b.tell( "FETC:STAT?" );
    // The reset's range write meets the bus the stalled read still holds.
    CHECK_EQ( b.errors(),
              "-240,\"Hardware error;i2c ESP_ERR_INVALID_STATE\","
              "-230,\"Data corrupt or stale;no capture closed since boot\"" );
    b.world().run( 2'000'000 );
    // Acknowledged, a reset keeps the closed window's statistics.
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    b.tell( "*RST", 300'000 );
    CHECK( std::stoi( field( b.ask( "FETC:STAT?" ), "samples" ) ) > 0 );
    CHECK( b.clean() );
}

TEST( "sides", "a sample a measurement's stop leaves queued is drained, never streamed" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:SAMP:COUN 30000" );
    b.world().send( "MEAS:CURR?" );
    b.world().run( 100'000 );
    // The sampler's next shunt read holds it past the console's ABORt: the
    // sample it finishes reaches the queue after the stop was asked for.
    b.world().ina.stallReadRegister = reg::kRegVShunt;
    b.world().ina.stallReadUs       = 20'000;
    b.world().sendRaw( "ABOR\r" );
    b.world().run( 500'000 );
    // The acquisition took it off the queue: nothing of the closed window
    // reaches the line afterwards as a stream record.
    CHECK( b.drain().find( "METER t=" ) == std::string::npos );
    CHECK_CONTAINS( b.errors(), "-230,\"Data corrupt or stale;the acquisition was aborted after " );
    b.world().run( 500'000 );
    CHECK( b.drain().find( "METER t=" ) == std::string::npos );
}

TEST( "sides", "SYSTem:STATus? names a pace that fell back to the timer while it runs" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 30'000 );
    const std::string alert = b.ask( "SYST:STAT?" );
    CHECK_EQ( field( alert, "pace" ), "alert" );
    CHECK_EQ( field( alert, "pace_fallback" ), "0" );
    b.world().ina.alertPin = sim::Ina228Model::AlertPin::Absent;
    b.world().run( 100'000 );
    const std::string fell = b.ask( "SYST:STAT?" );
    CHECK_EQ( field( fell, "running" ), "1" );
    CHECK_EQ( field( fell, "pace" ), "timer" );
    CHECK_EQ( field( fell, "pace_fallback" ), "1" );
    b.tell( "ABOR" );
}

// --- The lead cycle -----------------------------------------------------------

TEST( "sides", "a lead cycle that falls due while the loop works recloses with no input" ) {
    Bench b;
    b.tell( "ROUT:LEAD:CYCL 40,FORC", 1'000 );
    REQUIRE( b.world().leadOpen() );
    // Every clock read now stands for 1.7 ms of work, so the loop's last
    // check before it waits finds the cycle not yet due and its wait finds
    // it more than a millisecond past: the wait is none, and the next pass
    // recloses. Nothing is sent meanwhile, so no input wakes the loop.
    b.world().clockReadUs = 1'700;
    b.world().run( 340'000 );
    b.world().clockReadUs = 1;
    CHECK( !b.world().leadOpen() );
    const std::string result = b.ask( "ROUT:RES?" );
    CHECK_EQ( field( result, "path" ), "shunt" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK( b.clean() );
}

// --- TRACe ------------------------------------------------------------------

TEST( "sides", "a dump asked for past the ring's end is clamped to what was captured" ) {
    Bench b( []( sim::World& w ) { w.ringSamples = 4096; } );
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC;:INIT", 2'000'000 );
    b.tell( "ABOR" );
    REQUIRE( b.ask( "TRAC:POIN?" ) == "4096" );
    b.drain();
    b.world().send( "TRAC:DATA? RAW,4090,100" );
    b.world().run( 300'000 );
    const std::string dump = b.drain();
    CHECK_CONTAINS( dump, "METER_DUMP count=6 " );
    b.world().send( "TRAC:DATA? RAW,4090,3" );
    b.world().run( 300'000 );
    CHECK_CONTAINS( b.drain(), "METER_DUMP count=3 " );
    CHECK( b.clean() );
}

// --- CALibration --------------------------------------------------------------

TEST( "sides", "a blank store reads nominal; the VBUS side is reported as stored" ) {
    Bench             b;
    const std::string blank = b.ask( "CAL:DATA?" );
    CHECK_EQ( field( blank, "shunt_uohm" ), "100000" );
    CHECK_EQ( field( blank, "shunt_measured" ), "0" );
    CHECK_EQ( field( blank, "nulled_r0" ), "0" );
    CHECK_EQ( field( blank, "nulled_r1" ), "0" );
    CHECK_EQ( field( blank, "vbus_side" ), "unknown" );
    CHECK_EQ( field( blank, "cal_tag" ), "-" );
    b.tell( "CAL:VBUS SOUR" );
    CHECK_EQ( field( b.ask( "CAL:DATA?" ), "vbus_side" ), "source" );
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "vbus_side" ), "source" );
    b.tell( "ABOR;:CAL:VBUS UNKN" );
    CHECK_EQ( field( b.ask( "CAL:DATA?" ), "vbus_side" ), "unknown" );
    CHECK( b.clean() );
}

TEST( "sides", "a null on both ranges and a measured shunt give q=cal; a warmer die, q=stale" ) {
    Bench b;
    // The nulls: no current crosses the shunt.
    const std::int64_t current = b.world().circuit.dutNa;
    b.world().circuit.dutNa    = 0;
    b.tell( "SENS:PROF STATE;:CAL:ZERO 1", 1'200'000 );
    b.tell( "ABOR;:SENS:CURR:RANG:IND 1;:CAL:ZERO 1", 1'200'000 );
    b.tell( "ABOR;:SENS:CURR:RANG:IND 0", 100'000 );
    b.world().circuit.dutNa  = current;
    const std::string nulled = b.ask( "CAL:DATA?" );
    CHECK_EQ( field( nulled, "nulled_r0" ), "1" );
    CHECK_EQ( field( nulled, "nulled_r1" ), "1" );
    CHECK_EQ( field( nulled, "off_die_mc_r1" ), "25000" );
    // Nulled but the shunt never measured: still uncalibrated.
    b.tell( "INIT", 50'000 );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "q" ), "uncal" );
    b.tell( "SENS:CURR:SHUN 0.100000" );
    b.tell( "INIT", 50'000 );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "0" );  // no CALibration bit
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "q" ), "cal" );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "q" ), "cal" );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "q" ), "cal" );
    // The die 6 C warmer than at the null: the offset is stale.
    b.world().ina.dieMc = 31'000;
    b.tell( "INIT", 50'000 );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "256" );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "q" ), "stale" );
    // 5 C is still within the null's reach.
    b.world().ina.dieMc = 30'000;
    b.tell( "INIT", 50'000 );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "q" ), "cal" );
    CHECK( b.clean() );
}
