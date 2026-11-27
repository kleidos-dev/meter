// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_relay2.cpp
 * @brief The bypass on a Relay 2 Click channel (RL2 on the bench since
 *        2026-11-20), active high on GPIO15, against a bench wired for it:
 *        the one image since 0.8.0 (the `meter_relay2` image before).
 */
#include "bench.h"
#include "pinmap.h"
#include "relay.h"
#include "scope.h"
#include "unit.h"

#include <string>

using namespace kleidos::meter::relay;
using bench::Bench;
using bench::field;
using bench::Scope;

namespace {

void clickBench( sim::World& world ) {
    world.circuit.bypassPart = sim::BypassPart::Relay2Click;
}

}  // namespace

TEST( "relay2", "K2 is a Click channel: active high, SSR settle times" ) {
    CHECK_EQ( std::string( kBypassPart ), "relay2click" );
    CHECK( kleidos::meter::pinmap::kDefaultMap.relay2Drive == Drive::ActiveHigh );
    CHECK( kChannels[kBypass].operateSettleMs == kSsrOperateSettleMs );
    CHECK( kChannels[kBypass].releaseSettleMs == kSsrReleaseSettleMs );
    CHECK( kleidos::meter::pinmap::kDefaultMap.relay1Drive == Drive::ActiveLow );
    CHECK( maxOperateSettleMs() == 20 && maxReleaseSettleMs() == 50 );
    const Plan on = planNullPath( 0, true );
    CHECK( on.steps[0].settleMs == 10 && on.steps[1].settleMs == 20 );
}

TEST( "relay2", "boot drives GPIO15 low: the Click's LED is dark, the bypass open" ) {
    Scope scope;
    clickBench( *scope.world );
    Bank bank;
    REQUIRE( bank.begin( kleidos::meter::pinmap::kDefaultMap ) );
    CHECK( scope->pads[15].driving() && scope->pads[15].level() == 0 );
    CHECK( scope->pads[7].level() == 1 );
    CHECK( !scope->bypassClosed() && !scope->leadOpen() );
    bank.drive( kBypass, true );
    CHECK( scope->pads[15].level() == 1 && scope->bypassClosed() );
    CHECK( bank.padMismatches() == 0 );
}

TEST( "relay2", "the null path forms, is seen, and never unpowers the DUT" ) {
    Bench b( clickBench );
    b.world().circuit.dutNa = 5'000'000;  // 5 mA: 500 uV, above the check's floor
    const std::string on    = b.ask( "ROUT:NULL ON;:ROUT:STAT?" );
    CHECK_EQ( field( on, "path" ), "bypass" );
    CHECK_EQ( field( on, "check" ), "ok" );
    CHECK_EQ( field( on, "bypass_part" ), "relay2click" );
    CHECK( b.world().bypassClosed() && b.world().leadOpen() );
    b.tell( "ROUT:NULL OFF", 200'000 );
    CHECK( !b.world().bypassClosed() && !b.world().leadOpen() );
    CHECK( b.world().circuit.disconnects == 0 );
    CHECK( b.clean() );
}

TEST( "relay2", "a Click that does not close is caught by the bypass check" ) {
    Bench b( []( sim::World& world ) {
        clickBench( world );
        world.circuit.bypassStuckOpen = true;
        world.circuit.dutNa           = 5'000'000;
    } );
    b.tell( "ROUT:NULL ON", 300'000 );
    const std::string errors = b.errors();
    CHECK_CONTAINS( errors, "103," );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
    CHECK( b.world().circuit.disconnects == 0 );
}

TEST( "relay2", "SYSTem:INFormation? names the bypass part and the image" ) {
    Bench             b( clickBench );
    const std::string info = b.ask( "SYST:INF?" );
    CHECK_EQ( field( info, "bypass_part" ), "relay2click" );
    CHECK_EQ( field( info, "image" ), "meter" );
}
