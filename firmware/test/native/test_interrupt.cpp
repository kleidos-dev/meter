// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_interrupt.cpp
 * @brief interrupt.cpp on the simulated CPU's tables: which lines are free,
 *        the route made on the calling core, a line the allocator holds
 *        skipped, and the refusals that write nothing.
 */
#include "interrupt.h"
#include "scope.h"
#include "unit.h"

#include <cstddef>
#include <cstdint>

using namespace kleidos::meter;
using bench::Scope;

namespace {

/// What the routed line runs in these cases.
void handlerOne( void* ) {}

/// The facts of a line that is free: each case below changes one.
interrupt::LineFacts freeLine() {
    interrupt::LineFacts facts;
    facts.priority  = interrupt::kRoutedPriority;
    facts.levelType = true;
    return facts;
}

/// Runs @p body as core @p core.
template <typename Body>
void onCore( int core, Body body ) {
    const int caller = sim::currentCore();
    sim::setCurrentCore( core );
    body();
    sim::setCurrentCore( caller );
}

}  // namespace

TEST( "interrupt",
      "MC/DC: a line is free only at priority 1, level, unreserved, plain, unclaimed" ) {
    CHECK( interrupt::lineIsFree( freeLine() ) );
    interrupt::LineFacts facts = freeLine();
    facts.priority             = 2;
    CHECK( !interrupt::lineIsFree( facts ) );
    facts           = freeLine();
    facts.levelType = false;
    CHECK( !interrupt::lineIsFree( facts ) );
    facts          = freeLine();
    facts.reserved = true;
    CHECK( !interrupt::lineIsFree( facts ) );
    facts         = freeLine();
    facts.special = true;
    CHECK( !interrupt::lineIsFree( facts ) );
    facts            = freeLine();
    facts.hasHandler = true;
    CHECK( !interrupt::lineIsFree( facts ) );
}

TEST( "interrupt", "a source is routed to the calling core's first free line and enabled" ) {
    Scope            scope;
    interrupt::Route route;
    int              marker = 0;
    onCore( 1, [&] {
        CHECK( interrupt::routeHere( sim::kGpioSource, &handlerOne, &marker, route ) == ESP_OK );
    } );
    // Core 1's line 0 is reserved (esp_cpu_intr.c, no Wi-Fi task on Core 0):
    // line 1 is its first free one.
    CHECK( route.core == 1 && route.line == 1 );
    const sim::Cpu& core1 = scope->cpus[1];
    CHECK( core1.routes.at( sim::kGpioSource ) == 1 );
    CHECK( core1.lines[1].handler == &handlerOne && core1.lines[1].arg == &marker );
    CHECK( core1.enabled == ( 1U << 1U ) );
    // Nothing on the other core.
    CHECK( scope->cpus[0].routes.empty() && scope->cpus[0].enabled == 0 );
    CHECK( scope->interruptMaskDepth == 0 );
}

TEST( "interrupt", "a line the allocator gave a driver is skipped, and it skips the route's" ) {
    Scope scope;
    // On Core 0 line 0 is free: a driver's interrupt takes it first.
    const int driver = scope->allocatorTakesLine( 0 );
    REQUIRE( driver == 0 );
    interrupt::Route route;
    REQUIRE( interrupt::routeHere( sim::kGpioSource, &handlerOne, nullptr, route ) == ESP_OK );
    CHECK( route.core == 0 && route.line == 1 );
    // And a driver installed after the route does not take the routed line.
    const int later = scope->allocatorTakesLine( 0 );
    CHECK( later == 2 );
}

TEST( "interrupt", "no free line, or no handler: refused, and nothing written" ) {
    Scope scope;
    while ( scope->allocatorTakesLine( 0 ) >= 0 ) {}
    const sim::Cpu   before = scope->cpus[0];
    interrupt::Route route;
    CHECK( interrupt::routeHere( sim::kGpioSource, &handlerOne, nullptr, route )
           == ESP_ERR_NOT_FOUND );
    CHECK( route.core == -1 && route.line == -1 );
    CHECK( scope->cpus[0].routes.empty() && scope->cpus[0].enabled == before.enabled );
    CHECK( scope->interruptMaskDepth == 0 );

    Scope fresh;
    CHECK( interrupt::routeHere( sim::kGpioSource, nullptr, nullptr, route )
           == ESP_ERR_INVALID_ARG );
    CHECK( route.core == -1 && route.line == -1 );
    CHECK( fresh->cpus[0].routes.empty() && fresh->cpus[0].enabled == 0 );
    for ( const sim::CpuLine& line : fresh->cpus[0].lines ) {
        CHECK( line.handler == nullptr );
    }
}
