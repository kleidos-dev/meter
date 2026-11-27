// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_led.cpp
 * @brief status_led.cpp: the off frame on the RMT's registers (FW-10), the
 *        reset codes around it, the line held low after it, a channel that
 *        never counts out, and the report.
 */
#include "scope.h"
#include "status_led.h"
#include "unit.h"

#include <algorithm>
#include <array>
#include <cstdlib>
#include <span>
#include <string>

using namespace kleidos::meter;
using bench::Scope;

namespace {

std::string report() {
    std::array<char, 384> out = {};
    text::Writer          w( out );
    led::writeReport( w );
    return std::string( w.view() );
}

std::string blanked() {
    std::array<char, 64> out = {};
    text::Writer         w( out );
    led::writeBlankedList( w );
    return std::string( w.view() );
}

/// The value of `key=` in @p text, as a number (0 when absent).
long field( const std::string& text, const std::string& key ) {
    const std::size_t at = text.find( key + "=" );
    return at == std::string::npos ? 0
                                   : std::strtol( text.c_str() + at + key.size() + 1, nullptr, 10 );
}

/// When @p pad was routed to @p signal for the last time, or -1.
sim::Micros routedAt( const sim::Pad& pad, int signal ) {
    sim::Micros at = -1;
    for ( const auto& [when, driver] : pad.drivers ) {
        if ( driver == signal ) {
            at = when;
        }
    }
    return at;
}

/// Whether @p frame is the off frame the report states: kLedBlankPixels
/// pixels of 24 `0` bits, each high for t0h_ns and low for t0l_ns.
bool isOffFrame( const sim::RmtFrame& frame, long t0hNs, long t0lNs ) {
    const std::size_t bits = led::kLedBlankPixels * led::kLedBytesPerPixel * 8;
    if ( frame.halves.size() != 2 * bits ) {
        return false;
    }
    for ( std::size_t i = 0; i < frame.halves.size(); i += 2 ) {
        if ( frame.halves[i] != std::pair<int, std::int64_t>{ 1, t0hNs * 1000 }
             || frame.halves[i + 1] != std::pair<int, std::int64_t>{ 0, t0lNs * 1000 } ) {
            return false;
        }
    }
    return true;
}

}  // namespace

TEST( "led", "each data line gets 96 zero bits at the reported timing, between two reset codes" ) {
    Scope scope;
    led::blankAll();
    const std::string text = report();
    CHECK_EQ( text, "blanked=48,38 pixels=4 t0h_ns=300 t0l_ns=950 res_us=1000 held=low" );
    const std::vector<sim::RmtFrame>& frames = scope->rmt.frames;
    CHECK( frames.size() == led::kLedDataGpios.size() );
    for ( std::size_t i = 0; i < frames.size() && i < led::kLedDataGpios.size(); ++i ) {
        const sim::RmtFrame& frame = frames[i];
        const auto           pin   = static_cast<int>( led::kLedDataGpios[i] );
        // One line at a time, and the frame the report states, counted out by
        // the hardware in 96 passes: 120 us.
        CHECK( frame.pins == std::vector<int>{ pin } );
        CHECK( isOffFrame( frame, field( text, "t0h_ns" ), field( text, "t0l_ns" ) ) );
        CHECK( !frame.stopped );
        CHECK( frame.endUs - frame.startUs == 120 );
        // The channel idles low from before the routing to after it: the
        // leading and the trailing reset codes, each at least res_us long.
        const sim::Pad&   pad     = scope->pads[static_cast<std::size_t>( pin )];
        const sim::Micros toRmt   = routedAt( pad, sim::kSigRmtOut0 );
        const sim::Micros toGpio  = routedAt( pad, sim::kSigGpioOut );
        const long        resetUs = field( text, "res_us" );
        CHECK( toRmt >= 0 && frame.startUs - toRmt >= resetUs );
        CHECK( toGpio > frame.endUs && toGpio - frame.endUs >= resetUs );
        // Then the GPIO's own output, low: held=low.
        CHECK( pad.driving() && pad.outSignal == sim::kSigGpioOut && pad.latch == 0 );
        CHECK( pad.level() == 0 );
        CHECK( !pad.input && !pad.pullUp && !pad.pullDown && !pad.openDrain );
        CHECK( !pad.oneByPeripheral && !pad.inverted );
    }
    CHECK( blanked() == "48,38" );
}

TEST( "led", "the RMT is left gated, with no interrupt and nothing allocated" ) {
    Scope scope;
    led::blankAll();
    const sim::Rmt& rmt = scope->rmt;
    CHECK( !rmt.busClock && !rmt.groupClock && !rmt.memPowered );
    CHECK( rmt.interruptEnable == 0 );
    CHECK( rmt.stops == 0 );
    CHECK( !rmt.tx[0].loop && !rmt.tx[0].loopCountEnabled );
    // Nothing of ESP-IDF's that allocates, and no heap call of the harness's.
    CHECK( scope->idfLedger.empty() );
    CHECK( scope->heapAllocs == 0 && scope->heapFrees == 0 );
    // Scope's destructor holds the clock lock, the clocked writes and the
    // pads' levels.
}

TEST( "led", "a channel that never counts out is stopped, and both lines still end low" ) {
    Scope scope;
    scope->rmt.stall         = true;
    const sim::Micros before = scope->now();
    led::blankAll();
    const sim::Micros took = scope->now() - before;
    CHECK( scope->rmt.stops == 2 );
    for ( const sim::RmtFrame& frame : scope->rmt.frames ) {
        CHECK( frame.stopped && frame.halves.empty() );
        // Stopped at the poll's budget, 1 ms, not left to run.
        CHECK( frame.endUs - frame.startUs >= 1000 && frame.endUs - frame.startUs < 1100 );
    }
    // Two lines of a leading reset, the budget and a trailing reset each.
    CHECK( took < 2 * 3100 );
    for ( gpio_num_t pin : led::kLedDataGpios ) {
        const sim::Pad& pad = scope->pads[static_cast<std::size_t>( pin )];
        CHECK( pad.driving() && pad.outSignal == sim::kSigGpioOut && pad.level() == 0 );
    }
    CHECK( !scope->rmt.busClock );
}

TEST( "led", "a line the matrix gives back is driven from a low latch, whatever it held" ) {
    Scope scope;
    for ( gpio_num_t pin : led::kLedDataGpios ) {
        scope->pads[static_cast<std::size_t>( pin )].latch = 1;
    }
    led::blankAll();
    for ( gpio_num_t pin : led::kLedDataGpios ) {
        CHECK( scope->pads[static_cast<std::size_t>( pin )].level() == 0 );
    }
}

TEST( "led", "the reports, cut to a small buffer and to none" ) {
    Scope scope;
    led::blankAll();
    for ( std::size_t capacity = 0; capacity < 24; ++capacity ) {
        std::array<char, 24> buffer = {};
        text::Writer         list( std::span<char>( buffer.data(), capacity ) );
        led::writeBlankedList( list );
        CHECK( list.view().size() + 1U <= std::max<std::size_t>( capacity, 1U ) );
        text::Writer whole( std::span<char>( buffer.data(), capacity ) );
        led::writeReport( whole );
        CHECK( whole.view().size() + 1U <= std::max<std::size_t>( capacity, 1U ) );
        CHECK( whole.truncated() == ( capacity < report().size() + 1U ) );
    }
}
