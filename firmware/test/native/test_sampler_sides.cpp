// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_sampler_sides.cpp
 * @brief The sampler's, the driver's and the parameter windows' decisions no
 *        other suite took (FW-29): each case takes the side of a branch the
 *        decision-coverage gate listed untaken, or holds a window of
 *        docs/scpi.md at its edges, and says what the instrument does there,
 *        by value, so that a change to that side fails it.
 *
 * `DIAGnostic:REGister?` on every register at its own width; a range switch
 * that waits the period of the profile the device converts with; the die
 * temperature a TRACE capture notes at each boundary, which is all `q=` is
 * judged on there; the ALERT interrupt that wakes the sampler only when it
 * waits; the pacing probe after a stop the sampler did not acknowledge; a null
 * window that cannot start, one whose stop is not acknowledged and the zero
 * seconds the console never passes; the die's age at the instant it is noted;
 * a threshold mark that meets a full stream queue; and every refusal window
 * of docs/scpi.md at min-1, min, max and max+1.
 */
#include "bench.h"
#include "ina228_regs.h"
#include "sampler.h"
#include "unit.h"

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

using bench::Bench;
using bench::field;

namespace reg = kleidos::meter::ina228;

namespace {

/// The sampler task's name, as sampler.cpp creates it: what a stall names.
constexpr const char* kSamplerTask = "meter-sample";
/// The console's task: ESP-IDF's main task, which app_main() keeps.
constexpr const char* kConsoleTask = "main";

/// @brief The error code that leads the queue's answer.
/// @param[in] errors What `SYSTem:ERRor:ALL?` answered.
/// @return The text before its first comma ("0" for an empty queue).
std::string leadingCode( const std::string& errors ) {
    return errors.substr( 0, errors.find( ',' ) );
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

/// @brief Calibrates @p b at the bench's 25 C: a null on both ranges with
///        nothing crossing the shunt, and the shunt measured, so `q=` reads
///        `cal` until the die moves more than 5 C (as FW-28's end-to-end case).
/// @param[in,out] b The bench.
void calibrate( Bench& b ) {
    const std::int64_t current = b.world().circuit.dutNa;
    b.world().circuit.dutNa    = 0;
    b.tell( "SENS:PROF STATE;:CAL:ZERO 1", 1'200'000 );
    b.tell( "ABOR;:SENS:CURR:RANG:IND 1;:CAL:ZERO 1", 1'200'000 );
    b.tell( "ABOR;:SENS:CURR:RANG:IND 0", 100'000 );
    b.world().circuit.dutNa = current;
    b.tell( "SENS:CURR:SHUN 0.100000" );
}

/// One edge of a parameter window: the order, the query that reads what it
/// set, what the query answers after the order, and the code the order
/// queued ("0" for none).
struct Edge {
    const char* order;
    const char* query;
    const char* answer;
    const char* code;
};

}  // namespace

// --- The driver -------------------------------------------------------------

TEST( "sampler-sides", "DIAGnostic:REGister? reads every register at its own width" ) {
    Bench       b;
    sim::World& w = b.world();
    // Registers the model holds as written, planted with every byte distinct.
    w.ina.regs[reg::kRegShuntTempco] = 0x0ABCU;
    w.ina.regs[reg::kRegPower]       = 0x123456U;
    w.ina.regs[reg::kRegEnergy]      = 0x0102030405ULL;
    w.ina.regs[reg::kRegShuntOvl]    = 0x7123U;
    w.ina.regs[reg::kRegShuntUvl]    = 0x8123U;
    w.ina.regs[reg::kRegBusOvl]      = 0x6123U;
    w.ina.regs[reg::kRegBusUvl]      = 0x0123U;
    w.ina.regs[reg::kRegTempLimit]   = 0x4123U;
    w.ina.regs[reg::kRegPowerLimit]  = 0xF123U;
    // And the live ones, from the circuit: 50 mA through 0.1 ohm plus the
    // 0.48 uV offset is 16,002 counts of 312.5 nV; the 3.9 V bus 19,968 of
    // 195.3125 uV; the die's 25 C 3,200 of 7.8125 mC; CHARGE a planted count;
    // DIAG_ALRT with no conversion ending, MEMSTAT alone.
    w.ina.chargeCounts                                           = 0x0102030405LL;
    w.ina.convert                                                = false;
    const std::vector<std::pair<const char*, const char*>> table = {
        { "#H00", "addr=0x00 bytes=2 raw=0x0000" },
        { "#H01", "addr=0x01 bytes=2 raw=0xFB68" },
        { "#H02", "addr=0x02 bytes=2 raw=0x1000" },
        { "#H03", "addr=0x03 bytes=2 raw=0x0ABC" },
        { "#H04", "addr=0x04 bytes=3 raw=0x03E820" },
        { "#H05", "addr=0x05 bytes=3 raw=0x04E000" },
        { "#H06", "addr=0x06 bytes=2 raw=0x0C80" },
        { "#H07", "addr=0x07 bytes=3 raw=0x03E820" },
        { "#H08", "addr=0x08 bytes=3 raw=0x123456" },
        { "#H09", "addr=0x09 bytes=5 raw=0x0102030405" },
        { "#H0A", "addr=0x0A bytes=5 raw=0x0102030405" },
        { "#H0B", "addr=0x0B bytes=2 raw=0x0001" },
        { "#H0C", "addr=0x0C bytes=2 raw=0x7123" },
        { "#H0D", "addr=0x0D bytes=2 raw=0x8123" },
        { "#H0E", "addr=0x0E bytes=2 raw=0x6123" },
        { "#H0F", "addr=0x0F bytes=2 raw=0x0123" },
        { "#H10", "addr=0x10 bytes=2 raw=0x4123" },
        { "#H11", "addr=0x11 bytes=2 raw=0xF123" },
        { "#H3E", "addr=0x3E bytes=2 raw=0x5449" },
        { "#H3F", "addr=0x3F bytes=2 raw=0x2281" },
    };
    for ( const auto& [address, answer] : table ) {
        CHECK_EQ( b.ask( std::string( "DIAG:REG? " ) + address ), answer );
    }
    CHECK( b.clean() );
    // A reserved address is refused rather than read with a guessed width,
    // the first after the listed ones, the last before the identities and
    // the last of the byte; past the byte the address is out of range.
    for ( const char* reserved : { "#H12", "#H3D", "#H40", "#HFF" } ) {
        b.tell( std::string( "DIAG:REG? " ) + reserved );
        CHECK_EQ( b.errors(), std::string( "-224,\"Illegal parameter value;reserved register 0x" )
                                  + ( reserved + 2 ) + "\"" );
    }
    b.tell( "DIAG:REG? -1" );
    CHECK_EQ( leadingCode( b.errors() ), "-222" );
    b.tell( "DIAG:REG? #H100" );
    CHECK_EQ( b.errors(),
              "-222,\"Data out of range;use DIAGnostic:REGister? <addr> (#H00..#HFF)\"" );
}

TEST( "sampler-sides", "a range switch waits the period the device converts at, TRACE's too" ) {
    Bench b;
    // Before any capture the device converts at its reset word's 3,156 us.
    b.tell( "SENS:CURR:RANG:IND 1", 300'000 );
    CHECK_EQ( field( b.ask( "SENS:CURR:RANG:REP?" ), "discard_us" ), "3156" );
    // A TRACE capture leaves its word, which converts the shunt alone: no bus
    // and no die conversion in the sequence, 280 us.
    b.tell( "SENS:CURR:RANG:IND 0;:SENS:PROF TRAC;:INIT", 50'000 );
    b.tell( "ABOR" );
    b.tell( "SENS:CURR:RANG:IND 1", 300'000 );
    const std::string trace = b.ask( "SENS:CURR:RANG:REP?" );
    CHECK_EQ( field( trace, "discard_us" ), "280" );
    CHECK_EQ( field( trace, "rstacc" ), "1" );
    CHECK_EQ( field( trace, "cnvrf" ), "1" );
    // STATE's word converts all three: 50 + 1,052 + 50 us.
    b.tell( "SENS:CURR:RANG:IND 0;:SENS:PROF STAT;:INIT", 50'000 );
    b.tell( "ABOR" );
    b.tell( "SENS:CURR:RANG:IND 1", 300'000 );
    CHECK_EQ( field( b.ask( "SENS:CURR:RANG:REP?" ), "discard_us" ), "1152" );
    CHECK( b.clean() );
}

// --- The die temperature a TRACE capture notes --------------------------------

TEST( "sampler-sides", "a TRACE capture's two boundaries are what q= is judged on" ) {
    // TRACE converts no die temperature: its opening and closing reads are
    // the only evidence it gives the quality token (MUT-F2, MUT-F2b).
    Bench b;
    calibrate( b );
    b.tell( "SENS:PROF TRAC;:INIT", 50'000 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "q" ), "cal" );
    // The bench warms by 6 C while the ring fills: nothing the capture
    // converts says so until it closes.
    b.world().ina.dieMc = 31'000;
    b.world().run( 50'000 );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "die_mc" ), "25000" );
    b.tell( "ABOR" );
    // The closing boundary read 31 C, and the live token is judged on it.
    const std::string closed = b.ask( "SYST:STAT?" );
    CHECK_EQ( field( closed, "die_mc" ), "31000" );
    CHECK_EQ( field( closed, "q" ), "stale" );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "256" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "q" ), "stale" );
    // Back at 25 C, a capture that opens and closes there is calibrated.
    b.world().ina.dieMc = 25'000;
    b.tell( "INIT", 50'000 );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "q" ), "cal" );
    // One that opens 6 C warmer is stale from its opening read on, while it
    // runs, though nothing it converts is a temperature.
    b.world().ina.dieMc = 31'000;
    b.tell( "INIT", 50'000 );
    const std::string opened = b.ask( "SYST:STAT?" );
    CHECK_EQ( field( opened, "running" ), "1" );
    CHECK_EQ( field( opened, "die_mc" ), "31000" );
    CHECK_EQ( field( opened, "q" ), "stale" );
    CHECK_EQ( field( b.ask( "FETC:HEAD?" ), "die_mc_start" ), "31000" );
    b.world().ina.dieMc = 25'000;
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "q" ), "cal" );
    CHECK( b.clean() );
}

// --- The ALERT interrupt and the sampler's acknowledgement ------------------

TEST( "sampler-sides",
      "a conversion's edge asks for a switch only when the sampler waits for it" ) {
    Bench       b;
    sim::World& w = b.world();
    b.tell( "SENS:PROF STATE;:INIT", 30'000 );
    REQUIRE( field( b.ask( "SYST:STAT?" ), "pace" ) == "alert" );
    // Paced by the pin, the sampler waits for every conversion: each edge
    // wakes it, and the interrupt asks for the switch.
    std::size_t gives  = w.isrGives;
    std::size_t yields = w.isrYields;
    w.run( 20'000 );
    CHECK( w.isrGives - gives >= 15U );  // 20 ms of 1.152 ms conversions
    CHECK( w.isrYields - yields == w.isrGives - gives );
    // Held elsewhere (a stall, a preemption), it waits for nothing: the next
    // conversion's edge gives the semaphore and asks for no switch, and the
    // pin, low until DIAG_ALRT is read, makes no other.
    w.taskStallsUs[kSamplerTask] = 50'000;
    gives                        = w.isrGives;
    yields                       = w.isrYields;
    w.run( 30'000 );
    CHECK( w.isrGives - gives == 1U );
    CHECK( w.isrYields - yields == 0U );
    w.run( 30'000 );  // the stall ends
    // It takes the conversion it was given and runs on, paced by the pin.
    gives  = w.isrGives;
    yields = w.isrYields;
    w.run( 20'000 );
    CHECK( w.isrGives - gives >= 15U );
    CHECK( w.isrYields - yields == w.isrGives - gives );
    const std::string status = b.ask( "SYST:STAT?" );
    CHECK_EQ( field( status, "pace" ), "alert" );
    CHECK_EQ( field( status, "pace_fallback" ), "0" );
    b.tell( "ABOR" );
    CHECK( b.clean() );
}

TEST( "sampler-sides", "*TST? after a stop the sampler did not acknowledge probes nothing" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 30'000 );
    // The sampler is held for 2 s: the stop's 500 ms pass unacknowledged.
    b.world().taskStallsUs[kSamplerTask] = 2'000'000;
    b.world().run( 5'000 );
    b.tell( "ABOR", 700'000 );
    CHECK_EQ( b.errors(),
              "107,\"Sampler not running;the sampler did not acknowledge the stop "
              "(the window is closed with no result)\"" );
    // Its loop may still run, so the pacing probe is not asked of it: the
    // self-test reads the ALERT wire as failed (32) and nothing else.
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "32" );
    // Once the loop has seen the stop and said so, the probe runs again.
    b.world().run( 2'000'000 );
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "0" );
    CHECK( b.clean() );
}

// --- Null windows -------------------------------------------------------------

TEST( "sampler-sides", "a null window that cannot start leaves no null and no window" ) {
    Bench b;
    b.world().circuit.dutNa    = 0;
    b.world().ina.failRegister = reg::kRegAdcConfig;
    b.world().ina.failCount    = 1;
    b.tell( "SENS:PROF STATE;:CAL:ZERO 1", 100'000 );
    b.world().ina.failRegister = -1;
    CHECK_EQ( b.errors(), "-240,\"Hardware error;i2c write failed\"" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "sink" ), "idle" );
    b.tell( "CAL:ZERO:RES?" );
    CHECK_EQ( b.errors(), "-230,\"Data corrupt or stale;no null taken since boot\"" );
    // The next one starts: two counts of 312.5 nV, the sense loop's offset.
    b.tell( "CAL:ZERO 1", 1'200'000 );
    b.tell( "ABOR" );
    const std::string result = b.ask( "CAL:ZERO:RES?" );
    CHECK_EQ( field( result, "offset_pv" ), "625000" );
    CHECK_EQ( field( result, "n" ), "868" );
    CHECK_EQ( field( result, "stored" ), "1" );
    CHECK( b.clean() );
}

TEST( "sampler-sides", "a reset under a null the sampler does not acknowledge keeps no null" ) {
    Bench b;
    b.world().circuit.dutNa = 0;
    b.tell( "SENS:PROF STATE;:CAL:ZERO 2", 100'000 );
    REQUIRE( field( b.ask( "CAL:ZERO:RES?" ), "running" ) == "1" );
    b.world().taskStallsUs[kSamplerTask] = 2'000'000;
    b.world().run( 5'000 );
    b.tell( "*RST", 700'000 );
    // The reset closed the window without its result, as it does with one.
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "sink" ), "idle" );
    b.tell( "CAL:ZERO:RES?" );
    CHECK_EQ( b.errors(), "-230,\"Data corrupt or stale;no null taken since boot\"" );
    // The loop has not said it stopped: a new window is refused until it does.
    b.tell( "CAL:ZERO 1" );
    CHECK_EQ( b.errors(),
              "107,\"Sampler not running;sampler engine has not acknowledged the "
              "last stop (try again; reboot if it persists)\"" );
    b.world().run( 2'000'000 );
    b.tell( "CAL:ZERO 1", 1'200'000 );
    b.tell( "ABOR" );
    const std::string result = b.ask( "CAL:ZERO:RES?" );
    CHECK_EQ( field( result, "offset_pv" ), "625000" );
    CHECK_EQ( field( result, "stored" ), "1" );
    CHECK( b.clean() );
}

TEST( "sampler-sides", "a null whose stop is not acknowledged has no result, and says so" ) {
    // What the console does with it is the reset above; this is the sampler's
    // answer to any caller: false, the result cleared, the window closed.
    Bench b;
    b.world().circuit.dutNa = 0;
    b.tell( "SENS:PROF STATE" );  // paced by the pin: its loop waits, so a stall holds it
    const char* error = nullptr;
    REQUIRE( b.sampler().startZero( 2, error ) );
    b.world().run( 100'000 );
    REQUIRE( b.sampler().sampleCount() > 0U );
    b.world().taskStallsUs[kSamplerTask] = 2'000'000;
    b.world().run( 5'000 );
    kleidos::meter::ZeroResult result = {};
    result.samples                    = 7;
    CHECK( !b.sampler().finishZero( result ) );
    CHECK( result.samples == 0U );
    CHECK( result.dieMilliCelsius == kleidos::meter::kDieMilliCelsiusUnknown );
    CHECK( !b.sampler().zeroOpen() );
}

TEST( "sampler-sides", "the sampler refuses a null of zero seconds or past ten minutes itself" ) {
    // The console refuses both first (CALibration:ZERO, below); the sampler's
    // own bound is the last word for any other caller.
    Bench       b;
    const char* error = nullptr;
    CHECK( !b.sampler().startZero( 0, error ) );
    CHECK_EQ( std::string( error ), "use CALibration:ZERO <seconds> (1..600)" );
    error = nullptr;
    CHECK( !b.sampler().startZero( kleidos::meter::kZeroMaxSeconds + 1U, error ) );
    CHECK_EQ( std::string( error ), "use CALibration:ZERO <seconds> (1..600)" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "sink" ), "idle" );
}

TEST( "sampler-sides", "the die's age is zero at the instant it is noted, and counts from there" ) {
    Bench b;
    b.world().circuit.dutNa = 0;
    const char* error       = nullptr;
    REQUIRE( b.sampler().startZero( 1, error ) );
    b.world().run( 1'100'000 );
    kleidos::meter::ZeroResult result = {};
    REQUIRE( b.sampler().finishZero( result ) );
    CHECK( result.dieMilliCelsius == 25'000 );
    // The null noted the die it read last, at the clock's last reading: with
    // the clock held, no time has passed since.
    b.world().clockReadUs = 0;
    CHECK( b.sampler().dieAgeUs() == 0U );
    b.world().clockReadUs = 1;
    CHECK( b.sampler().dieAgeUs() == 1U );
    CHECK( b.sampler().dieAgeUs() == 2U );
}

// --- The stream queue ---------------------------------------------------------

TEST( "sampler-sides", "a threshold mark that meets a full stream queue is not written" ) {
    // 80 mA for 10 ms of every 50 ms, 2 mA otherwise: a rise and a fall
    // through 40 mA every 50 ms.
    Bench b( []( sim::World& w ) {
        w.circuit.dutNaAt = []( sim::Micros now ) -> std::int64_t {
            return ( now % 50'000 ) < 10'000 ? 80'000'000 : 2'000'000;
        };
    } );
    b.tell( "SENS:CURR:THR 0.04;:SENS:CURR:THR:MARK ON;:SENS:PROF STAT;:INIT", 20'000 );
    // The console stops reading for 600 ms: the 256-sample queue fills in
    // 300 ms of STATE's 1.152 ms samples, and the crossings after that find
    // no room for their mark.
    b.world().taskStallsUs[kConsoleTask] = 600'000;
    b.world().run( 800'000 );
    b.tell( "ABOR" );
    const std::string out   = b.drain();
    const int         marks = linesStartingWith( out, "METER_MARK " );
    const std::string stats = b.ask( "FETC:STAT?" );
    const int         rises = std::stoi( field( stats, "rises" ) );
    // Every mark queued reached the line, and only those: the count says so.
    CHECK_EQ( field( stats, "thr_marks" ), std::to_string( marks ) );
    CHECK( rises >= 15 );
    // Each rise has a fall after it but perhaps the last: crossings are at
    // least twice the rises less one, and the stall cost some of their marks.
    CHECK( marks < 2 * rises - 1 );
    CHECK( marks > 0 );
    CHECK( std::stoi( field( stats, "dropped" ) ) > 0 );
}

// --- Parameter windows at their edges ------------------------------------------

TEST( "sampler-sides", "every window of docs/scpi.md at min-1, min, max and max+1" ) {
    // Each window: its least value, one step below it, its largest, one step
    // past it. A refused value leaves the setting as it was (-222, a value
    // outside its window, docs/scpi.md); every step is the parameter's own
    // resolution (an integer's 1, an ohm's micro-ohm, an ampere's nanoampere
    // or microampere).
    Bench                   b;
    const std::vector<Edge> edges = {
        { "*ESE 0", "*ESE?", "0", "0" },
        { "*ESE -1", "*ESE?", "0", "-222" },
        { "*ESE 255", "*ESE?", "255", "0" },
        { "*ESE 256", "*ESE?", "255", "-222" },
        { "*SRE 0", "*SRE?", "0", "0" },
        { "*SRE -1", "*SRE?", "0", "-222" },
        { "*SRE 255", "*SRE?", "255", "0" },
        { "*SRE 256", "*SRE?", "255", "-222" },
        { "*SRE 0", "*SRE?", "0", "0" },
        { "STAT:OPER:ENAB 0", "STAT:OPER:ENAB?", "0", "0" },
        { "STAT:OPER:ENAB -1", "STAT:OPER:ENAB?", "0", "-222" },
        { "STAT:OPER:ENAB 32767", "STAT:OPER:ENAB?", "32767", "0" },
        { "STAT:OPER:ENAB 32768", "STAT:OPER:ENAB?", "32767", "-222" },
        { "STAT:QUES:ENAB 0", "STAT:QUES:ENAB?", "0", "0" },
        { "STAT:QUES:ENAB -1", "STAT:QUES:ENAB?", "0", "-222" },
        { "STAT:QUES:ENAB 32767", "STAT:QUES:ENAB?", "32767", "0" },
        { "STAT:QUES:ENAB 32768", "STAT:QUES:ENAB?", "32767", "-222" },
        { "STAT:OPER:PTR 0", "STAT:OPER:PTR?", "0", "0" },
        { "STAT:OPER:PTR -1", "STAT:OPER:PTR?", "0", "-222" },
        { "STAT:OPER:PTR 32767", "STAT:OPER:PTR?", "32767", "0" },
        { "STAT:OPER:PTR 32768", "STAT:OPER:PTR?", "32767", "-222" },
        { "STAT:OPER:NTR 0", "STAT:OPER:NTR?", "0", "0" },
        { "STAT:OPER:NTR -1", "STAT:OPER:NTR?", "0", "-222" },
        { "STAT:OPER:NTR 32767", "STAT:OPER:NTR?", "32767", "0" },
        { "STAT:OPER:NTR 32768", "STAT:OPER:NTR?", "32767", "-222" },
        { "STAT:QUES:PTR 0", "STAT:QUES:PTR?", "0", "0" },
        { "STAT:QUES:PTR -1", "STAT:QUES:PTR?", "0", "-222" },
        { "STAT:QUES:PTR 32767", "STAT:QUES:PTR?", "32767", "0" },
        { "STAT:QUES:PTR 32768", "STAT:QUES:PTR?", "32767", "-222" },
        { "STAT:QUES:NTR 0", "STAT:QUES:NTR?", "0", "0" },
        { "STAT:QUES:NTR -1", "STAT:QUES:NTR?", "0", "-222" },
        { "STAT:QUES:NTR 32767", "STAT:QUES:NTR?", "32767", "0" },
        { "STAT:QUES:NTR 32768", "STAT:QUES:NTR?", "32767", "-222" },
        { "STAT:PRES", "STAT:OPER:ENAB?", "0", "0" },
        { "SYST:COMM:I2C:FREQ 50000", "SYST:COMM:I2C:FREQ?", "50000", "0" },
        { "SYST:COMM:I2C:FREQ 49999", "SYST:COMM:I2C:FREQ?", "50000", "-222" },
        { "SYST:COMM:I2C:FREQ 1000000", "SYST:COMM:I2C:FREQ?", "1000000", "0" },
        { "SYST:COMM:I2C:FREQ 1000001", "SYST:COMM:I2C:FREQ?", "1000000", "-222" },
        { "SYST:COMM:I2C:FREQ 400000", "SYST:COMM:I2C:FREQ?", "400000", "0" },
        { "SENS:CURR:SHUN 0.05", "SENS:CURR:SHUN?", "0.05", "0" },
        { "SENS:CURR:SHUN 0.049999", "SENS:CURR:SHUN?", "0.05", "-222" },
        { "SENS:CURR:SHUN 0.2", "SENS:CURR:SHUN?", "0.2", "0" },
        { "SENS:CURR:SHUN 0.200001", "SENS:CURR:SHUN?", "0.2", "-222" },
        { "SENS:CURR:SHUN:RANG0 0.05", "SENS:CURR:SHUN:RANG0?", "0.05", "0" },
        { "SENS:CURR:SHUN:RANG0 0.049999", "SENS:CURR:SHUN:RANG0?", "0.05", "-222" },
        { "SENS:CURR:SHUN:RANG0 0.2", "SENS:CURR:SHUN:RANG0?", "0.2", "0" },
        { "SENS:CURR:SHUN:RANG0 0.200001", "SENS:CURR:SHUN:RANG0?", "0.2", "-222" },
        { "SENS:CURR:SHUN:RANG1 0.05", "SENS:CURR:SHUN:RANG1?", "0.05", "0" },
        { "SENS:CURR:SHUN:RANG1 0.049999", "SENS:CURR:SHUN:RANG1?", "0.05", "-222" },
        { "SENS:CURR:SHUN:RANG1 0.2", "SENS:CURR:SHUN:RANG1?", "0.2", "0" },
        { "SENS:CURR:SHUN:RANG1 0.200001", "SENS:CURR:SHUN:RANG1?", "0.2", "-222" },
        { "SENS:CURR:SHUN 0.1", "SENS:CURR:SHUN?", "0.1", "0" },
        { "SENS:CURR:THR -1.6384", "SENS:CURR:THR?", "-1.6384", "0" },
        { "SENS:CURR:THR -1.638400001", "SENS:CURR:THR?", "-1.6384", "-222" },
        { "SENS:CURR:THR 1.6384", "SENS:CURR:THR?", "1.6384", "0" },
        { "SENS:CURR:THR 1.638400001", "SENS:CURR:THR?", "1.6384", "-222" },
        { "SENS:CURR:THR OFF", "SENS:CURR:THR?", "OFF", "0" },
        { "SENS:CURR:THR:HYST 0", "SENS:CURR:THR:HYST?", "0.0", "0" },
        { "SENS:CURR:THR:HYST -0.000000001", "SENS:CURR:THR:HYST?", "0.0", "-222" },
        { "SENS:CURR:THR:HYST 1.6384", "SENS:CURR:THR:HYST?", "1.6384", "0" },
        { "SENS:CURR:THR:HYST 1.638400001", "SENS:CURR:THR:HYST?", "1.6384", "-222" },
        { "TRIG:LEV -1.6384", "TRIG:LEV?", "-1.6384", "0" },
        { "TRIG:LEV -1.638400001", "TRIG:LEV?", "-1.6384", "-222" },
        { "TRIG:LEV 1.6384", "TRIG:LEV?", "1.6384", "0" },
        { "TRIG:LEV 1.638400001", "TRIG:LEV?", "1.6384", "-222" },
        // The pretrigger's window is the ring's: the harness's 65,536 samples.
        { "TRIG:PRET 0", "TRIG:PRET?", "0", "0" },
        { "TRIG:PRET -1", "TRIG:PRET?", "0", "-222" },
        { "TRIG:PRET 65535", "TRIG:PRET?", "65535", "0" },
        { "TRIG:PRET 65536", "TRIG:PRET?", "65535", "-222" },
        { "SAMP:COUN 1", "SAMP:COUN?", "1", "0" },
        { "SAMP:COUN 0", "SAMP:COUN?", "1", "-222" },
        { "SAMP:COUN 1000000", "SAMP:COUN?", "1000000", "0" },
        { "SAMP:COUN 1000001", "SAMP:COUN?", "1000000", "-222" },
        // An integer far past 2^64 is out of range too, not a wrapped value.
        { "SAMP:COUN 99999999999999999999", "SAMP:COUN?", "1000000", "-222" },
        // The range in amperes, to the microampere: the narrow range holds up
        // to 0.4096 A, the wide one up to 1.6384 A, and nothing below 0.
        { "SENS:CURR:RANG 0.4096", "SENS:CURR:RANG:IND?", "1", "0" },
        { "SENS:CURR:RANG 0.409601", "SENS:CURR:RANG:IND?", "0", "0" },
        { "SENS:CURR:RANG 0", "SENS:CURR:RANG:IND?", "1", "0" },
        { "SENS:CURR:RANG -0.000001", "SENS:CURR:RANG:IND?", "1", "-222" },
        { "SENS:CURR:RANG 1.6384", "SENS:CURR:RANG:IND?", "0", "0" },
        { "SENS:CURR:RANG 1.638401", "SENS:CURR:RANG:IND?", "0", "-222" },
        { "SENS:CURR:RANG:IND 0", "SENS:CURR:RANG:IND?", "0", "0" },
        { "SENS:CURR:RANG:IND -1", "SENS:CURR:RANG:IND?", "0", "-222" },
        { "SENS:CURR:RANG:IND 1", "SENS:CURR:RANG:IND?", "1", "0" },
        { "SENS:CURR:RANG:IND 2", "SENS:CURR:RANG:IND?", "1", "-222" },
        { "SENS:CURR:RANG:IND 0", "SENS:CURR:RANG:IND?", "0", "0" },
        { "CAL:SHUN:REF 1", "CAL:SHUN:REF?", "1.0", "0" },
        { "CAL:SHUN:REF 0.999999", "CAL:SHUN:REF?", "1.0", "-222" },
        { "CAL:SHUN:REF 4000", "CAL:SHUN:REF?", "4000.0", "0" },
        { "CAL:SHUN:REF 4000.000001", "CAL:SHUN:REF?", "4000.0", "-222" },
        // A calendar date in 2026..2099: each part at its edges, and the
        // lengths of the months that are not 31 days long.
        { "CAL:DATE 2026,1,1", "CAL:DATE?", "2026,1,1", "0" },
        { "CAL:DATE 2025,12,31", "CAL:DATE?", "2026,1,1", "-222" },
        { "CAL:DATE 2099,12,31", "CAL:DATE?", "2099,12,31", "0" },
        { "CAL:DATE 2100,1,1", "CAL:DATE?", "2099,12,31", "-222" },
        { "CAL:DATE 2026,0,1", "CAL:DATE?", "2099,12,31", "-222" },
        { "CAL:DATE 2026,13,1", "CAL:DATE?", "2099,12,31", "-222" },
        { "CAL:DATE 2026,1,0", "CAL:DATE?", "2099,12,31", "-222" },
        { "CAL:DATE 2026,1,32", "CAL:DATE?", "2099,12,31", "-222" },
        { "CAL:DATE 2026,4,30", "CAL:DATE?", "2026,4,30", "0" },
        { "CAL:DATE 2026,4,31", "CAL:DATE?", "2026,4,30", "-222" },
        { "CAL:DATE 2028,2,29", "CAL:DATE?", "2028,2,29", "0" },
        { "CAL:DATE 2027,2,29", "CAL:DATE?", "2028,2,29", "-222" },
        { "CAL:DATE 2027,2,28", "CAL:DATE?", "2027,2,28", "0" },
        // A tag of 23 characters at most, and of one at least; a longer one
        // is a string too long for its room (-150), an empty one no token.
        { "CAL:TAG \"abcdefghijklmnopqrstuvw\"", "CAL:TAG?", "\"abcdefghijklmnopqrstuvw\"", "0" },
        { "CAL:TAG \"abcdefghijklmnopqrstuvwx\"", "CAL:TAG?", "\"abcdefghijklmnopqrstuvw\"",
          "-150" },
        { "CAL:TAG \"a\"", "CAL:TAG?", "\"a\"", "0" },
        { "CAL:TAG \"\"", "CAL:TAG?", "\"a\"", "-224" },
    };
    for ( const Edge& edge : edges ) {
        b.tell( edge.order, 100'000 );
        CHECK_EQ( std::string( edge.order ) + " -> " + b.ask( edge.query ),
                  std::string( edge.order ) + " -> " + edge.answer );
        CHECK_EQ( std::string( edge.order ) + " -> " + leadingCode( b.errors() ),
                  std::string( edge.order ) + " -> " + edge.code );
    }
}

TEST( "sampler-sides", "the windows that open something, at their edges" ) {
    Bench b;
    b.world().circuit.dutNa = 0;
    // CALibration:ZERO, 1..600 s: both ends open a window, past them none.
    for ( const char* seconds : { "0", "601" } ) {
        b.tell( std::string( "SENS:PROF STATE;:CAL:ZERO " ) + seconds );
        CHECK_EQ( leadingCode( b.errors() ), "-222" );
        CHECK_EQ( field( b.ask( "SYST:STAT?" ), "sink" ), "idle" );
    }
    for ( const char* seconds : { "1", "600" } ) {
        b.tell( std::string( "CAL:ZERO " ) + seconds, 20'000 );
        const std::string open = b.ask( "CAL:ZERO:RES?" );
        CHECK_EQ( field( open, "running" ), "1" );
        CHECK_EQ( field( open, "over_s" ), seconds );
        b.tell( "ABOR" );
        CHECK( b.clean() );
    }
    // ROUTe:LEAD:CYCLe, 1..60000 ms: K1 opens for exactly that long.
    for ( const char* ms : { "0", "60001" } ) {
        b.tell( std::string( "ROUT:LEAD:CYCL " ) + ms + ",FORC" );
        CHECK_EQ( leadingCode( b.errors() ), "-222" );
        CHECK( !b.world().leadOpen() );
    }
    b.tell( "ROUT:LEAD:CYCL 1,FORC", 0 );
    b.world().run( 500 );
    CHECK( b.world().leadOpen() );
    b.world().run( 200'000 );
    CHECK( !b.world().leadOpen() );
    b.tell( "ROUT:LEAD:CYCL 60000,FORC", 1'000 );
    b.world().run( 59'000'000 );
    CHECK( b.world().leadOpen() );
    b.world().run( 1'200'000 );
    CHECK( !b.world().leadOpen() );
    CHECK( b.clean() );
    // TRACe:MARK, 1..15 printable characters, on a running capture.
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    b.drain();
    b.tell( "TRAC:MARK \"a\";:TRAC:MARK \"abcdefghijklmno\"", 20'000 );
    const std::string marked = b.drain();  // asking a question takes the line
    CHECK_CONTAINS( marked, "METER_MARK mark=a n=" );
    CHECK_CONTAINS( marked, "METER_MARK mark=abcdefghijklmno n=" );
    CHECK( b.clean() );
    b.tell( "TRAC:MARK \"abcdefghijklmnop\"", 20'000 );
    CHECK( b.drain().find( "abcdefghijklmnop" ) == std::string::npos );
    CHECK_EQ( leadingCode( b.errors() ), "-224" );
    b.tell( "TRAC:MARK \"\"" );
    CHECK_EQ( leadingCode( b.errors() ), "-224" );
    b.tell( "ABOR" );
    // TRACe:DATA?: a place among the samples the ring holds, and a count of
    // at least one; the last place dumps one row, the next is past the ring.
    b.tell( "SENS:PROF TRAC;:INIT", 50'000 );
    b.tell( "ABOR" );
    const unsigned long captured = std::stoul( b.ask( "TRAC:POIN?" ) );
    REQUIRE( captured > 1U );
    const std::string last = std::to_string( captured - 1U );
    b.drain();
    b.world().send( "TRAC:DATA? RAW," + last );
    b.world().run( 100'000 );
    CHECK_CONTAINS( b.drain(), "METER_DUMP count=1 " );
    b.world().send( "TRAC:DATA? RAW,0,1" );
    b.world().run( 100'000 );
    CHECK_CONTAINS( b.drain(), "METER_DUMP count=1 " );
    CHECK( b.clean() );
    b.tell( "TRAC:DATA? RAW," + std::to_string( captured ) );
    CHECK_EQ( leadingCode( b.errors() ), "-222" );
    b.tell( "TRAC:DATA? RAW,-1" );
    CHECK_EQ( leadingCode( b.errors() ), "-222" );
    b.tell( "TRAC:DATA? RAW,0,0" );
    CHECK_EQ( leadingCode( b.errors() ), "-222" );
    b.tell( "TRAC:DATA? RAW,0,-1" );
    CHECK_EQ( leadingCode( b.errors() ), "-222" );
    // The numbers of a header: the shunt's ranges are 0 and 1, the spare
    // relays 3 and 4 (neither wired on the rig: -241), the others no suffix
    // of theirs (-114).
    b.tell( "SENS:CURR:SHUN:RANG2 0.1" );
    CHECK_EQ( leadingCode( b.errors() ), "-114" );
    for ( const char* relay : { "2", "5" } ) {
        b.tell( std::string( "ROUT:REL" ) + relay + " ON" );
        CHECK_EQ( leadingCode( b.errors() ), "-114" );
    }
    for ( const char* relay : { "3", "4" } ) {
        b.tell( std::string( "ROUT:REL" ) + relay + " ON" );
        CHECK_EQ( leadingCode( b.errors() ), "-241" );
    }
    // An acquisition may last 600 s: SLEEP's 67.52 ms period allows 8,886
    // samples (599.98 s) and refuses 8,887 (600.05 s) before it starts.
    b.tell( "SENS:PROF SLE;:SAMP:COUN 8887" );
    b.tell( "MEAS:CURR?" );
    CHECK_EQ( leadingCode( b.errors() ), "-222" );
    CHECK_EQ( field( b.ask( "SYST:STAT?" ), "sink" ), "idle" );
    b.tell( "SAMP:COUN 8886" );
    b.world().send( "MEAS:CURR?" );
    b.world().run( 1'000'000 );
    // It started: only an ABORt ends it now, after the samples it took.
    b.world().sendRaw( "ABOR\r" );
    b.world().run( 300'000 );
    const std::string aborted = b.errors();
    CHECK( aborted.rfind( "-230,\"Data corrupt or stale;the acquisition was aborted after ", 0 )
           == 0 );
    CHECK_CONTAINS( aborted, " of 8886 samples" );
}
