// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_relay.cpp
 * @brief relay.h / relay.cpp: the four circuits, the plans and their
 *        interlocks, the null-path verdicts and the bank on its pads. Each
 *        condition of each decision is shown to change the outcome alone.
 */
#include "pinmap.h"
#include "relay.h"
#include "scope.h"
#include "unit.h"

#include <array>
#include <span>
#include <string>
#include <utility>

using namespace kleidos::meter::relay;
using bench::Scope;
using kleidos::meter::pinmap::kDefaultMap;

namespace {

constexpr Mask kK1   = bitOf( kLead );
constexpr Mask kK2   = bitOf( kBypass );
constexpr Mask kNull = static_cast<Mask>( kK1 | kK2 );

std::string state( Mask mask, Evidence evidence = Evidence::None, std::size_t capacity = 128 ) {
    std::array<char, 128>        out = {};
    kleidos::meter::text::Writer w( std::span<char>( out.data(), capacity ) );
    writeState( w, mask, evidence );
    return std::string( w.view() );
}

std::string channels( Mask mask, std::size_t capacity = 32 ) {
    std::array<char, 32>         out = {};
    kleidos::meter::text::Writer w( std::span<char>( out.data(), capacity ) );
    writeChannels( w, mask );
    return std::string( w.view() );
}

}  // namespace

TEST( "relay", "the four circuits and which of them measure" ) {
    CHECK( pathOf( 0 ) == Path::Shunt );
    CHECK( pathOf( kK1 ) == Path::None );
    CHECK( pathOf( kK2 ) == Path::Both );
    CHECK( pathOf( kNull ) == Path::Bypass );
    CHECK( shuntCarriesAll( Path::Shunt ) );
    CHECK( shuntCarriesAll( Path::None ) );
    CHECK( !shuntCarriesAll( Path::Both ) );
    CHECK( !shuntCarriesAll( Path::Bypass ) );
    CHECK_EQ( std::string( pathName( Path::Shunt ) ), "shunt" );
    CHECK_EQ( std::string( pathName( Path::Bypass ) ), "bypass" );
    CHECK_EQ( std::string( pathName( Path::Both ) ), "both" );
    CHECK_EQ( std::string( pathName( Path::None ) ), "none" );
    CHECK_EQ( std::string( pathName( static_cast<Path>( 9 ) ) ), "none" );
    CHECK_EQ( std::string( evidenceName( Evidence::None ) ), "-" );
    CHECK_EQ( std::string( evidenceName( Evidence::Seen ) ), "ok" );
    CHECK_EQ( std::string( evidenceName( Evidence::Unverified ) ), "unverified" );
    CHECK_EQ( std::string( evidenceName( Evidence::Skipped ) ), "skipped" );
    CHECK_EQ( std::string( evidenceName( static_cast<Evidence>( 9 ) ) ), "-" );
}

TEST( "relay", "drive levels: the HW-316 is active low, the Click active high" ) {
    CHECK( energizeLevel( Drive::ActiveLow ) == 0 );
    CHECK( releaseLevel( Drive::ActiveLow ) == 1 );
    CHECK( energizeLevel( Drive::ActiveHigh ) == 1 );
    CHECK( releaseLevel( Drive::ActiveHigh ) == 0 );
    CHECK_EQ( std::string( kBypassPart ), "relay2click" );
    CHECK( kDefaultMap.relay1Drive == Drive::ActiveLow );
    CHECK( kDefaultMap.relay2Drive == Drive::ActiveHigh );
    CHECK( maxOperateSettleMs() == 20 && maxReleaseSettleMs() == 50 );
}

TEST( "relay", "plans: one coil, and the null path make-before-break both ways" ) {
    CHECK( planSet( 0, kLead, false ).count == 0 );
    const Plan open = planSet( 0, kLead, true );
    CHECK( open.count == 1 && open.steps[0].settleMs == kOperateSettleMs );
    const Plan close = planSet( kK1, kLead, false );
    CHECK( close.count == 1 && close.steps[0].settleMs == kReleaseSettleMs );

    const Plan on = planNullPath( 0, true );
    CHECK( on.count == 2 && on.steps[0].channel == kBypass && on.steps[1].channel == kLead );
    CHECK( planNullPath( kK2, true ).count == 1 );  // K2 already closed
    CHECK( planNullPath( kK1, true ).count == 1 );  // K1 already open
    CHECK( planNullPath( kNull, true ).count == 0 );
    const Plan off = planNullPath( kNull, false );
    CHECK( off.count == 2 && off.steps[0].channel == kLead && off.steps[1].channel == kBypass );
    CHECK( planNullPath( kK1, false ).count == 1 );
    CHECK( planNullPath( kK2, false ).count == 1 );
    CHECK( finalState( 0, on ) == kNull );
    CHECK( finalState( kNull, off ) == 0 );

    // A plan never holds more steps than it has room for.
    Plan full;
    full.add( 0, true );
    full.add( 1, true );
    full.add( 2, true );
    CHECK( full.count == Plan::kMaxSteps );
}

TEST( "relay", "interlocks: into none, and off the DUT's current (MC/DC)" ) {
    // unpowersDut: a step into None from a state that was not None.
    CHECK( unpowersDut( 0, planSet( 0, kLead, true ) ) );
    CHECK( !unpowersDut( kK1, planSet( kK1, 2, true ) ) );      // already None
    CHECK( !unpowersDut( kK2, planSet( kK2, kLead, true ) ) );  // lands on Bypass
    CHECK( !unpowersDut( 0, Plan{} ) );
    // bypassesShunt: at the start, or after a step.
    CHECK( bypassesShunt( kK2, Plan{} ) );                     // already Both
    CHECK( bypassesShunt( 0, planSet( 0, kBypass, true ) ) );  // a step into Both
    CHECK( !bypassesShunt( 0, planSet( 0, kLead, true ) ) );   // into None
    CHECK( !bypassesShunt( 0, Plan{} ) );
}

TEST( "relay", "null-path verdicts: each condition alone (MC/DC)" ) {
    constexpr std::int64_t uv = 1'000'000;
    CHECK( magnitude( -5 ) == 5 && magnitude( 5 ) == 5 );
    CHECK( seesDutCurrent( 200 * uv ) );
    CHECK( seesDutCurrent( -200 * uv ) );
    CHECK( !seesDutCurrent( 199 * uv ) );
    CHECK( bypassTookCurrent( 400 * uv, 300 * uv ) );  // exactly three quarters
    CHECK( !bypassTookCurrent( 400 * uv, 301 * uv ) );
    CHECK( bypassTookCurrent( 400 * uv, 25 * uv ) );   // exactly a sixteenth
    CHECK( bypassTookCurrent( 400 * uv, -25 * uv ) );  // of either sign
    CHECK( !bypassTookCurrent( 400 * uv, 24 * uv ) );  // a lead that opened
    CHECK( shareLeftShunt( 400 * uv, 300 * uv ) && shareLeftShunt( 400 * uv, 0 ) );
    CHECK( !shareLeftShunt( 400 * uv, 301 * uv ) );
    // Beyond the residual limit fails whatever else holds.
    CHECK( !leadLeftShuntDead( 51 * uv, 0, 0, false ) );
    // Within it, unjudged: passes.
    CHECK( leadLeftShuntDead( 50 * uv, 30 * uv, 30 * uv, false ) );
    // Within it, judged: the half test decides.
    CHECK( leadLeftShuntDead( 10 * uv, 40 * uv, 20 * uv, true ) );
    CHECK( !leadLeftShuntDead( 10 * uv, 40 * uv, 21 * uv, true ) );
}

TEST( "relay", "state and channel tokens, and what a short buffer gets" ) {
    // The spares are not wired on the rig (0.13.0): their tokens say so,
    // whatever a mask claims of them.
    CHECK_EQ(
        state( 0 ),
        "k1=lead:closed k2=bypass:open k3=spare:unwired k4=spare:unwired path=shunt check=-" );
    CHECK_EQ( state( kNull, Evidence::Seen ),
              "k1=lead:open k2=bypass:closed k3=spare:unwired k4=spare:unwired path=bypass "
              "check=ok" );
    CHECK_EQ( state( bitOf( 2 ) | bitOf( 3 ), Evidence::Skipped ),
              "k1=lead:closed k2=bypass:open k3=spare:unwired k4=spare:unwired path=shunt "
              "check=skipped" );
    // Truncated, never overrun: the result always ends inside the buffer.
    CHECK( state( 0, Evidence::None, 20 ).size() == 19 );
    CHECK( state( 0, Evidence::None, 1 ).empty() );
    CHECK( state( 0, Evidence::None, 0 ).empty() );
    CHECK_EQ( channels( 0 ), "-" );
    CHECK_EQ( channels( kK1 | bitOf( 2 ) ), "k1,k3" );
    CHECK_EQ( channels( 0x0F ), "k1,k2,k3,k4" );
    CHECK( channels( 0x0F, 5 ).size() == 4 );
    CHECK( channels( 0, 1 ).empty() );
    CHECK( channels( kK1, 0 ).empty() );
    // Appended: a writer that already holds text gets the tokens after a space.
    std::array<char, 160>        out = {};
    kleidos::meter::text::Writer w( out );
    w.text( "ok" );
    writeState( w, 0, Evidence::None );
    CHECK_EQ( std::string( w.view() ),
              "ok k1=lead:closed k2=bypass:open k3=spare:unwired k4=spare:unwired path=shunt "
              "check=-" );
}

TEST( "relay", "the bank releases every coil at boot, latch before pad" ) {
    Scope scope;
    Bank  bank;
    CHECK( !bank.ready() );
    bank.drive( kLead, true );  // refused before begin()
    CHECK( bank.energized() == 0 );
    CHECK( bank.begin( kDefaultMap ) );
    CHECK( bank.ready() );
    // K1 on GPIO7, released high; K2 on GPIO15, released low.
    for ( const auto& [gpio, released] : { std::pair{ 7U, 1 }, std::pair{ 15U, 0 } } ) {
        const sim::Pad& pad = scope->pads[gpio];
        CHECK( pad.driving() && pad.input );
        CHECK( pad.firstDriven == released && pad.enabledAt == 1 );
        CHECK( pad.level() == released );
    }
    for ( const std::size_t gpio : { 16U, 17U } ) {
        // The spares have no GPIO of the map's: none is driven, both parked.
        const sim::Pad& pad = scope->pads[gpio];
        CHECK( !pad.driving() && pad.input && pad.pullDown && !pad.pullUp );
    }
    CHECK( scope->circuit.disconnects == 0 );
    CHECK( !scope->leadOpen() && !scope->bypassClosed() );
    CHECK( bank.path() == Path::Shunt && !bank.shuntBypassed() );
    CHECK( bank.padMismatches() == 0 );
}

TEST( "relay", "the bank parks every pin nothing drives, and none it drives or must not touch" ) {
    Scope scope;
    Bank  bank;
    REQUIRE( bank.begin( kDefaultMap ) );
    for ( const gpio_num_t pin : kParkedPins ) {
        const sim::Pad& pad = scope->pads[static_cast<std::size_t>( pin )];
        CHECK( pad.configured && pad.input && !pad.output );
        CHECK( pad.pullDown && !pad.pullUp && pad.level() == 0 );
    }
    // The unwired spare GPIO16 reads a defined low, not a float.
    CHECK( scope->pads[16].level() == 0 );
    // The wired relays are driven, not parked; the reserved pins are untouched.
    CHECK( scope->pads[7].driving() && !scope->pads[7].pullDown );
    CHECK( scope->pads[15].driving() && !scope->pads[15].pullDown );
    for ( const int pin : { 0, 3, 19, 20, 43, 44, 45, 46 } ) {
        CHECK( !scope->pads[static_cast<std::size_t>( pin )].configured );
    }
}

TEST( "relay", "the parked pins' rules refuse each planted table, as the build's check would" ) {
    // The static_assert holds only the true result for today's tables; each
    // refusal is shown here, on tables of the same sizes (the same
    // instantiation the check compiles), and run, so the report sees them.
    CHECK( detail::parkedPinsSound( kParkedPins, kReservedPins ) );
    auto reserved = kParkedPins;  // a strapping pin parked
    reserved[0]   = static_cast<gpio_num_t>( 0 );
    CHECK( !detail::parkedPinsSound( reserved, kReservedPins ) );
    auto twice = kParkedPins;  // GPIO1 parked twice
    twice[1]   = twice[0];
    CHECK( !detail::parkedPinsSound( twice, kReservedPins ) );
    auto lastTwice                   = kParkedPins;  // the last two entries the same
    lastTwice[lastTwice.size() - 1U] = lastTwice[lastTwice.size() - 2U];
    CHECK( !detail::parkedPinsSound( lastTwice, kReservedPins ) );
    // A pin in use, or one left floating, is the static_assert of pinmap.h:
    // the table is parkedSet() of the default map.
    CHECK( kleidos::meter::pinmap::parkedSet( kDefaultMap )
           == kleidos::meter::pinmap::setOf( kParkedPins ) );
    // A reserved pin is found wherever it sits in the reserved table.
    CHECK( detail::holds( kReservedPins, 46 ) && detail::holds( kReservedPins, 0 ) );
    CHECK( !detail::holds( kReservedPins, 47 ) );
}

TEST( "relay", "a pin that will not park leaves the others parked and the bank ready" ) {
    Scope scope;
    scope->faults.failGpioConfig    = true;
    scope->faults.failGpioConfigPin = 21;
    Bank bank;
    CHECK( bank.begin( kDefaultMap ) );
    CHECK( bank.ready() );
    CHECK( !scope->pads[21].configured );
    CHECK( scope->pads[18].pullDown && scope->pads[39].pullDown );
}

TEST( "relay", "the bank drives each coil and reads its pads back" ) {
    Scope scope;
    Bank  bank;
    REQUIRE( bank.begin( kDefaultMap ) );
    bank.drive( kBypass, true );
    CHECK( scope->bypassClosed() );
    CHECK( bank.path() == Path::Both && bank.shuntBypassed() );
    bank.drive( kLead, true );
    CHECK( scope->leadOpen() && bank.path() == Path::Bypass );
    CHECK( scope->circuit.disconnects == 0 );
    bank.drive( kLead, false );
    bank.drive( kBypass, false );
    CHECK( bank.energized() == 0 );
    bank.drive( kRelayCount, true );  // out of range: ignored
    CHECK( bank.energized() == 0 );
    bank.drive( 2, true );  // unwired: ignored, and its pad untouched
    CHECK( bank.energized() == 0 && !scope->pads[16].driving() );
    // A pad held against its drive is named; an unwired one is never read.
    scope->pads[7].forced = 0;
    CHECK( bank.padMismatches() == bitOf( kLead ) );
    scope->pads[7].forced  = -1;
    scope->pads[17].forced = 0;
    CHECK( bank.padMismatches() == 0 );
    scope->pads[17].forced = -1;
}

TEST( "relay", "the bank records each drive, and reads back only a whole record after a reset" ) {
    Scope scope;
    scope->resetReason = 4;  // a panic: RTC memory kept
    {
        Bank bank;
        REQUIRE( bank.begin( kDefaultMap ) );
        bank.drive( kBypass, true );
        bank.drive( kLead, true );
        const ResetRecord& kept = detail::resetRecord();
        CHECK( recordWhole( kept ) && kept.energized == kNull );
    }
    // The coils the reset found, once: begin() records the released state.
    Bank after;
    REQUIRE( after.begin( kDefaultMap ) );
    CHECK( after.releasedByReset() == kNull );
    CHECK( detail::resetRecord().energized == 0 && recordWhole( detail::resetRecord() ) );
    Bank again;
    REQUIRE( again.begin( kDefaultMap ) );
    CHECK( again.releasedByReset() == 0 );
    // Noise: a wrong magic number, or a state its inverted copy denies.
    detail::resetRecord() = ResetRecord{ 0, kK1, static_cast<Mask>( ~kK1 ) };
    Bank noise;
    REQUIRE( noise.begin( kDefaultMap ) );
    CHECK( noise.releasedByReset() == 0 );
    detail::resetRecord() = ResetRecord{ kResetRecordMagic, kK1, kK1 };
    Bank torn;
    REQUIRE( torn.begin( kDefaultMap ) );
    CHECK( torn.releasedByReset() == 0 );
    // A power-on ignores even a whole record.
    detail::resetRecord() = ResetRecord{ kResetRecordMagic, kK1, static_cast<Mask>( ~kK1 ) };
    scope->resetReason    = 1;
    Bank cold;
    REQUIRE( cold.begin( kDefaultMap ) );
    CHECK( cold.releasedByReset() == 0 );
}

TEST( "relay", "a pad that will not configure leaves the bank refusing every order" ) {
    Scope scope;
    scope->faults.failGpioConfig    = true;
    scope->faults.failGpioConfigPin = 15;
    Bank bank;
    CHECK( !bank.begin( kDefaultMap ) );
    CHECK( !bank.ready() );
    bank.drive( kLead, true );
    CHECK( bank.energized() == 0 );
    // The failed pin stays the floating input reset left: off.
    CHECK( !scope->pads[15].configured );
    CHECK( !scope->bypassClosed() );
}
