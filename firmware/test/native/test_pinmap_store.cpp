// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_pinmap_store.cpp
 * @brief pinmap_store.cpp on the harness's model of the flash: the blob byte
 *        for byte, a map saved and loaded back field by field, every record
 *        the store refuses read as no record and the defaults, a refused
 *        record newer than the map stored, the flash's failures, a power cut
 *        after every byte of a pin save, and the calibration's log left
 *        alone by it (FW-03).
 */
#include "calibration.h"
#include "crc32.h"
#include "flash_log.h"
#include "flash_port.h"
#include "pin_table.h"
#include "pinmap.h"
#include "pinmap_store.h"
#include "scope.h"
#include "unit.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <initializer_list>
#include <span>
#include <vector>

using namespace kleidos::meter;
using bench::Scope;
using pin_table::Role;
using pinmap::PinMap;
using pinmap::Pull;
using relay::Drive;

namespace {

using Bytes = std::vector<std::uint8_t>;

/// The pin map's log.
constexpr flash_log::Log kPins = flash_log::Log::Pins;

/// Where the layout keeps each field (pinmap_store.cpp, StoredMap), as an
/// independent statement of it.
constexpr std::size_t kMagicAt   = 0;
constexpr std::size_t kVersionAt = 4;
constexpr std::size_t kDigestAt  = 8;
constexpr std::size_t kPinsAt    = 12;
constexpr std::size_t kClockAt   = 32;
constexpr std::size_t kPullSdaAt = 36;
constexpr std::size_t kAddressAt = 41;
constexpr std::size_t kTailAt    = 42;

/// A map unlike the default in every field, which the table takes.
PinMap otherMap() {
    PinMap map      = pinmap::kDefaultMap;
    map.pins        = { 8, 9, 10, 11, 12 };
    map.pullSda     = Pull::Off;
    map.pullScl     = Pull::Up;
    map.pullAlert   = Pull::Off;
    map.relay1Drive = Drive::ActiveHigh;
    map.relay2Drive = Drive::ActiveLow;
    map.address     = 0x45;
    map.sclHz       = 100'000;
    return map;
}

/// Whether two maps are the same in every field.
bool sameMap( const PinMap& a, const PinMap& b ) {
    return a.pins == b.pins && a.pullSda == b.pullSda && a.pullScl == b.pullScl
           && a.pullAlert == b.pullAlert && a.relay1Drive == b.relay1Drive
           && a.relay2Drive == b.relay2Drive && a.address == b.address && a.sclHz == b.sclHz;
}

/// The blob of @p map, as a vector to patch.
Bytes blobOf( const PinMap& map ) {
    const pinmap_store::Blob blob = pinmap_store::encode( map );
    return { blob.begin(), blob.end() };
}

/// Stores @p value at @p at of @p blob, least significant byte first.
void put32( Bytes& blob, std::size_t at, std::uint32_t value ) {
    for ( std::size_t i = 0; i < 4U; ++i ) {
        blob[at + i] = static_cast<std::uint8_t>( value >> ( 8U * i ) );
    }
}

/// Appends @p blob to the pin map log as a record whose header and CRC hold,
/// past the store's judgment: what another image could have left.
void appendRaw( const Bytes& blob ) {
    REQUIRE( flash_log::mount( kPins, flash_port::kPinLogBaseAddress, nullptr )
             == flash_log::Status::Ok );
    REQUIRE( flash_log::save( kPins, blob.data(), blob.size() ) == flash_log::Status::Ok );
}

/// Writes a valid record of @p blob with @p sequence straight into the
/// model at the start of the pin map log's first sector.
void plantRecord( sim::Flash& flash, std::uint32_t sequence, const Bytes& blob ) {
    std::array<std::uint8_t, 12> head = {};
    const auto                   size = static_cast<std::uint32_t>( blob.size() );
    for ( std::uint32_t i = 0; i < 4U; ++i ) {
        head[i]      = static_cast<std::uint8_t>( flash_log::kMagic >> ( 8U * i ) );
        head[4U + i] = static_cast<std::uint8_t>( sequence >> ( 8U * i ) );
        head[8U + i] = static_cast<std::uint8_t>( size >> ( 8U * i ) );
    }
    const std::uint32_t crc =
        crc32::update( crc32::update( crc32::kInitial, head.data(), head.size() ), blob.data(),
                       blob.size() )
        ^ crc32::kInitial;
    const std::uint32_t base = flash_port::kPinLogBaseAddress;
    for ( std::uint32_t i = 0; i < 12U; ++i ) {
        flash.at( base + i ) = head[i];
    }
    for ( std::uint32_t i = 0; i < 4U; ++i ) {
        flash.at( base + 12U + i ) = static_cast<std::uint8_t>( crc >> ( 8U * i ) );
    }
    for ( std::uint32_t i = 0; i < size; ++i ) {
        flash.at( base + 16U + i ) = blob[i];
    }
}

/// Loads, and checks that the store found @p found and runs the defaults.
void loadsDefaults( pinmap_store::Found found ) {
    const pinmap_store::Loaded loaded = pinmap_store::load();
    CHECK( loaded.found == found );
    CHECK( sameMap( loaded.map, pinmap::kDefaultMap ) );
}

}  // namespace

// --- The blob ------------------------------------------------------------------------

TEST( "pinmap_store", "the default map's blob, byte for byte" ) {
    const std::uint32_t d    = pin_table::kPinTableDigest;
    Bytes               want = { 'P', 'I', 'N', 'M' };  // the magic
    const auto          add  = [&want]( std::initializer_list<std::uint8_t> bytes ) {
        want.insert( want.end(), bytes.begin(), bytes.end() );
    };
    add( { 0x01, 0x00, 0x00, 0x00 } );  // version 1
    add( { static_cast<std::uint8_t>( d ), static_cast<std::uint8_t>( d >> 8U ),
           static_cast<std::uint8_t>( d >> 16U ), static_cast<std::uint8_t>( d >> 24U ) } );
    add( { 4, 0, 0, 0, 5, 0, 0, 0, 6, 0, 0, 0 } );  // SDA, SCL, ALERT
    add( { 7, 0, 0, 0, 15, 0, 0, 0 } );             // Relay 1, Relay 2
    add( { 0x80, 0x1A, 0x06, 0x00 } );              // 400 kHz
    add( { 1, 1, 1 } );                             // three pulls up
    add( { 0, 1 } );                                // Relay 1 active low, Relay 2 high
    add( { 0x00 } );                                // the address: auto
    add( { 0x00, 0x00 } );                          // the tail
    REQUIRE( want.size() == pinmap_store::kBlobBytes );
    CHECK( blobOf( pinmap::kDefaultMap ) == want );
}

TEST( "pinmap_store", "every field of a map has its place in the blob" ) {
    const Bytes blob = blobOf( otherMap() );
    CHECK( blob[kPinsAt] == 8U && blob[kPinsAt + 16U] == 12U );
    CHECK( blob[kClockAt] == 0xA0U && blob[kClockAt + 1U] == 0x86U
           && blob[kClockAt + 2U] == 0x01U );
    const Bytes flags( blob.begin() + kPullSdaAt, blob.begin() + kAddressAt );
    CHECK( flags == Bytes( { 0, 1, 0, 1, 0 } ) );
    CHECK( blob[kAddressAt] == 0x45U );
}

// --- Load and save ---------------------------------------------------------------------

TEST( "pinmap_store", "a new flash holds no record: the defaults" ) {
    Scope scope;
    loadsDefaults( pinmap_store::Found::Empty );
    CHECK( scope->flash.programs == 0U );
}

TEST( "pinmap_store", "a saved map loads back field by field, as a reboot reads it" ) {
    Scope scope;
    REQUIRE( pinmap::validate( otherMap() ).problem == pinmap::Problem::None );
    CHECK( pinmap_store::save( otherMap() ) == pinmap_store::Saved::Ok );
    pinmap_store::Loaded loaded = pinmap_store::load();
    CHECK( loaded.found == pinmap_store::Found::Stored && sameMap( loaded.map, otherMap() ) );
    // The defaults saved after it are the stored map now, not "no record".
    CHECK( pinmap_store::save( pinmap::kDefaultMap ) == pinmap_store::Saved::Ok );
    loaded = pinmap_store::load();
    CHECK( loaded.found == pinmap_store::Found::Stored );
    CHECK( sameMap( loaded.map, pinmap::kDefaultMap ) );
    CHECK( flash_log::head( kPins ).sequence == 2U );
    CHECK( scope->flash.refusedByRegion == 0 );
}

TEST( "pinmap_store", "each pull, each drive, the address and the clock come back" ) {
    Scope               scope;
    std::vector<PinMap> maps;
    for ( const Pull pull : { Pull::Off, Pull::Up } ) {
        PinMap map  = pinmap::kDefaultMap;
        map.pullSda = pull;
        maps.push_back( map );
        map         = pinmap::kDefaultMap;
        map.pullScl = pull;
        maps.push_back( map );
        map           = pinmap::kDefaultMap;
        map.pullAlert = pull;
        maps.push_back( map );
    }
    for ( const Drive drive : { Drive::ActiveLow, Drive::ActiveHigh } ) {
        PinMap map      = pinmap::kDefaultMap;
        map.relay1Drive = drive;
        maps.push_back( map );
        map             = pinmap::kDefaultMap;
        map.relay2Drive = drive;
        maps.push_back( map );
    }
    for ( const std::uint8_t address :
          { pinmap::kAddressAuto, std::uint8_t{ 0x40 }, std::uint8_t{ 0x4F } } ) {
        PinMap map  = pinmap::kDefaultMap;
        map.address = address;
        maps.push_back( map );
    }
    for ( const std::uint32_t hz : { pin_table::kClockMinHz, pin_table::kClockMaxHz } ) {
        PinMap map = pinmap::kDefaultMap;
        map.sclHz  = hz;
        maps.push_back( map );
    }
    for ( const PinMap& map : maps ) {
        REQUIRE( pinmap_store::save( map ) == pinmap_store::Saved::Ok );
        const pinmap_store::Loaded loaded = pinmap_store::load();
        CHECK( loaded.found == pinmap_store::Found::Stored && sameMap( loaded.map, map ) );
    }
}

// --- What the store refuses ------------------------------------------------------------

TEST( "pinmap_store", "another length, magic, version or table reads as no record" ) {
    const Bytes        good = blobOf( otherMap() );
    std::vector<Bytes> refused;
    refused.emplace_back( good.begin(), good.end() - 1 );  // 43 bytes
    Bytes longer = good;
    longer.push_back( 0 );
    refused.push_back( longer );  // 45 bytes
    Bytes magic = good;
    put32( magic, kMagicAt, 0x474F4C4BU );
    refused.push_back( magic );
    for ( const std::uint8_t version : { std::uint8_t{ 0 }, std::uint8_t{ 2 } } ) {
        Bytes other       = good;
        other[kVersionAt] = version;
        refused.push_back( other );
    }
    Bytes wide = good;
    put32( wide, kVersionAt, 0x00010001U );  // the low half right, the word not
    refused.push_back( wide );
    Bytes table = good;
    put32( table, kDigestAt, pin_table::kPinTableDigest ^ 1U );
    refused.push_back( table );
    for ( const Bytes& blob : refused ) {
        Scope scope;
        appendRaw( blob );
        loadsDefaults( pinmap_store::Found::Refused );
        CHECK( flash_log::head( kPins ).refused == 1U );
    }
}

TEST( "pinmap_store", "a pull or a drive byte that names no value reads as no record" ) {
    for ( std::size_t at = kPullSdaAt; at < kAddressAt; ++at ) {
        for ( const std::uint8_t value : { std::uint8_t{ 2 }, std::uint8_t{ 0xFF } } ) {
            Scope scope;
            Bytes blob = blobOf( pinmap::kDefaultMap );
            blob[at]   = value;
            appendRaw( blob );
            loadsDefaults( pinmap_store::Found::Refused );
        }
    }
}

TEST( "pinmap_store", "a byte of padding that is not zero reads as no record" ) {
    for ( const std::size_t at : { kTailAt, kTailAt + 1U } ) {
        Scope scope;
        Bytes blob = blobOf( otherMap() );
        blob[at]   = 1;
        appendRaw( blob );
        loadsDefaults( pinmap_store::Found::Refused );
    }
}

TEST( "pinmap_store", "a map validate() refuses reads as no record, and is never saved" ) {
    std::vector<PinMap> refused;
    refused.push_back( pinmap::withPin( pinmap::kDefaultMap, Role::Sda, 45 ) );    // reserved
    refused.push_back( pinmap::withPin( pinmap::kDefaultMap, Role::Relay2, 4 ) );  // shared
    refused.push_back( pinmap::withPin( pinmap::kDefaultMap, Role::Alert, 39 ) );  // too high
    refused.push_back( pinmap::withPin( pinmap::kDefaultMap, Role::Scl, pin_table::kNoGpio ) );
    PinMap address  = pinmap::kDefaultMap;
    address.address = 0x3F;
    refused.push_back( address );
    PinMap clock = pinmap::kDefaultMap;
    clock.sclHz  = pin_table::kClockMaxHz + 1U;
    refused.push_back( clock );
    for ( const PinMap& map : refused ) {
        REQUIRE( pinmap::validate( map ).problem != pinmap::Problem::None );
        Scope scope;
        appendRaw( blobOf( map ) );
        loadsDefaults( pinmap_store::Found::Refused );
        const std::size_t programs = scope->flash.programs;
        CHECK( pinmap_store::save( map ) == pinmap_store::Saved::Refused );
        CHECK( scope->flash.programs == programs );
    }
}

TEST( "pinmap_store", "a refused record newer than the stored map gives the defaults" ) {
    Scope scope;
    REQUIRE( pinmap_store::save( otherMap() ) == pinmap_store::Saved::Ok );
    Bytes table = blobOf( otherMap() );
    put32( table, kDigestAt, 0x12345678U );
    appendRaw( table );
    // Not the older map: nobody chose it after the refused one.
    loadsDefaults( pinmap_store::Found::Refused );
    CHECK( flash_log::head( kPins ).sequence == 1U );
    CHECK( flash_log::head( kPins ).refusedSequence == 2U );
    // A save after it is the newest, in the other sector, and is the map.
    REQUIRE( pinmap_store::save( otherMap() ) == pinmap_store::Saved::Ok );
    CHECK( flash_log::head( kPins ).sector == 1U && flash_log::head( kPins ).sequence == 3U );
    const pinmap_store::Loaded loaded = pinmap_store::load();
    CHECK( loaded.found == pinmap_store::Found::Stored && sameMap( loaded.map, otherMap() ) );
}

TEST( "pinmap_store", "a refused record older than the stored map is history" ) {
    Scope scope;
    Bytes table = blobOf( otherMap() );
    put32( table, kDigestAt, 0x12345678U );
    appendRaw( table );
    REQUIRE( pinmap_store::save( otherMap() ) == pinmap_store::Saved::Ok );
    const pinmap_store::Loaded loaded = pinmap_store::load();
    CHECK( loaded.found == pinmap_store::Found::Stored && sameMap( loaded.map, otherMap() ) );
    CHECK( flash_log::head( kPins ).refused == 1U
           && flash_log::head( kPins ).refusedSequence == 1U );
}

// --- The flash's failures --------------------------------------------------------------

TEST( "pinmap_store", "a read that fails at the mount or at the load: the defaults" ) {
    Scope scope;
    REQUIRE( pinmap_store::save( otherMap() ) == pinmap_store::Saved::Ok );
    // The mount's first read.
    scope->flash.readsUntilFailure = 0;
    loadsDefaults( pinmap_store::Found::Unreadable );
    // The load's read of the record, after the mount's reads all succeed:
    // a load reads the header and the blob again, two reads.
    const std::size_t before = scope->flash.reads;
    REQUIRE( pinmap_store::load().found == pinmap_store::Found::Stored );
    const auto mountReads          = static_cast<long long>( scope->flash.reads - before ) - 2;
    scope->flash.readsUntilFailure = mountReads;
    loadsDefaults( pinmap_store::Found::Unreadable );
    CHECK( pinmap_store::load().found == pinmap_store::Found::Stored );
}

TEST( "pinmap_store", "a save the flash fails, or with no sequence left, answers Failed" ) {
    Scope scope;
    scope->flash.failProgram = true;
    CHECK( pinmap_store::save( otherMap() ) == pinmap_store::Saved::Failed );
    scope->flash.failProgram = false;
    loadsDefaults( pinmap_store::Found::Empty );
    // A read that fails at the save's mount is read again by the save.
    scope->flash.readsUntilFailure = 0;
    CHECK( pinmap_store::save( otherMap() ) == pinmap_store::Saved::Ok );
    // A record at the last sequence takes no other.
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, flash_log::kLastSequence, blobOf( pinmap::kDefaultMap ) );
    CHECK( pinmap_store::save( otherMap() ) == pinmap_store::Saved::Failed );
    const pinmap_store::Loaded loaded = pinmap_store::load();
    CHECK( loaded.found == pinmap_store::Found::Stored );
    CHECK( sameMap( loaded.map, pinmap::kDefaultMap ) );
}

TEST( "pinmap_store", "a power cut after every byte of a pin save leaves the old map or the new" ) {
    for ( std::size_t keep = 0; keep <= 16U + pinmap_store::kBlobBytes; ++keep ) {
        for ( const bool tears : { false, true } ) {
            Scope scope;
            REQUIRE( pinmap_store::save( pinmap::kDefaultMap ) == pinmap_store::Saved::Ok );
            scope->flash.cutAt    = static_cast<long long>( scope->flash.changes );
            scope->flash.cutKeep  = keep;
            scope->flash.cutTears = tears;
            CHECK( pinmap_store::save( otherMap() ) == pinmap_store::Saved::Failed );
            scope->flash.powerOn();
            const pinmap_store::Loaded loaded = pinmap_store::load();
            CHECK( loaded.found == pinmap_store::Found::Stored );
            const bool whole = keep == 16U + pinmap_store::kBlobBytes;
            CHECK( sameMap( loaded.map, whole ? otherMap() : pinmap::kDefaultMap ) );
        }
    }
}

// --- The calibration's log -------------------------------------------------------------

TEST( "pinmap_store", "pin saves leave the calibration's log alone, and the reverse" ) {
    Scope            scope;
    CalibrationStore calibration;
    calibration.setShuntMicroOhm( 100'012 );
    calibration.setOffset( ina228::ShuntRange::Wide163mV, 482'954, 25'000 );
    REQUIRE( calibration.save() );
    // A log's two sectors, as they are in the flash now.
    const auto sectors = [&scope]( std::uint32_t address ) {
        const std::span<const std::uint8_t> flash( scope->flash.bytes );
        const auto log = flash.subspan( address - scope->flash.base, flash_port::kLogBytes );
        return Bytes( log.begin(), log.end() );
    };
    const Bytes before = sectors( flash_port::kLogBaseAddress );
    for ( std::uint32_t n = 0; n < 200U; ++n ) {
        REQUIRE( pinmap_store::save( ( n % 2U ) == 0U ? otherMap() : pinmap::kDefaultMap )
                 == pinmap_store::Saved::Ok );
    }
    CHECK( sectors( flash_port::kLogBaseAddress ) == before );
    CHECK( scope->flash.erases[0] == 0U && scope->flash.erases[1] == 0U );
    CHECK( scope->flash.erases[2] + scope->flash.erases[3] >= 2U );
    CalibrationStore reread;
    REQUIRE( reread.load() );
    CHECK( reread.value().shuntMicroOhm == 100'012U );
    // The calibration's saves, the pin map unchanged.
    const Bytes pinBytes = sectors( flash_port::kPinLogBaseAddress );
    for ( std::uint32_t n = 0; n < 120U; ++n ) {
        calibration.setDate( 20261125U + n );
        REQUIRE( calibration.save() );
    }
    CHECK( sectors( flash_port::kPinLogBaseAddress ) == pinBytes );
    const pinmap_store::Loaded loaded = pinmap_store::load();
    CHECK( loaded.found == pinmap_store::Found::Stored );
    CHECK( sameMap( loaded.map, pinmap::kDefaultMap ) );
}
