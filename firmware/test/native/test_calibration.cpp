// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_calibration.cpp
 * @brief calibration.h: the record in the flash log and the store's judgment
 *        of one, the setters and the `q=` token, each condition of the
 *        quality decision and of the record's judgment shown to flip it on
 *        its own.
 */
#include "calibration.h"
#include "flash_log.h"
#include "flash_port.h"
#include "scope.h"
#include "unit.h"

#include <array>
#include <cstddef>
#include <cstring>
#include <string>
#include <vector>

using namespace kleidos::meter;
using bench::Scope;
using ina228::ShuntRange;

namespace {

/// The calibration's log, the one these cases run on.
constexpr flash_log::Log kCal = flash_log::Log::Calibration;

constexpr ShuntRange kR0 = ShuntRange::Wide163mV;
constexpr ShuntRange kR1 = static_cast<ShuntRange>( 1 );

CalibrationStore calibrated() {
    CalibrationStore store;
    store.setShuntMicroOhm( 100'012 );
    store.setOffset( kR0, 482'954, 25'000 );
    return store;
}

using Bytes = std::vector<std::uint8_t>;

/// The bytes of the log's newest record's blob, as the log gives them out
/// with no judgment of its own (an empty vector when it holds none).
Bytes newestBlob() {
    REQUIRE( flash_log::mount( kCal, flash_port::kLogBaseAddress, nullptr )
             == flash_log::Status::Ok );
    std::array<std::uint8_t, flash_log::kMaxBlobBytes> out    = {};
    std::size_t                                        length = 0;
    if ( flash_log::load( kCal, out.data(), out.size(), length ) != flash_log::Status::Ok ) {
        return {};
    }
    return { out.begin(), out.begin() + static_cast<std::ptrdiff_t>( length ) };
}

/// Appends @p blob to the log as a record whose header and CRC hold, past
/// the store's judgment: what another image, or a failing flash that kept
/// its CRC, could have left.
void appendRaw( const Bytes& blob ) {
    REQUIRE( flash_log::mount( kCal, flash_port::kLogBaseAddress, nullptr )
             == flash_log::Status::Ok );
    REQUIRE( flash_log::save( kCal, blob.data(), blob.size() ) == flash_log::Status::Ok );
}

/// Stores @p value at @p at of @p blob, least significant byte first.
void put32( Bytes& blob, std::size_t at, std::uint32_t value ) {
    for ( std::size_t i = 0; i < 4; ++i ) {
        blob[at + i] = static_cast<std::uint8_t>( value >> ( 8 * i ) );
    }
}

/// Where the record layout (calibration.cpp, StoredBlob) keeps each word a
/// test changes.
constexpr std::size_t kMagicAt       = 0;
constexpr std::size_t kVersionAt     = 4;
constexpr std::size_t kShuntAt       = 8;
constexpr std::size_t kRangeShuntAt0 = 40;
constexpr std::size_t kRangeShuntAt1 = 44;

/// The blob a store with a measured shunt of 100.012 mOhm writes.
Bytes goodBlob() {
    CalibrationStore store = calibrated();
    REQUIRE( store.save() );
    return newestBlob();
}

}  // namespace

TEST( "calibration", "quality names" ) {
    CHECK_EQ( std::string( qualityName( Quality::Uncal ) ), "uncal" );
    CHECK_EQ( std::string( qualityName( Quality::Stale ) ), "stale" );
    CHECK_EQ( std::string( qualityName( Quality::Cal ) ), "cal" );
    CHECK_EQ( std::string( qualityName( Quality::Invalid ) ), "invalid" );
    CHECK_EQ( std::string( qualityName( static_cast<Quality>( 9 ) ) ), "uncal" );
}

TEST( "calibration", "quality: each condition decides on its own (MC/DC)" ) {
    const CalibrationStore store = calibrated();
    // MATHOF outranks everything.
    CHECK( store.quality( kR0, 25'000, true ) == Quality::Invalid );
    // Nulled and measured, within the window: cal.
    CHECK( store.quality( kR0, 25'000, false ) == Quality::Cal );
    // The other range was never nulled: uncal (first operand of the OR).
    CHECK( store.quality( kR1, 25'000, false ) == Quality::Uncal );
    // Nulled but the shunt never measured: uncal (second operand).
    CalibrationStore unmeasured;
    unmeasured.setOffset( kR0, 0, 25'000 );
    CHECK( unmeasured.quality( kR0, 25'000, false ) == Quality::Uncal );
    // Staleness: beyond +/-5 C either way, at the edge, and skipped on no evidence.
    CHECK( store.quality( kR0, 30'001, false ) == Quality::Stale );
    CHECK( store.quality( kR0, 19'999, false ) == Quality::Stale );
    CHECK( store.quality( kR0, 30'000, false ) == Quality::Cal );
    CHECK( store.quality( kR0, kDieMilliCelsiusUnknown, false ) == Quality::Cal );
    CalibrationStore unknownNull;
    unknownNull.setShuntMicroOhm( 100'000 );
    unknownNull.setOffset( kR0, 0, kDieMilliCelsiusUnknown );
    CHECK( unknownNull.quality( kR0, 90'000, false ) == Quality::Cal );
    // A shunt measuring exactly the nominal value is still a measurement.
    CalibrationStore nominal;
    nominal.setShuntMicroOhm( kNominalShuntMicroOhm );
    nominal.setOffset( kR0, 0, 25'000 );
    CHECK( nominal.quality( kR0, 25'000, false ) == Quality::Cal );
}

TEST( "calibration", "the record round-trips through the flash log" ) {
    Scope            scope;
    CalibrationStore store = calibrated();
    store.setOffset( kR1, -919'552, 23'625 );
    store.setVbusSide( VbusSide::Source );
    store.setTag( "R100_0.10ohm_1pct_1028" );
    CHECK( store.save() );
    CHECK( flash_log::head( kCal ).length == 80U );

    CalibrationStore loaded;
    CHECK( loaded.load() );
    CHECK( loaded.value().shuntMicroOhm == 100'012 );
    CHECK( loaded.shuntMeasured() );
    CHECK( loaded.nulled( kR0 ) && loaded.nulled( kR1 ) );
    CHECK( loaded.offsetPicovolts( kR1 ) == -919'552 );
    CHECK( loaded.offsetDieMilliCelsius( kR0 ) == 25'000 );
    CHECK( loaded.value().vbusSide == VbusSide::Source );
    CHECK_EQ( std::string( loaded.value().tag ), "R100_0.10ohm_1pct_1028" );

    // An unknown side byte reads back as Unknown.
    store.setVbusSide( VbusSide::Unknown );
    CHECK( store.save() );
    CHECK( loaded.load() );
    CHECK( loaded.value().vbusSide == VbusSide::Unknown );
    CHECK( !loaded.nulled( static_cast<ShuntRange>( 0 ) ) == false );
    // Each save is a new record: the log holds both, and the newest wins.
    CHECK( flash_log::head( kCal ).sequence == 2U );
}

TEST( "calibration", "a load that finds nothing usable keeps the honest defaults" ) {
    Scope            scope;
    CalibrationStore store;
    // Nothing stored: the erased partition of a fresh or fully erased meter.
    CHECK( !store.load() );
    CHECK( !store.shuntMeasured() );
    CHECK( store.shuntMicroOhm( kR0 ) == kNominalShuntMicroOhm );
    // What an older image's NVS left at 0x9000 is no record of the log.
    for ( std::uint32_t i = 0; i < 64; ++i ) {
        scope->flash.at( flash_port::kLogBaseAddress + i ) = static_cast<std::uint8_t>( 0xFE - i );
    }
    CHECK( !store.load() );
    CHECK( !store.shuntMeasured() );
    // The first save over it erases the other sector and writes there.
    CHECK( calibrated().save() );
    CHECK( flash_log::head( kCal ).sector == 1U );
    CHECK( store.load() );
    CHECK( store.shuntMicroOhm( kR0 ) == 100'012 );
    // A read that fails keeps what the store held.
    CalibrationStore reread;
    scope->flash.readsUntilFailure = 0;
    CHECK( !reread.load() );
    CHECK( !reread.shuntMeasured() );
    // The scan's four reads (sector 0's header, sector 1's record and the
    // erased header after it) hold, and the record's own read fails.
    scope->flash.readsUntilFailure = 4;
    CHECK( !reread.load() );
    CHECK( !reread.shuntMeasured() );
    CHECK( reread.load() );
    CHECK( reread.shuntMeasured() );
}

TEST( "calibration", "a save that the flash does not take fails, and the value stays live" ) {
    Scope            scope;
    CalibrationStore store   = calibrated();
    scope->flash.failProgram = true;
    CHECK( !store.save() );
    CHECK( store.value().shuntMicroOhm == 100'012 );
    scope->flash.failProgram = false;
    // The log reads the sectors again and the next save is taken.
    CHECK( store.save() );
    CalibrationStore loaded;
    CHECK( loaded.load() );
    CHECK( loaded.value().shuntMicroOhm == 100'012 );
}

TEST( "calibration", "a save of a shunt outside the window writes nothing" ) {
    Scope            scope;
    CalibrationStore store = calibrated();
    CHECK( store.save() );
    const std::size_t programs = scope->flash.programs;
    store.setShuntMicroOhm( kMinShuntMicroOhm - 1 );
    CHECK( !store.save() );
    store.setShuntMicroOhm( 100'000 );
    store.setShuntRangeMicroOhm( kR1, kMaxShuntMicroOhm + 1 );
    CHECK( !store.save() );
    CHECK( scope->flash.programs == programs );
    CalibrationStore loaded;
    CHECK( loaded.load() );
    CHECK( loaded.value().shuntMicroOhm == 100'012 );
}

TEST( "calibration", "the record's judgment: each condition refuses on its own (MC/DC)" ) {
    // A refused record is passed over as one whose CRC fails: the record
    // before it is the calibration. Each case starts from a good record of
    // 100.012 mOhm and appends one that departs from it in one field.
    struct Case {
        const char* what;
        Bytes ( *make )( Bytes );
    };
    const Case refused[] = {
        { "one byte short",
          []( Bytes b ) {
              b.pop_back();
              return b;
          } },
        { "another magic",
          []( Bytes b ) {
              put32( b, kMagicAt, 0x494E4133U );
              return b;
          } },
        { "the version 2 layout's version",
          []( Bytes b ) {
              b[kVersionAt] = 2;
              return b;
          } },
        { "a common shunt under the window",
          []( Bytes b ) {
              put32( b, kShuntAt, kMinShuntMicroOhm - 1 );
              return b;
          } },
        { "a common shunt over the window",
          []( Bytes b ) {
              put32( b, kShuntAt, kMaxShuntMicroOhm + 1 );
              return b;
          } },
        { "a common shunt of 0",
          []( Bytes b ) {
              put32( b, kShuntAt, 0 );
              return b;
          } },
        { "range 0's shunt outside the window",
          []( Bytes b ) {
              put32( b, kRangeShuntAt0, kMaxShuntMicroOhm + 1 );
              return b;
          } },
        { "range 1's shunt outside the window",
          []( Bytes b ) {
              put32( b, kRangeShuntAt1, kMinShuntMicroOhm - 1 );
              return b;
          } },
    };
    for ( const Case& c : refused ) {
        Scope       scope;
        const Bytes good = goodBlob();
        const Bytes bad  = c.make( good );
        appendRaw( bad );
        CalibrationStore store;
        CHECK_EQ( std::string( c.what ) + ( store.load() ? " loaded" : " refused" ),
                  std::string( c.what ) + " loaded" );
        CHECK_EQ( std::string( c.what ) + ": " + std::to_string( store.value().shuntMicroOhm ),
                  std::string( c.what ) + ": 100012" );
        CHECK( !store.storedReadable() );
        CHECK( flash_log::head( kCal ).refused == 1U );
    }
    // The window's bounds are measurements, and so is a range's 0.
    const std::uint32_t taken[] = { kMinShuntMicroOhm, kMaxShuntMicroOhm };
    for ( const std::uint32_t microOhm : taken ) {
        Scope scope;
        Bytes blob = goodBlob();
        put32( blob, kShuntAt, microOhm );
        put32( blob, kRangeShuntAt0, microOhm );
        put32( blob, kRangeShuntAt1, 0 );
        appendRaw( blob );
        CalibrationStore store;
        CHECK( store.load() );
        CHECK( store.shuntMicroOhm( kR0 ) == microOhm );
        CHECK( store.shuntMicroOhm( kR1 ) == microOhm );
        CHECK( store.storedReadable() );
    }
}

TEST( "calibration", "a refused record with nothing before it leaves the defaults" ) {
    Scope scope;
    Bytes blob = goodBlob();
    // Start again from an erased partition, with the refused record alone.
    scope->flash = sim::Flash{};
    put32( blob, kShuntAt, 1'000'000 );
    appendRaw( blob );
    CalibrationStore store;
    CHECK( !store.load() );
    CHECK( !store.shuntMeasured() );
    CHECK( store.shuntMicroOhm( kR0 ) == kNominalShuntMicroOhm );
    CHECK( !store.storedReadable() );
    // The next save does not append after it: it starts the other sector.
    CHECK( calibrated().save() );
    CHECK( flash_log::head( kCal ).sector == 1U );
    // Numbered above the refused record, so that it is the newest the
    // sectors hold, and the self-test passes again.
    CHECK( flash_log::head( kCal ).sequence == 2U );
    CHECK( store.load() );
    CHECK( store.shuntMicroOhm( kR0 ) == 100'012 );
    CHECK( store.storedReadable() );
}

TEST( "calibration", "the self-test's look at the store reads the flash again" ) {
    Scope            scope;
    CalibrationStore store;
    // Nothing stored is a pass.
    CHECK( store.storedReadable() );
    CHECK( calibrated().save() );
    CHECK( store.storedReadable() );
    // The scan's read fails.
    scope->flash.readsUntilFailure = 0;
    CHECK( !store.storedReadable() );
    // The scan holds and the record's own read fails: the scan reads the
    // record's header and blob, the erased header after it and the other
    // sector's header, and the load reads the record again.
    scope->flash.readsUntilFailure = 4;
    CHECK( !store.storedReadable() );
    CHECK( store.storedReadable() );
    // The record no longer holds: its CRC fails, and nothing is stored.
    scope->flash.at( flash_port::kLogBaseAddress + 20 ) ^= 0x01U;
    CHECK( store.storedReadable() );
    CHECK( !store.load() );
}

TEST( "calibration", "the tag is copied, bounded and clearable" ) {
    CalibrationStore store;
    store.setTag( "abc" );
    CHECK_EQ( std::string( store.value().tag ), "abc" );
    store.setTag( "0123456789012345678901234567890" );
    CHECK( std::strlen( store.value().tag ) == kCalTagCapacity );
    store.setTag( nullptr );
    CHECK_EQ( std::string( store.value().tag ), "" );
}

TEST( "calibration", "each range's own shunt, and the common value that clears them" ) {
    CalibrationStore store;
    CHECK( store.shuntMicroOhm( kR0 ) == kNominalShuntMicroOhm );
    store.setShuntRangeMicroOhm( kR1, 100'144 );
    CHECK( store.shuntMeasured() );
    CHECK( store.shuntMicroOhm( kR0 ) == kNominalShuntMicroOhm );
    CHECK( store.shuntMicroOhm( kR1 ) == 100'144 );
    store.setShuntRangeMicroOhm( kR1, 0 );
    CHECK( store.shuntMicroOhm( kR1 ) == kNominalShuntMicroOhm );
    store.setShuntRangeMicroOhm( kR0, 100'088 );
    store.setShuntRangeMicroOhm( kR1, 100'144 );
    // A new common value is a new calibration: the per-range values go.
    store.setShuntMicroOhm( 100'116 );
    CHECK( store.shuntMicroOhm( kR0 ) == 100'116 );
    CHECK( store.shuntMicroOhm( kR1 ) == 100'116 );
}

TEST( "calibration", "the calendar check" ) {
    CHECK( isCalendarDate( 2026, 11, 20 ) );
    CHECK( isCalendarDate( 2026, 12, 31 ) );
    CHECK( isCalendarDate( 2099, 1, 1 ) );
    CHECK( isCalendarDate( 2028, 2, 29 ) );
    CHECK( !isCalendarDate( 2027, 2, 29 ) );
    CHECK( isCalendarDate( 2027, 2, 28 ) );
    CHECK( !isCalendarDate( 2026, 4, 31 ) );
    CHECK( !isCalendarDate( 2025, 12, 31 ) );
    CHECK( !isCalendarDate( 2100, 1, 1 ) );
    CHECK( !isCalendarDate( 2026, 0, 1 ) );
    CHECK( !isCalendarDate( 2026, 13, 1 ) );
    CHECK( !isCalendarDate( 2026, 1, 0 ) );
    // February in a year that is not a leap year, and a leap year's other months.
    CHECK( isCalendarDate( 2028, 3, 31 ) );
    CHECK( !isCalendarDate( 2028, 4, 31 ) );
}

TEST( "calibration", "the record round-trips the per-range shunt and the date" ) {
    Scope            scope;
    CalibrationStore store = calibrated();
    store.setShuntRangeMicroOhm( kR1, 100'144 );
    store.setDate( 20261120 );
    CHECK( store.save() );
    CHECK( flash_log::head( kCal ).length == 80U );
    CalibrationStore loaded;
    CHECK( loaded.load() );
    CHECK( loaded.shuntMicroOhm( kR0 ) == 100'012 );
    CHECK( loaded.shuntMicroOhm( kR1 ) == 100'144 );
    CHECK( loaded.value().date == 20261120 );
}
