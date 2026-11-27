// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_sampler.cpp
 * @brief sampler.cpp on its own, with its task running on the simulated board:
 *        bring-up failures, the pace probe and its fallback, the ring, the
 *        stream, the null, marks, read failures and the statistics.
 */
#include "sampler.h"
#include "scope.h"
#include "unit.h"

#include <cstring>
#include <string>

using namespace kleidos::meter;
using bench::Scope;

namespace {

struct Engine {
    Scope            scope;
    Ina228Driver     device;
    CalibrationStore calibration;
    Sampler          sampler;
    bool             ready = false;

    explicit Engine( void ( *setup )( sim::World& ) = nullptr ) {
        if ( setup != nullptr ) {
            setup( *scope.world );
        }
        (void)device.openBus( GPIO_NUM_4, GPIO_NUM_5, 1000000, {} );
        (void)device.bringUp( Ina228Driver::kScanForAddress );
        ready = sampler.begin( device, calibration, GPIO_NUM_6, true, scope->ring() );
    }
};

std::uint8_t currentOnly() {
    return channelBit( Channel::CurrentMicroamps );
}

}  // namespace

TEST( "sampler", "a profile outside the table reads as TRACE's" ) {
    CHECK_EQ( std::string( profileSpec( static_cast<Profile>( 9 ) ).name ), "TRACE" );
}

TEST( "sampler", "begin fails cleanly on each object it cannot build" ) {
    {
        Engine e( []( sim::World& w ) { w.faults.failQueue = true; } );
        CHECK( !e.ready && !e.sampler.engineReady() );
    }
    for ( int which = 0; which < 4; ++which ) {  // conversion, start, done, probe done
        Scope scope;
        scope->faults.failSemaphore   = true;
        scope->faults.failSemaphoreAt = which;
        Ina228Driver     device;
        CalibrationStore calibration;
        Sampler          sampler;
        CHECK( !sampler.begin( device, calibration, GPIO_NUM_6, true, scope->ring() ) );
    }
    {
        Engine e( []( sim::World& w ) {
            w.faults.failGpioConfig    = true;
            w.faults.failGpioConfigPin = 6;
        } );
        CHECK( !e.ready );
    }
    {
        // No free CPU line on the core: every level-1 line has a handler.
        Engine e( []( sim::World& w ) {
            while ( w.allocatorTakesLine( 0 ) >= 0 ) {}
        } );
        CHECK( !e.ready );
        CHECK( e.scope->cpus[0].routes.count( sim::kGpioSource ) == 0 );
    }
    for ( const gpio_num_t pin : { GPIO_NUM_38, GPIO_NUM_NC } ) {
        // Outside GPIO0 to 31: its status is in a register alertIsr() does
        // not read, and GPIO_NUM_NC is no pin. Refused before anything is
        // configured or routed.
        Scope            scope;
        Ina228Driver     device;
        CalibrationStore calibration;
        Sampler          sampler;
        CHECK( !sampler.begin( device, calibration, pin, true, scope->ring() ) );
        CHECK( scope->cpus[0].routes.empty() && scope->gpioIsr.empty() );
    }
    {
        Engine e( []( sim::World& w ) { w.faults.failTask = true; } );
        CHECK( !e.ready );
        CaptureInfo info;
        const char* error = nullptr;
        CHECK( !e.sampler.start( Profile::State, currentOnly(), info, error ) );
        CHECK_EQ( std::string( error ), "sampler engine failed to start (reboot)" );
    }
}

TEST( "sampler", "start refuses with a reason: open capture, no device, channels, rate, bus" ) {
    Engine      e;
    CaptureInfo info;
    const char* error = nullptr;
    CHECK( !e.sampler.start( Profile::State, 0, info, error ) );
    CHECK_EQ( std::string( error ), "no channels selected" );
    REQUIRE( e.device.setBusSpeed( 100000 ) == ESP_OK );
    CHECK( !e.sampler.start( Profile::Trace, 0, info, error ) );
    CHECK_CONTAINS( error, "needs a faster bus" );
    REQUIRE( e.device.setBusSpeed( 1000000 ) == ESP_OK );
    e.scope->ina.failCount = 1;
    CHECK( !e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK_EQ( std::string( error ), "i2c write failed" );
    CHECK( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK( !e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK_EQ( std::string( error ), "capture running (ABORt first)" );
    CHECK( !e.sampler.setProfile( Profile::Sleep, error ) );
    CaptureStats stats;
    CHECK( e.sampler.stop( stats ) );
    CHECK( !e.sampler.stop( stats ) );
    CHECK( e.sampler.setProfile( Profile::Sleep, error ) );

    Scope            bare;
    Ina228Driver     absent;
    CalibrationStore calibration;
    Sampler          sampler;
    REQUIRE( sampler.begin( absent, calibration, GPIO_NUM_6, true, bare->ring() ) );
    CHECK( !sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK_EQ( std::string( error ), "no device (scan found none)" );
}

TEST( "sampler", "another pin's interrupt on the ALERT line is cleared and not counted" ) {
    Engine e;
    REQUIRE( e.ready );
    // GPIO7 latches a falling edge every 300 us with its interrupt enabled:
    // the GPIO's one source raises the ALERT pin's line for it too.
    e.scope->gpioIntrType[7] = GPIO_INTR_NEGEDGE;
    e.scope->gpioIntrEnable |= 1ULL << 7U;
    sim::World*       world           = e.scope.world;
    const std::size_t noise           = world->addInterrupt( [world] { world->raiseGpio( 7 ); } );
    world->interrupt( noise ).period  = 300;
    world->interrupt( noise ).next    = world->now() + 300;
    world->interrupt( noise ).enabled = true;
    // The probe still counts one edge per conversion, so STATE stays
    // alert-paced: the 30-odd GPIO7 edges of its window were not ALERT's.
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK( info.pace == Pace::Alert );
    CHECK( info.paceEdges >= 7 && info.paceEdges <= 9 );
    e.scope->run( 20'000 );
    // Every one was cleared: no level line left raised (Scope checks storms).
    CHECK( ( e.scope->gpioStatus & ( 1U << 7U ) ) == 0 );
    world->interrupt( noise ).enabled = false;
    CaptureStats stats;
    CHECK( e.sampler.stop( stats ) );
    CHECK( stats.staleEdges == 0 );
}

TEST( "sampler", "a streamed capture: samples, statistics, the pace kept" ) {
    Engine e;
    e.scope->circuit.dutNa = 10'000'000;
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK( info.pace == Pace::Alert );  // the part as it is: held until read
    CHECK( info.dieMilliCelsiusStart == 25'000 );
    CHECK( info.chargeValidStart );
    CHECK_EQ( std::string( e.sampler.sinkName() ), "stream" );
    e.scope->run( 50'000 );
    Sample sample;
    int    taken = 0;
    while ( e.sampler.nextStreamSample( sample, 0 ) ) {
        ++taken;
    }
    CHECK( taken > 30 );
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.samples >= static_cast<std::uint32_t>( taken ) );
    CHECK( !stats.paceUnmet );
    CHECK( stats.chargeValid );
    CHECK( stats.minNanoamps <= stats.maxNanoamps );
    CHECK( stats.meanPicoamps > 9'990'000'000LL );
    CHECK( e.sampler.dieAgeUs() < 100'000 );
    CHECK_EQ( std::string( e.sampler.sinkName() ), "idle" );
}

TEST( "sampler", "the pacing probe runs in the sampler task, and a stuck one says timer" ) {
    // 0.16.2: the probe reads DIAG_ALRT where the capture will, never on the
    // caller's core, whose other work delayed its reads past a conversion.
    Engine e;
    REQUIRE( e.ready );
    e.scope->ina.diagReads          = 0;
    e.scope->ina.diagReadsFromTasks = 0;
    CHECK( e.sampler.probeAlert() );
    CHECK( e.scope->ina.diagReads >= 8 );
    CHECK( e.scope->ina.diagReadsFromTasks == e.scope->ina.diagReads );

    // A probe whose first read holds the bus past its window and the answer's
    // margin is not waited for: it says timer, and the next one paces.
    e.scope->ina.stallReadRegister = static_cast<int>( kleidos::meter::ina228::kRegDiagAlrt );
    e.scope->ina.stallReadUs       = 3'000'000;
    CHECK( !e.sampler.probeAlert() );
    e.scope->run( 3'500'000 );
    CaptureInfo  info;
    const char*  error = nullptr;
    CaptureStats stats;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK( info.pace == Pace::Alert );
    CHECK( info.paceEdges >= 7 && info.paceEdges <= 9 );
    REQUIRE( e.sampler.stop( stats ) );
}

TEST( "sampler", "a probe before a timed-out stop is acknowledged says timer, asks nothing" ) {
    // 0.16.3: a loop still running cannot take the request until it ends, and
    // would then run the probe on its own, over whatever the console had set
    // up by then. So the probe waits for the acknowledgement, as a start does.
    Engine      e;
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    e.scope->run( 20'000 );
    e.scope->ina.stallNextUs = 2'000'000;
    e.scope->run( 5'000 );
    CaptureStats stats;
    CHECK( !e.sampler.stop( stats ) );  // the loop holds the bus: no acknowledgement

    e.scope->ina.diagReadsFromTasks = 0;
    const sim::Micros asked         = e.scope->now();
    CHECK( !e.sampler.probeAlert() );
    // Answered once the self-test's two settling periods (0.16.4) have
    // passed, not after a probe's window and its 500 ms.
    CHECK( e.scope->now() - asked < 2 * 1'152 + 1'000 );
    e.scope->run( 2'500'000 );  // the loop ends and acknowledges
    // At most the stalled sample's own read, never a probe's eight.
    CHECK( e.scope->ina.diagReadsFromTasks < 8 );

    CHECK( e.sampler.probeAlert() );  // acknowledged since: the task probes
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK( info.pace == Pace::Alert );
    CHECK( info.paceEdges >= 7 && info.paceEdges <= 9 );
    e.scope->run( 50'000 );
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.samples > 0 );
}

TEST( "sampler", "ALERT paces the capture, and a dead pin falls back to the timer" ) {
    Engine      e( []( sim::World& w ) { w.ina.alertPin = sim::Ina228Model::AlertPin::Pulses; } );
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::Sleep, currentOnly(), info, error ) );
    CHECK( info.pace == Pace::Alert );
    CHECK( info.paceEdges >= 7 && info.paceEdges <= 9 );
    e.scope->run( 400'000 );
    CHECK( !e.sampler.paceFellBack() );
    e.scope->ina.alertPin = sim::Ina228Model::AlertPin::Absent;
    e.scope->run( 400'000 );
    CHECK( e.sampler.paceFellBack() );
    CHECK( e.sampler.effectivePace() == Pace::Timer );
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.paceFellBack );
}

TEST( "sampler", "STATE and SLEEP probe a pin held until read; TRACE and BURST never probe" ) {
    Engine       e;  // the part on the bench: the pin stays low until DIAG_ALRT is read
    CaptureInfo  info;
    const char*  error = nullptr;
    CaptureStats stats;
    for ( const Profile profile : { Profile::State, Profile::Sleep } ) {
        REQUIRE( e.sampler.start( profile, currentOnly(), info, error ) );
        CHECK( info.pace == Pace::Alert );
        CHECK( info.paceEdges >= 7 && info.paceEdges <= 9 );
        CHECK( info.paceExpected == 8 );
        REQUIRE( e.sampler.stop( stats ) );
    }
    for ( const Profile profile : { Profile::Trace, Profile::Burst } ) {
        REQUIRE( e.sampler.start( profile, 0, info, error ) );
        CHECK( info.pace == Pace::Timer );
        CHECK( info.paceEdges == 0 );
        CHECK( info.paceExpected == 0 );
        REQUIRE( e.sampler.stop( stats ) );
        CHECK( stats.staleEdges == 0 );
    }

    // A wire off its terminal: no edge, so the timer paces, and SLEEP still
    // reads DIAG_ALRT every sample for its flags.
    Engine dead( []( sim::World& w ) { w.ina.alertPin = sim::Ina228Model::AlertPin::Absent; } );
    REQUIRE( dead.sampler.start( Profile::Sleep, currentOnly(), info, error ) );
    CHECK( info.pace == Pace::Timer );
    CHECK( info.paceEdges == 0 );
    dead.scope->ina.diagFlags = kleidos::meter::ina228::kDiagShntOl;
    dead.scope->run( 300'000 );
    REQUIRE( dead.sampler.stop( stats ) );
    CHECK( stats.samples >= 3 );
    CHECK( ( stats.flags & kleidos::meter::ina228::kDiagShntOl ) != 0 );
}

TEST( "sampler", "an alert-paced STATE capture: a sample per conversion, stamped at its edge" ) {
    Engine      e;
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    REQUIRE( info.pace == Pace::Alert );
    e.scope->run( 100'000 );
    Sample        sample;
    std::uint64_t previous = 0;
    int           taken    = 0;
    int           off      = 0;
    while ( e.sampler.nextStreamSample( sample, 0 ) ) {
        if ( previous != 0 ) {
            const auto gap = static_cast<std::int64_t>( sample.timestampUs - previous );
            off += ( gap < 1152 - 5 || gap > 1152 + 5 ) ? 1 : 0;
        }
        previous = sample.timestampUs;
        ++taken;
    }
    CHECK( taken >= 80 );
    CHECK( off == 0 );  // no conversion missed, none read twice
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( !stats.paceFellBack );
    CHECK( stats.staleEdges == 0 );
    CHECK( stats.paceErrorPpm > -1000 && stats.paceErrorPpm < 1000 );
    // The DIAG_ALRT read that releases the pin carries its flags: CNVRF and
    // the latch the clip detector rests on.
    CHECK( ( stats.flags & kleidos::meter::ina228::kDiagCnvrf ) != 0 );
}

TEST( "sampler", "a glitch on the ALERT wire is counted and takes no sample" ) {
    Engine      e;
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    REQUIRE( info.pace == Pace::Alert );
    e.scope->run( 20'000 );
    e.scope->ina.alertGlitches = 3;
    e.scope->run( 40'000 );
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.staleEdges == 3 );
    CHECK( !stats.paceFellBack );
    CHECK( stats.readFailures == 0 );
}

TEST( "sampler", "a DIAG_ALRT read that fails leaves the pin low: the timer takes over" ) {
    Engine      e;
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    REQUIRE( info.pace == Pace::Alert );
    e.scope->run( 10'000 );
    e.scope->ina.failRegister = kleidos::meter::ina228::kRegDiagAlrt;
    e.scope->ina.failCount    = 1;
    e.scope->run( 60'000 );
    CHECK( e.sampler.paceFellBack() );
    CHECK( e.sampler.effectivePace() == Pace::Timer );
    e.scope->ina.failRegister = -1;
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.paceFellBack );
    CHECK( stats.readFailures == 0 );  // the sample's own reads all succeeded
}

TEST( "sampler", "the pace error is the samples' own intervals, not the start's phase" ) {
    // A few SLEEP samples, each a conversion of the simulated oscillator, which
    // keeps its period exactly. From the capture's start, the span would carry
    // the start's phase against the first conversion, up to a period, as an
    // error of up to 1 / samples: thousands of ppm on five samples. From the
    // first sample it is the oscillator alone.
    Engine      e;
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::Sleep, currentOnly(), info, error ) );
    REQUIRE( info.pace == Pace::Alert );
    // The part's grid half a period on from where the start left it, as the
    // bus traffic between the probe and the loop leaves it on the bench: the
    // loop's first edge then comes half a period after the capture's start.
    e.scope->ina.convertingSince -= 67'520 / 2;
    e.scope->run( 6 * 67'520 );
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    REQUIRE( stats.samples >= 4 );
    CHECK( stats.paceErrorPpm > -100 && stats.paceErrorPpm < 100 );
    CHECK( !stats.paceUnmet );
}

TEST( "sampler", "TRACE fills its ring, anchors it and ends itself" ) {
    Engine e( []( sim::World& w ) { w.ringSamples = 4096; } );
    e.scope->ina.dieMc = 26'000;
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::Trace, 0, info, error ) );
    CHECK( info.ringSamples == 4096 );
    CHECK( info.dieMilliCelsiusStart == 26'000 );  // the triggered conversion
    Mark mark;
    CHECK( e.sampler.addMark( "start", mark ) );
    CHECK( e.scope->runUntil( [&] { return !e.sampler.running(); }, 3'000'000 ) );
    CHECK( e.sampler.captureOpen() );  // finished, awaiting STOP
    CHECK_EQ( std::string( e.sampler.sinkName() ), "ring" );
    CHECK( e.sampler.ringCount() == 4096 );
    CHECK( e.sampler.anchorCount() == 4 );
    CHECK( e.sampler.anchorAt( 1 ).index == kAnchorEvery );
    CHECK( e.sampler.markCount() == 1 );
    CHECK( e.sampler.ringAt( 0 ) == e.sampler.ringAt( 4095 ) );
    CHECK( e.sampler.ringAt( 5000 ) == 0 );
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.samples == 4096 );
}

TEST( "sampler",
      "the ring is the caller's array, used whole, and begin refuses one out of bounds" ) {
    {
        Engine      e( []( sim::World& w ) { w.ringSamples = 8192; } );
        CaptureInfo info;
        const char* error = nullptr;
        CHECK( e.sampler.ringCapacity() == 8192 );  // before any capture
        REQUIRE( e.sampler.start( Profile::Trace, 0, info, error ) );
        CHECK( info.ringSamples == 8192 );
        CaptureStats stats;
        CHECK( e.sampler.stop( stats ) );
        // A second capture reuses the same ring.
        REQUIRE( e.sampler.start( Profile::Trace, 0, info, error ) );
        CHECK( e.sampler.stop( stats ) );
    }
    for ( const std::size_t samples :
          { std::size_t{ kTraceSamplesFloor - 1 }, std::size_t{ kTraceSamples + 1 } } ) {
        Scope scope;
        scope->ringSamples = samples;
        Ina228Driver     device;
        CalibrationStore calibration;
        Sampler          sampler;
        CHECK( !sampler.begin( device, calibration, GPIO_NUM_6, true, scope->ring() ) );
        CHECK( !sampler.engineReady() );
    }
}

TEST( "sampler", "a ring capture whose pointer write keeps failing ends with no sample" ) {
    // The ring path reads from the retained pointer: a failed write would
    // leave it on DIAG_ALRT, whose bytes every read would then return as shunt
    // counts (HM2-06). Each attempt fails, so nothing is stored and the
    // failure is counted.
    Engine      e( []( sim::World& w ) { w.ringSamples = 4096; } );
    CaptureInfo info;
    const char* error         = nullptr;
    e.scope->ina.failRegister = kleidos::meter::ina228::kRegVShunt;
    e.scope->ina.failCount    = 1000;
    REQUIRE( e.sampler.start( Profile::Trace, 0, info, error ) );
    CHECK( e.scope->runUntil( [&] { return !e.sampler.running(); }, 3'000'000 ) );
    CHECK( e.sampler.ringCount() == 0 );
    CHECK( e.sampler.readFailures() == 1 );
    e.scope->ina.failCount    = 0;
    e.scope->ina.failRegister = -1;
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.samples == 0 && stats.readFailures == 1 );
}

TEST( "sampler", "a ring capture's pointer write that fails once is written again" ) {
    // What the ring holds when nothing fails: the shunt register's counts.
    std::int32_t expected = 0;
    {
        Engine      clean( []( sim::World& w ) { w.ringSamples = 4096; } );
        CaptureInfo info;
        const char* error = nullptr;
        REQUIRE( clean.sampler.start( Profile::Trace, 0, info, error ) );
        CHECK( clean.scope->runUntil( [&] { return !clean.sampler.running(); }, 3'000'000 ) );
        expected = clean.sampler.ringAt( 0 );
        CaptureStats stats;
        REQUIRE( clean.sampler.stop( stats ) );
    }
    for ( const int failures : { 1, 2 } ) {
        Engine      e( []( sim::World& w ) { w.ringSamples = 4096; } );
        CaptureInfo info;
        const char* error         = nullptr;
        e.scope->ina.failRegister = kleidos::meter::ina228::kRegVShunt;
        e.scope->ina.failCount    = failures;
        REQUIRE( e.sampler.start( Profile::Trace, 0, info, error ) );
        CHECK( e.scope->runUntil( [&] { return !e.sampler.running(); }, 3'000'000 ) );
        CHECK( e.sampler.ringCount() == 4096 );
        CHECK( e.sampler.readFailures() == 0 );
        CHECK( e.sampler.ringAt( 0 ) == expected && e.sampler.ringAt( 4095 ) == expected );
        e.scope->ina.failRegister = -1;
        CaptureStats stats;
        REQUIRE( e.sampler.stop( stats ) );
    }
}

TEST( "sampler", "a read that fails is counted and anchored, never silently skipped" ) {
    Engine      e( []( sim::World& w ) { w.ringSamples = 4096; } );
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::Trace, 0, info, error ) );
    e.scope->run( 20'000 );
    e.scope->ina.failCount = 3;
    CHECK( e.scope->runUntil( [&] { return !e.sampler.running(); }, 3'000'000 ) );
    CHECK( e.sampler.readFailures() == 3 );
    CHECK( e.sampler.anchorCount() >= 5 );
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.readFailures == 3 );
}

TEST( "sampler", "STATE and SLEEP readings tolerate a failed bus or die read" ) {
    Engine      e;
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    e.scope->ina.failRegister = kleidos::meter::ina228::kRegVBus;
    e.scope->ina.failCount    = 2;
    e.scope->run( 10'000 );
    e.scope->ina.failRegister = kleidos::meter::ina228::kRegDieTemp;
    e.scope->ina.failCount    = 2;
    e.scope->run( 10'000 );
    e.scope->ina.failRegister = kleidos::meter::ina228::kRegVShunt;
    e.scope->ina.failCount    = 1;
    e.scope->run( 10'000 );
    e.scope->ina.failRegister = kleidos::meter::ina228::kRegDiagAlrt;
    e.scope->ina.failCount    = 1;
    e.scope->ina.failRegister = -1;
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.readFailures == 1 );
}

TEST( "sampler", "a full stream queue counts drops; the stop reads still close" ) {
    Engine      e;
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    e.scope->run( 400'000 );  // nobody drains the queue
    CHECK( e.sampler.dropped() > 0 );
    e.sampler.noteDropped( 2 );
    // The boundary reads at the stop fail: no flags, no charge, no die.
    e.scope->ina.failAfter = 0;
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( !stats.chargeValid );
    CHECK( stats.dieMilliCelsiusEnd == kDieMilliCelsiusUnknown );
    e.scope->ina.failAfter = -1;
}

TEST( "sampler", "the boundary reads at a start may fail without refusing it" ) {
    Engine e;
    e.scope->ina.failRegister = kleidos::meter::ina228::kRegVShunt;
    e.scope->ina.failCount    = 1;  // the consistency delta
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK( info.shuntCurrentDelta == 0 );
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    e.scope->ina.failRegister = kleidos::meter::ina228::kRegDieTemp;
    e.scope->ina.failCount    = 1;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK( info.dieMilliCelsiusStart == kDieMilliCelsiusUnknown );
    REQUIRE( e.sampler.stop( stats ) );
    e.scope->ina.failRegister = kleidos::meter::ina228::kRegCharge;
    e.scope->ina.failCount    = 1;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK( !info.chargeValidStart );
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( !stats.chargeValid );
}

TEST( "sampler", "the null: mean, deviation, early stop and its refusals" ) {
    Engine e;
    e.scope->circuit.dutNa   = 0;
    e.scope->ina.noiseCounts = 2;
    const char* error        = nullptr;
    CHECK( !e.sampler.startZero( 0, error ) );
    CHECK( !e.sampler.startZero( kZeroMaxSeconds + 1, error ) );
    REQUIRE( e.sampler.setProfile( Profile::Sleep, error ) );
    CHECK( e.sampler.zeroSampleCount( 1 ) == 14 );
    REQUIRE( e.sampler.startZero( 1, error ) );
    CHECK( e.sampler.zeroOpen() );
    CHECK_EQ( std::string( e.sampler.sinkName() ), "null" );
    CHECK( e.scope->runUntil( [&] { return !e.sampler.running(); }, 3'000'000 ) );
    ZeroResult result;
    REQUIRE( e.sampler.finishZero( result ) );
    CHECK( result.samples == 14 );
    CHECK( result.seconds == 1 );
    CHECK( result.deviationPicovolts > 0 );
    CHECK( result.offsetPicovolts > -1'000'000 && result.offsetPicovolts < 2'000'000 );

    // Stopped early, after a single sample: a mean but no deviation.
    REQUIRE( e.sampler.setProfile( Profile::Sleep, error ) );
    REQUIRE( e.sampler.startZero( 60, error ) );
    CHECK( e.scope->runUntil( [&] { return e.sampler.sampleCount() >= 1; }, 1'000'000 ) );
    REQUIRE( e.sampler.finishZero( result ) );
    CHECK( result.samples >= 1 );

    // STATE at 1152 us: a 1 s null is 868 samples. A window shorter than one
    // period cannot be asked for in whole seconds, so the zero-sample refusal
    // is reached only by a profile slower than a second, which none is.
    REQUIRE( e.sampler.setProfile( Profile::State, error ) );
    CHECK( e.sampler.zeroSampleCount( 1 ) == 868 );
}

TEST( "sampler", "a null of no samples reports none" ) {
    Engine      e;
    const char* error = nullptr;
    REQUIRE( e.sampler.setProfile( Profile::Sleep, error ) );
    REQUIRE( e.sampler.startZero( 10, error ) );
    ZeroResult result;
    CHECK( !e.sampler.finishZero( result ) );
    CHECK( result.samples == 0 );
}

TEST( "sampler", "marks fill up and labels are bounded" ) {
    Engine e;
    Mark   mark;
    for ( std::size_t i = 0; i < kMaxMarks; ++i ) {
        CHECK( e.sampler.addMark( "m", mark ) );
    }
    CHECK( !e.sampler.addMark( "m", mark ) );
    Engine f;
    CHECK( f.sampler.addMark( "0123456789abcdefXYZ", mark ) );
    CHECK( std::strlen( mark.label ) == kMarkLabelCapacity );
    CHECK( f.sampler.addMark( nullptr, mark ) );
    CHECK( mark.label[0] == '\0' );
}

TEST( "sampler", "with no stream queue nothing is taken" ) {
    Scope   scope;
    Sampler sampler;
    Sample  sample;
    CHECK( !sampler.nextStreamSample( sample, 0 ) );
    CHECK( sampler.ringAt( 0 ) == 0 );
    CHECK( sampler.dieAgeUs() == 0 );
}

TEST( "sampler",
      "MC/DC: a ringing ALERT is not a pace, and a capture that outruns its period is flagged" ) {
    {
        Engine      e( []( sim::World& w ) {
            w.ina.alertPin     = sim::Ina228Model::AlertPin::Pulses;
            w.ina.alertDivider = 2;  // twice per conversion: 16 edges in the probe
        } );
        CaptureInfo info;
        const char* error = nullptr;
        REQUIRE( e.sampler.start( Profile::Sleep, currentOnly(), info, error ) );
        CHECK( info.pace == Pace::Timer );
        CaptureStats stats;
        CHECK( e.sampler.stop( stats ) );
    }
    Engine      e( []( sim::World& w ) { w.ina.alertPin = sim::Ina228Model::AlertPin::Pulses; } );
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::Sleep, currentOnly(), info, error ) );
    REQUIRE( info.pace == Pace::Alert );
    e.scope->ina.alertDivider = 3;
    e.scope->retimeAlert();
    e.scope->run( 1'000'000 );
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.paceErrorPpm < -kPaceErrorLimitPpm );
    CHECK( stats.paceUnmet );
}

TEST( "sampler", "a ring left circular past the anchor array keeps the last anchors, in order" ) {
    // A level trigger that never fires: the ring wraps for longer than the
    // anchor array holds, which then keeps the last kAnchorSlots anchors in
    // index order, the newest the last regular one written.
    Engine      e( []( sim::World& w ) { w.ringSamples = 4096; } );
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE(
        e.sampler.startTriggered( Profile::Trace, std::int64_t{ 1 } << 50, true, 0, info, error ) );
    const auto wanted = static_cast<std::uint32_t>( ( kAnchorSlots + 2 ) * kAnchorEvery );
    CHECK( e.scope->runUntil( [&] { return e.sampler.sampleCount() > wanted; }, 400'000'000 ) );
    REQUIRE( e.sampler.anchorCount() == kAnchorSlots );
    bool ordered = true;
    for ( std::size_t i = 1; i < e.sampler.anchorCount(); ++i ) {
        ordered = ordered && e.sampler.anchorAt( i ).index > e.sampler.anchorAt( i - 1 ).index;
    }
    CHECK( ordered );
    CHECK( e.sampler.anchorAt( kAnchorSlots - 1 ).index % kAnchorEvery == 0 );
    CHECK( e.sampler.anchorAt( kAnchorSlots - 1 ).index + kAnchorEvery > e.sampler.sampleCount() );
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( !stats.triggered );
}

TEST( "sampler", "MC/DC: each write of the start's configuration failing on its own" ) {
    for ( int step = 1; step <= 3; ++step ) {
        Engine e;
        e.scope->ina.failAfter = step;
        CaptureInfo info;
        const char* error = nullptr;
        CHECK( !e.sampler.start( Profile::State, currentOnly(), info, error ) );
        CHECK_EQ( std::string( error ), "i2c write failed" );
    }
    Engine e;
    e.scope->ina.failRegister = kleidos::meter::ina228::kRegCurrent;
    e.scope->ina.failCount    = 1;
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::State, currentOnly(), info, error ) );
    CHECK( info.shuntCurrentDelta == 0 );
    CaptureStats stats;
    CHECK( e.sampler.stop( stats ) );
}

TEST( "sampler", "MC/DC: the anchor table saturates and the ring still fills" ) {
    Engine      e( []( sim::World& w ) { w.ringSamples = 4096; } );
    CaptureInfo info;
    const char* error = nullptr;
    REQUIRE( e.sampler.start( Profile::Trace, 0, info, error ) );
    e.scope->ina.failEvery = 2;  // every other read fails: an anchor each time
    CHECK( e.scope->runUntil( [&] { return !e.sampler.running(); }, 10'000'000 ) );
    e.scope->ina.failEvery = 0;
    // The forced anchors stop at their cap, and none pushed a regular one out:
    // the ring's four, from index 0 on, are all still there (0.15.0).
    CHECK( e.sampler.anchorCount() == 4 + kMaxForcedAnchors );
    std::size_t regular = 0;
    for ( std::size_t i = 0; i < e.sampler.anchorCount(); ++i ) {
        regular += e.sampler.anchorAt( i ).index % kAnchorEvery == 0 ? 1U : 0U;
    }
    CHECK( regular == 4 );
    CHECK( e.sampler.anchorAt( 0 ).index == 0 );
    CaptureStats stats;
    REQUIRE( e.sampler.stop( stats ) );
    CHECK( stats.readFailures > kMaxForcedAnchors );
}
