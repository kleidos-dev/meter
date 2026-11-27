// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_watchdog.cpp
 * @brief The instrument's own watchdog (FW-11) and the sampler's supervision
 *        (0.14.0): Timer Group 0's MWDT set for a system reset after 5 s, fed
 *        only once the console, the sampler and Core 0's idle task have all
 *        checked in; each of them that stops resets the chip, a running
 *        capture that takes nothing is reported once, and the forced hang
 *        stops the console's check-ins.
 */
#include "bench.h"
#include "scope.h"
#include "unit.h"
#include "watchdog.h"

#include <hal/wdt_types.h>
#include <soc/periph_defs.h>

#include <string>

using bench::Bench;
using bench::field;
using bench::Scope;
using kleidos::meter::watchdog::Party;
namespace watchdog = kleidos::meter::watchdog;

namespace {

/// The reference count ESP-IDF keeps on Timer Group 0's bus clock.
int timerGroupReferences( const sim::World& world ) {
    return world.rccReferences.at( static_cast<std::size_t>( PERIPH_TIMG0_MODULE ) );
}

}  // namespace

TEST( "watchdog", "a round is complete only when every subscribed party checked in" ) {
    constexpr std::uint32_t all = watchdog::bitOf( Party::Console )
                                  | watchdog::bitOf( Party::Sampler )
                                  | watchdog::bitOf( Party::Idle0 );
    static_assert( watchdog::bitOf( Party::Console ) == 1U );
    static_assert( watchdog::bitOf( Party::Sampler ) == 2U );
    static_assert( watchdog::bitOf( Party::Idle0 ) == 4U );
    CHECK( watchdog::roundComplete( all, all ) );
    CHECK( watchdog::roundComplete( all | 8U, all ) );  // a stray bit changes nothing
    CHECK( !watchdog::roundComplete( all & ~2U, all ) );
    CHECK( !watchdog::roundComplete( 0U, 0U ) );  // nothing subscribed: no round
    CHECK( !watchdog::roundComplete( all, 0U ) );
}

TEST( "watchdog", "begin() sets the timer for a system reset after 5 s, clocked and locked" ) {
    Scope s;
    CHECK( watchdog::begin() );
    const sim::Mwdt& wdt = s->mwdt;
    CHECK( wdt.enabled );
    CHECK( wdt.locked );
    CHECK( wdt.stageAction[0] == static_cast<int>( WDT_STAGE_ACTION_RESET_SYSTEM ) );
    CHECK( wdt.stageAction[1] == static_cast<int>( WDT_STAGE_ACTION_OFF ) );
    CHECK( wdt.stageAction[2] == static_cast<int>( WDT_STAGE_ACTION_OFF ) );
    CHECK( wdt.stageAction[3] == static_cast<int>( WDT_STAGE_ACTION_OFF ) );
    CHECK( wdt.prescaler == 40000U );
    CHECK( wdt.stageHold[0] == 10000U );
    CHECK( wdt.stage0Us() == sim::Micros{ 5'000'000 } );
    CHECK( !wdt.flashboot );
    CHECK( !wdt.levelInterrupt );
    CHECK( !wdt.interruptEnabled );
    CHECK( wdt.cpuResetLength == static_cast<int>( WDT_RESET_SIG_LENGTH_3_2us ) );
    CHECK( wdt.sysResetLength == static_cast<int>( WDT_RESET_SIG_LENGTH_3_2us ) );
    CHECK( wdt.feeds >= 1U );
    // The group's clock taken as ESP-IDF's task watchdog took it: one
    // reference, the clock on and the group reset by its first user.
    CHECK( timerGroupReferences( *s.world ) == 1 );
    CHECK( s->timerGroups[0].busClock );
    CHECK( s->timerGroups[0].resets == 1 );
    CHECK( s->rccDepth == 0 );
    // Core 0's idle hook, and only Core 0's.
    CHECK( s->idleHooks[0].size() == std::size_t{ 1 } );
    CHECK( s->idleHooks[1].empty() );
    CHECK( watchdog::subscribed( Party::Idle0 ) );
    CHECK( !watchdog::subscribed( Party::Console ) );
    CHECK( !watchdog::subscribed( Party::Sampler ) );
}

TEST( "watchdog", "a timer group another user holds is not reset" ) {
    Scope s;
    s->rccReferences.at( static_cast<std::size_t>( PERIPH_TIMG0_MODULE ) ) = 1;
    CHECK( watchdog::begin() );
    CHECK( timerGroupReferences( *s.world ) == 2 );
    CHECK( s->timerGroups[0].resets == 0 );
    CHECK( s->mwdt.enabled );
}

TEST( "watchdog", "the feed waits for every subscribed party, and only for them" ) {
    Scope s;
    CHECK( watchdog::begin() );
    const std::size_t afterBegin = s->mwdt.feeds;
    // Only Core 0's idle task is waited for at first: its check-in feeds.
    watchdog::checkIn( Party::Console );  // not subscribed: ignored
    CHECK( s->mwdt.feeds == afterBegin );
    watchdog::checkIn( Party::Idle0 );
    CHECK( s->mwdt.feeds == afterBegin + 1 );

    // Each task subscribes, which checks it in.
    CHECK( watchdog::subscribe( Party::Console ) );
    CHECK( watchdog::subscribe( Party::Sampler ) );
    CHECK( watchdog::subscribed( Party::Console ) );
    CHECK( watchdog::subscribed( Party::Sampler ) );
    CHECK( s->mwdt.feeds == afterBegin + 1 );
    watchdog::checkIn( Party::Idle0 );  // the round: console, sampler, idle
    CHECK( s->mwdt.feeds == afterBegin + 2 );

    // A new round: two parties checking in again and again feed nothing.
    for ( int i = 0; i < 10; ++i ) {
        watchdog::checkIn( Party::Console );
        watchdog::checkIn( Party::Idle0 );
    }
    CHECK( s->mwdt.feeds == afterBegin + 2 );
    watchdog::checkIn( Party::Sampler );
    CHECK( s->mwdt.feeds == afterBegin + 3 );
    CHECK( s->mwdt.locked );
}

TEST( "watchdog", "a watchdog that did not start takes no subscription" ) {
    Scope s;
    s->failIdleHook = true;
    CHECK( !watchdog::begin() );
    CHECK( !watchdog::subscribe( Party::Console ) );
    CHECK( !watchdog::subscribed( Party::Console ) );
    CHECK( !watchdog::subscribed( Party::Idle0 ) );
    watchdog::checkIn( Party::Idle0 );
    CHECK( s->mwdt.feeds == 0U );
}

TEST( "watchdog", "begin() forgets the subscriptions of a previous run" ) {
    Scope s;
    CHECK( watchdog::begin() );
    CHECK( watchdog::subscribe( Party::Sampler ) );
    CHECK( watchdog::begin() );
    CHECK( !watchdog::subscribed( Party::Sampler ) );
}

TEST( "watchdog", "both tasks subscribe and check in through a ring, a dump and a stream" ) {
    Bench b( []( sim::World& w ) { w.ringSamples = 8192; } );
    CHECK_EQ( b.ask( "DIAG:WATC?" ), "console=1 sampler=1 timeout_s=5 panic=1 stalls=0" );
    const std::size_t booted = b.world().mwdt.feeds;
    // An idle instrument: the sampler waits for a start in 1 s slices.
    b.world().run( 3'000'000 );
    b.tell( "SENS:PROF TRAC;:INIT", 3'000'000 );  // the ring fills and ends itself
    b.tell( "ABOR" );
    b.drain();
    b.world().send( "TRAC:DATA? RAW" );
    b.world().run( 3'000'000 );
    CHECK_CONTAINS( b.drain(), "METER_DUMP_END" );
    b.tell( "SENS:PROF SLEEP;:INIT", 3'000'000 );  // 67.5 ms a sample, streamed
    b.tell( "ABOR" );
    CHECK( b.clean() );
    // Fed about once a second (the sampler's idle slice), never near 5 s.
    CHECK( b.world().mwdt.feeds >= booted + 10 );
    CHECK( b.world().mwdt.longestGapUs < 1'100'000 );
    b.world().noteMwdt();
    CHECK( b.world().mwdt.resetAtUs < 0 );
}

TEST( "watchdog", "a dump that never blocks still gives Core 0's idle task its check-in" ) {
    // A full ring's counting pass formats a million rows without a single
    // blocking call, about 5.6 s on the meter: here a ring of 16,384 samples
    // (about a thousand RAW rows) on a CPU slowed so that each pass takes
    // longer than the timeout.
    Bench b( []( sim::World& w ) { w.ringSamples = 16384; } );
    b.tell( "SENS:PROF TRAC;:INIT", 5'000'000 );  // the ring fills and ends itself
    b.tell( "ABOR" );
    b.drain();
    const std::size_t before    = b.world().mwdt.feeds;
    const sim::Micros started   = b.world().now();
    b.world().mwdt.longestGapUs = 0;
    b.world().clockReadUs       = 10'000;
    b.world().send( "TRAC:DATA? RAW" );
    CHECK( b.world().runUntil(
        [&] { return b.world().uart.tx.find( "METER_DUMP_END" ) != std::string::npos; },
        120'000'000 ) );
    b.world().clockReadUs = 1;
    CHECK( b.world().now() - started > 10'000'000 );  // both passes outlast the timeout
    CHECK( b.world().mwdt.feeds > before + 10 );
    // About the sampler's 1 s idle slice, at 10 ms a clock read.
    CHECK( b.world().mwdt.longestGapUs < 1'500'000 );
    b.world().noteMwdt();
    CHECK( b.world().mwdt.resetAtUs < 0 );
}

TEST( "watchdog", "a running capture that takes no sample is reported once, and TIME is set" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    b.world().ina.stallNextUs = 3'000'000;  // the sampler's next read holds the bus
    b.world().run( 2'500'000 );
    const std::string errors = b.errors();
    CHECK_CONTAINS( errors, "107,\"Sampler not running;the sampler took no sample for" );
    CHECK( ( std::stoi( b.ask( "STAT:QUES:COND?" ) ) & 4 ) == 4 );
    b.world().run( 1'000'000 );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "DIAG:WATC?" ), "stalls" ), "1" );
    // The next capture starts clean: no stall, no TIME bit, no error.
    b.tell( "INIT", 100'000 );
    CHECK( ( std::stoi( b.ask( "STAT:QUES:COND?" ) ) & 4 ) == 0 );
    b.tell( "ABOR" );
    CHECK( b.clean() );
    // Three seconds without a check-in are inside the timeout: no reset.
    b.world().noteMwdt();
    CHECK( b.world().mwdt.resetAtUs < 0 );
}

TEST( "watchdog", "a sampler that stops checking in resets the chip within its timeout" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    const sim::Micros held       = b.world().now();
    b.world().ina.stallNextUs    = 8'000'000;  // longer than the timeout
    b.world().mwdt.resetExpected = true;
    b.world().run( 7'000'000 );
    b.world().noteMwdt();
    const sim::Micros resetAt = b.world().mwdt.resetAtUs;
    CHECK( resetAt >= 0 );
    CHECK( resetAt - held > 3'900'000 );
    CHECK( resetAt - held <= 6'100'000 );
}

TEST( "watchdog", "a Core 0 whose idle task never runs resets the chip" ) {
    Bench b;
    b.world().mwdt.resetExpected = true;
    const sim::Micros starved    = b.world().now();
    b.world().idleStarved        = true;
    b.world().run( 7'000'000 );
    b.world().noteMwdt();
    const sim::Micros resetAt = b.world().mwdt.resetAtUs;
    CHECK( resetAt >= 0 );
    CHECK( resetAt - starved > 3'900'000 );
    CHECK( resetAt - starved <= 6'100'000 );
}

TEST( "watchdog", "a timer that keeps no configuration supervises nothing, and is reported" ) {
    Bench b( []( sim::World& w ) { w.mwdt.stuck = true; } );
    CHECK_EQ( b.ask( "DIAG:WATC?" ), "console=0 sampler=0 timeout_s=5 panic=1 stalls=0" );
    b.tell( "SENS:PROF STATE;:INIT", 50'000 );
    b.tell( "ABOR" );
    CHECK( std::stoi( field( b.ask( "FETC:STAT?" ), "samples" ) ) > 0 );
    CHECK( !b.world().mwdt.enabled );
    b.world().run( 6'000'000 );
    b.world().noteMwdt();
    CHECK( b.world().mwdt.resetAtUs < 0 );
}

TEST( "watchdog", "an idle hook table that is full leaves the timer untouched, and is reported" ) {
    Bench b( []( sim::World& w ) { w.failIdleHook = true; } );
    CHECK_EQ( b.ask( "DIAG:WATC?" ), "console=0 sampler=0 timeout_s=5 panic=1 stalls=0" );
    CHECK( !b.world().mwdt.enabled );
    CHECK( b.world().mwdt.feeds == std::size_t{ 0 } );
    CHECK( b.world().timerGroups[0].resets == 0 );
    CHECK( timerGroupReferences( b.world() ) == 0 );
    b.tell( "SENS:PROF STATE;:INIT", 50'000 );
    b.tell( "ABOR" );
    CHECK( std::stoi( field( b.ask( "FETC:STAT?" ), "samples" ) ) > 0 );
}

TEST( "watchdog", "DIAGnostic:WATChdog:TEST is refused under a capture and resets the chip" ) {
    Bench b;
    b.tell( "SENS:PROF STATE;:INIT", 20'000 );
    b.tell( "DIAG:WATC:TEST" );
    CHECK_CONTAINS( b.errors(), "-221," );
    b.tell( "ABOR" );
    b.tell( "DIAG:WATC:TEST?" );
    CHECK_CONTAINS( b.errors(), "-113," );
    b.world().mwdt.resetExpected = true;
    const sim::Micros asked      = b.world().now();
    b.tell( "DIAG:WATC:TEST" );
    b.world().run( 7'000'000 );
    // On the device the timer resets the chip here: within the timeout of the
    // console's last check-in, which the sampler's and the idle task's go on
    // completing until the round waits for the console alone.
    b.world().noteMwdt();
    const sim::Micros resetAt = b.world().mwdt.resetAtUs;
    CHECK( resetAt >= 0 );
    CHECK( resetAt - asked > 3'900'000 );
    CHECK( resetAt - asked <= 6'100'000 );
}
