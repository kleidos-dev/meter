// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_flash_log.cpp
 * @brief flash_log.cpp on the harness's model of the flash (sim::Flash): the
 *        record's bytes, the append and the move to the other sector, the
 *        scan's choice and what it refuses, the refusals and every flash
 *        failure, a power cut after chosen bytes of every write and erase of
 *        a run that changes sectors several times, 10,000 saves, and the two
 *        logs (FW-03): each cut after every byte of every write and erase of
 *        a run of its own that changes sectors three times, with the other
 *        log's records checked after each cut, and their states kept apart.
 */
#include "crc32.h"
#include "flash_log.h"
#include "flash_port.h"
#include "scope.h"
#include "unit.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

using namespace kleidos::meter;
using bench::Scope;

namespace {

/// The calibration's log, the one these cases run on.
constexpr flash_log::Log kCal = flash_log::Log::Calibration;

constexpr std::uint32_t kSector = flash_log::kSectorBytes;

using Bytes = std::vector<std::uint8_t>;

/// A blob of @p length bytes that tells save @p n apart from every other.
Bytes blobFor( std::uint32_t n, std::size_t length ) {
    Bytes blob( length );
    for ( std::size_t i = 0; i < length; ++i ) {
        blob[i] = static_cast<std::uint8_t>( ( n * 131U ) + ( i * 7U ) + 1U );
    }
    return blob;
}

/// Saves @p blob.
flash_log::Status save( const Bytes& blob ) {
    return flash_log::save( kCal, blob.data(), blob.size() );
}

/// The newest blob, or an empty one when load() does not answer Ok.
Bytes loaded() {
    std::array<std::uint8_t, flash_log::kMaxBlobBytes> out    = {};
    std::size_t                                        length = 0;
    if ( flash_log::load( kCal, out.data(), out.size(), length ) != flash_log::Status::Ok ) {
        return {};
    }
    return { out.begin(), out.begin() + static_cast<std::ptrdiff_t>( length ) };
}

/// The flash address of @p offset in @p sector of the model.
std::uint32_t addressOf( const sim::Flash& flash, std::uint32_t sector, std::uint32_t offset ) {
    return flash.base + ( sector * kSector ) + offset;
}

/// Stores @p value at @p address of the model, least significant byte first.
void plantWord( sim::Flash& flash, std::uint32_t address, std::uint32_t value ) {
    for ( std::uint32_t i = 0; i < 4U; ++i ) {
        flash.at( address + i ) = static_cast<std::uint8_t>( value >> ( 8U * i ) );
    }
}

/// How a planted record departs from a valid one.
struct Plant {
    std::uint32_t magic    = flash_log::kMagic;
    std::uint32_t crcDelta = 0;   ///< Added to the right CRC.
    long long     length   = -1;  ///< The length word, when not the blob's.
};

/// Writes a record straight into the model, past the port: what an earlier
/// image or a failing flash left there.
void plantRecord( sim::Flash& flash, std::uint32_t sector, std::uint32_t offset,
                  std::uint32_t sequence, const Bytes& blob, const Plant& how = {} ) {
    const std::uint32_t address       = addressOf( flash, sector, offset );
    const auto          length        = how.length >= 0 ? static_cast<std::uint32_t>( how.length )
                                                        : static_cast<std::uint32_t>( blob.size() );
    std::array<std::uint8_t, 12> head = {};
    for ( std::uint32_t i = 0; i < 4U; ++i ) {
        head[i]      = static_cast<std::uint8_t>( how.magic >> ( 8U * i ) );
        head[4U + i] = static_cast<std::uint8_t>( sequence >> ( 8U * i ) );
        head[8U + i] = static_cast<std::uint8_t>( length >> ( 8U * i ) );
    }
    std::uint32_t crc = crc32::update( crc32::kInitial, head.data(), head.size() );
    crc = ( crc32::update( crc, blob.data(), blob.size() ) ^ crc32::kInitial ) + how.crcDelta;
    for ( std::uint32_t i = 0; i < 12U; ++i ) {
        flash.at( address + i ) = head[i];
    }
    plantWord( flash, address + 12U, crc );
    for ( std::size_t i = 0; i < blob.size(); ++i ) {
        flash.at( address + 16U + static_cast<std::uint32_t>( i ) ) = blob[i];
    }
}

/// A record as an independent reading of the model finds it.
struct Record {
    std::uint32_t sequence = 0;
    std::uint32_t offset   = 0;
    Bytes         blob;
};

/// One sector as an independent reading of the model finds it: the records
/// from its start, where they end, and whether everything after that reads
/// erased.
struct Parsed {
    std::vector<Record> records;
    std::uint32_t       end   = 0;
    bool                clean = true;  ///< Erased from `end` to the sector's end.
};

/// Reads @p sector of the model as flash_log.h says a record is laid out,
/// without the module's code.
Parsed parse( sim::Flash& flash, std::uint32_t sector ) {
    Parsed found;
    auto   word = [&]( std::uint32_t address ) {
        std::uint32_t value = 0;
        for ( std::uint32_t i = 4; i > 0U; --i ) {
            value = ( value << 8U ) | flash.at( address + i - 1U );
        }
        return value;
    };
    while ( found.end + 16U <= kSector ) {
        const std::uint32_t address = addressOf( flash, sector, found.end );
        const std::uint32_t length  = word( address + 8U );
        if ( word( address ) != flash_log::kMagic || length == 0U
             || length > flash_log::kMaxBlobBytes
             || flash_log::recordBytes( length ) > kSector - found.end ) {
            break;
        }
        std::array<std::uint8_t, 12> head = {};
        for ( std::uint32_t i = 0; i < 12U; ++i ) {
            head[i] = flash.at( address + i );
        }
        Bytes blob( length );
        for ( std::uint32_t i = 0; i < length; ++i ) {
            blob[i] = flash.at( address + 16U + i );
        }
        const std::uint32_t crc =
            crc32::update( crc32::update( crc32::kInitial, head.data(), head.size() ), blob.data(),
                           blob.size() )
            ^ crc32::kInitial;
        if ( crc != word( address + 12U ) ) {
            break;
        }
        found.records.push_back( { word( address + 4U ), found.end, blob } );
        found.end += flash_log::recordBytes( length );
    }
    for ( std::uint32_t offset = found.end; offset < kSector; ++offset ) {
        if ( flash.at( addressOf( flash, sector, offset ) ) != 0xFFU ) {
            found.clean = false;
        }
    }
    return found;
}

/// A fresh flash in @p scope's world, and the log mounted on it.
void fresh( Scope& scope ) {
    scope->flash = sim::Flash{};
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
}

/// Saves blobs of @p length, numbered on from @p last, while the first
/// sector has room for the next, and returns the last number saved: the next
/// save of that length moves the log to the second sector.
std::uint32_t fillFirstSector( std::size_t length, std::uint32_t last = 0 ) {
    const std::uint32_t bytes = flash_log::recordBytes( static_cast<std::uint32_t>( length ) );
    while ( flash_log::head( kCal ).sector == 0U
            && ( !flash_log::head( kCal ).hasRecord
                 || flash_log::head( kCal ).offset + 2U * bytes <= kSector ) ) {
        ++last;
        REQUIRE( save( blobFor( last, length ) ) == flash_log::Status::Ok );
    }
    return last;
}

}  // namespace

// --- The record and the append -------------------------------------------------

TEST( "flash_log", "a new flash mounts empty, reading one header of each sector" ) {
    Scope scope;
    fresh( scope );
    const flash_log::Head head = flash_log::head( kCal );
    CHECK( head.mounted && head.scanned && !head.hasRecord );
    CHECK( head.sequence == 0U && head.sector == 0U );
    CHECK( scope->flash.reads == 2U );
    std::array<std::uint8_t, 8> out    = {};
    std::size_t                 length = 99;
    CHECK( flash_log::load( kCal, out.data(), out.size(), length ) == flash_log::Status::Empty );
    CHECK( length == 99U );
}

TEST( "flash_log", "the first save is sequence 1 at the first sector's start, byte for byte" ) {
    Scope scope;
    fresh( scope );
    const Bytes blob = { 'c', 'a', 'l', 'i', 'b', 'r', 'a', 't', 'i', 'o', 'n' };
    REQUIRE( save( blob ) == flash_log::Status::Ok );
    // The magic "KLOG", sequence 1, length 11, and the CRC zlib.crc32() gives
    // for those twelve bytes and the blob; then the blob, then erased.
    const std::array<std::uint8_t, 16> header = { 0x4B, 0x4C, 0x4F, 0x47, 0x01, 0x00, 0x00, 0x00,
                                                  0x0B, 0x00, 0x00, 0x00, 0xAB, 0x36, 0x81, 0xDC };
    for ( std::uint32_t i = 0; i < 16U; ++i ) {
        CHECK( scope->flash.at( scope->flash.base + i ) == header[i] );
    }
    for ( std::uint32_t i = 0; i < 11U; ++i ) {
        CHECK( scope->flash.at( scope->flash.base + 16U + i ) == blob[i] );
    }
    CHECK( scope->flash.at( scope->flash.base + 27U ) == 0xFFU );
    CHECK( scope->flash.programs == 1U && scope->flash.erases[0] == 0U
           && scope->flash.erases[1] == 0U );
    const flash_log::Head head = flash_log::head( kCal );
    CHECK( head.hasRecord && head.sequence == 1U && head.sector == 0U && head.offset == 0U );
    CHECK( head.length == 11U );
    CHECK( loaded() == blob );
}

TEST( "flash_log", "saves append on word boundaries, the padding left erased" ) {
    Scope scope;
    fresh( scope );
    std::uint32_t offset = 0;
    for ( std::uint32_t n = 1; n <= 9U; ++n ) {
        REQUIRE( save( blobFor( n, n ) ) == flash_log::Status::Ok );
        CHECK( flash_log::head( kCal ).offset == offset );
        for ( std::uint32_t pad = 16U + n; pad < flash_log::recordBytes( n ); ++pad ) {
            CHECK( scope->flash.at( scope->flash.base + offset + pad ) == 0xFFU );
        }
        offset += flash_log::recordBytes( n );
    }
    const Parsed sector = parse( scope->flash, 0 );
    REQUIRE( sector.records.size() == 9U );
    for ( std::uint32_t n = 1; n <= 9U; ++n ) {
        CHECK( sector.records[n - 1U].sequence == n );
        CHECK( sector.records[n - 1U].blob == blobFor( n, n ) );
    }
    CHECK( sector.clean && sector.end == offset );
    CHECK( loaded() == blobFor( 9, 9 ) );
}

TEST( "flash_log", "a remount finds the newest record, as a reboot would" ) {
    Scope scope;
    fresh( scope );
    for ( std::uint32_t n = 1; n <= 30U; ++n ) {
        REQUIRE( save( blobFor( n, 40 ) ) == flash_log::Status::Ok );
    }
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == 30U );
    CHECK( loaded() == blobFor( 30, 40 ) );
    REQUIRE( save( blobFor( 31, 40 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == 31U && flash_log::head( kCal ).offset == 30U * 56U );
}

TEST( "flash_log", "the largest blob saves, one byte more and none at all are refused" ) {
    Scope scope;
    fresh( scope );
    const Bytes largest = blobFor( 1, flash_log::kMaxBlobBytes );
    CHECK( save( largest ) == flash_log::Status::Ok );
    const Bytes larger = blobFor( 2, flash_log::kMaxBlobBytes + 1U );
    CHECK( save( larger ) == flash_log::Status::BadLength );
    CHECK( flash_log::save( kCal, largest.data(), 0 ) == flash_log::Status::BadLength );
    CHECK( flash_log::save( kCal, nullptr, 4 ) == flash_log::Status::BadLength );
    CHECK( scope->flash.programs == 1U );
    CHECK( loaded() == largest );
}

TEST( "flash_log", "load refuses a null buffer and one with no room, and takes an exact fit" ) {
    Scope scope;
    fresh( scope );
    const Bytes blob = blobFor( 1, 33 );
    REQUIRE( save( blob ) == flash_log::Status::Ok );
    std::array<std::uint8_t, 33> out    = {};
    std::size_t                  length = 0;
    CHECK( flash_log::load( kCal, nullptr, 33, length ) == flash_log::Status::BadLength );
    CHECK( flash_log::load( kCal, out.data(), 32, length ) == flash_log::Status::BadLength );
    CHECK( length == 0U );
    CHECK( flash_log::load( kCal, out.data(), out.size(), length ) == flash_log::Status::Ok );
    CHECK( length == 33U && std::equal( out.begin(), out.end(), blob.begin() ) );
}

// --- The move to the other sector --------------------------------------------

TEST( "flash_log", "a full sector moves the log to the other one, erased only then" ) {
    Scope scope;
    fresh( scope );
    // 35 records of 116 bytes end at 4060: the 36th goes to sector 1.
    const std::uint32_t filled = fillFirstSector( 100 );
    CHECK( filled == 35U && flash_log::head( kCal ).sector == 0U );
    CHECK( scope->flash.erases[0] == 0U && scope->flash.erases[1] == 0U );
    const Bytes before( scope->flash.bytes.begin(), scope->flash.bytes.begin() + kSector );
    REQUIRE( save( blobFor( filled + 1U, 100 ) ) == flash_log::Status::Ok );
    CHECK( scope->flash.erases[0] == 0U && scope->flash.erases[1] == 1U );
    const flash_log::Head head = flash_log::head( kCal );
    CHECK( head.sector == 1U && head.offset == 0U && head.sequence == filled + 1U );
    CHECK( std::equal( before.begin(), before.end(), scope->flash.bytes.begin() ) );
    CHECK( loaded() == blobFor( filled + 1U, 100 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 1U
           && flash_log::head( kCal ).sequence == filled + 1U );
}

TEST( "flash_log", "a record that would cross the sector's end goes to the other sector" ) {
    Scope scope;
    fresh( scope );
    // 31 records of 128 bytes end at 3968; one of 112 bytes leaves 16, room
    // for a header only.
    for ( std::uint32_t n = 1; n <= 31U; ++n ) {
        REQUIRE( save( blobFor( n, 112 ) ) == flash_log::Status::Ok );
    }
    REQUIRE( save( blobFor( 32, 96 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 0U && flash_log::head( kCal ).offset == 3968U );
    REQUIRE( save( blobFor( 33, 1 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 1U && flash_log::head( kCal ).offset == 0U );
    CHECK( scope->flash.erases[1] == 1U );
}

TEST( "flash_log", "the smallest record fills a sector's last 20 bytes, and a remount finds it" ) {
    Scope scope;
    fresh( scope );
    for ( std::uint32_t n = 1; n <= 31U; ++n ) {
        REQUIRE( save( blobFor( n, 112 ) ) == flash_log::Status::Ok );
    }
    REQUIRE( save( blobFor( 32, 92 ) ) == flash_log::Status::Ok );  // ends at 4076
    REQUIRE( save( blobFor( 33, 4 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 0U && flash_log::head( kCal ).offset == 4076U );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == 33U && flash_log::head( kCal ).offset == 4076U );
    CHECK( loaded() == blobFor( 33, 4 ) );
}

TEST( "flash_log", "a sector filled to its last byte is read to its end" ) {
    Scope scope;
    fresh( scope );
    // 32 records of 128 bytes are exactly the sector.
    for ( std::uint32_t n = 1; n <= 32U; ++n ) {
        REQUIRE( save( blobFor( n, 112 ) ) == flash_log::Status::Ok );
    }
    CHECK( flash_log::head( kCal ).sector == 0U && flash_log::head( kCal ).offset == 31U * 128U );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == 32U
           && flash_log::head( kCal ).offset == 31U * 128U );
    REQUIRE( save( blobFor( 33, 112 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 1U );
}

TEST( "flash_log", "a sector whose last 12 bytes are left is read to a header's room" ) {
    Scope scope;
    fresh( scope );
    for ( std::uint32_t n = 1; n <= 31U; ++n ) {
        REQUIRE( save( blobFor( n, 112 ) ) == flash_log::Status::Ok );
    }
    REQUIRE( save( blobFor( 32, 100 ) ) == flash_log::Status::Ok );  // ends at 4084
    const std::size_t reads = scope->flash.reads;
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    // 32 records of two reads each, then sector 1's one header.
    CHECK( scope->flash.reads - reads == 65U );
    CHECK( flash_log::head( kCal ).sequence == 32U );
    REQUIRE( save( blobFor( 33, 1 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 1U );
}

TEST( "flash_log", "bytes that do not read erased after the last record move the log" ) {
    Scope scope;
    fresh( scope );
    REQUIRE( save( blobFor( 1, 8 ) ) == flash_log::Status::Ok );
    // The next header reads erased, so the scan calls the sector clean; a
    // byte further on, where the next record goes, does not.
    scope->flash.at( scope->flash.base + 24U + 40U ) = 0x7F;
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    REQUIRE( save( blobFor( 2, 64 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 1U && flash_log::head( kCal ).sequence == 2U );
    CHECK( scope->flash.erases[1] == 1U && scope->flash.zeroToOne == 0 );
    CHECK( loaded() == blobFor( 2, 64 ) );
}

// --- The scan's choice -----------------------------------------------------------

TEST( "flash_log", "the newest record wins whichever sector holds it" ) {
    Scope scope;
    // Only sector 1 holds a record.
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 1, 0, 5, blobFor( 5, 10 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 1U && flash_log::head( kCal ).sequence == 5U );
    CHECK( loaded() == blobFor( 5, 10 ) );
    // Both do, sector 1's newer.
    plantRecord( scope->flash, 0, 0, 4, blobFor( 4, 10 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 1U && flash_log::head( kCal ).sequence == 5U );
    // Both do, sector 0's newer.
    plantRecord( scope->flash, 0, 28, 9, blobFor( 9, 10 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 0U && flash_log::head( kCal ).sequence == 9U );
    CHECK( flash_log::head( kCal ).offset == 28U );
    CHECK( loaded() == blobFor( 9, 10 ) );
    // The next save appends after sector 0's last record.
    REQUIRE( save( blobFor( 10, 10 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 0U && flash_log::head( kCal ).offset == 56U );
    CHECK( flash_log::head( kCal ).sequence == 10U && scope->flash.erases[1] == 0U );
}

TEST( "flash_log", "a record of sequence 0, which no save writes, still counts" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 1, 0, 0, blobFor( 0, 6 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).hasRecord && flash_log::head( kCal ).sector == 1U );
    CHECK( flash_log::head( kCal ).sequence == 0U );
    CHECK( loaded() == blobFor( 0, 6 ) );
    REQUIRE( save( blobFor( 1, 6 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 1U && flash_log::head( kCal ).offset == 24U );
    CHECK( flash_log::head( kCal ).sequence == 1U );
}

TEST( "flash_log", "of two records with one sequence, the first found wins" ) {
    Scope scope;
    // Within a sector: the earlier one.
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 0, 0, 5, blobFor( 1, 4 ) );
    plantRecord( scope->flash, 0, 20, 5, blobFor( 2, 4 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).offset == 0U && loaded() == blobFor( 1, 4 ) );
    // Across the sectors: the first sector's.
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 0, 0, 5, blobFor( 1, 4 ) );
    plantRecord( scope->flash, 1, 0, 5, blobFor( 2, 4 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 0U && loaded() == blobFor( 1, 4 ) );
}

TEST( "flash_log", "within a sector the highest sequence wins, not the last" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 0, 0, 7, blobFor( 7, 4 ) );
    plantRecord( scope->flash, 0, 20, 3, blobFor( 3, 4 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == 7U && flash_log::head( kCal ).offset == 0U );
    CHECK( loaded() == blobFor( 7, 4 ) );
    REQUIRE( save( blobFor( 8, 4 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).offset == 40U && flash_log::head( kCal ).sequence == 8U );
}

TEST( "flash_log",
      "a header or CRC that does not hold ends the scan, and its sector takes nothing more" ) {
    // Each record but the first two is whole, its CRC right for what it
    // holds: only the check named refuses it.
    struct Case {
        const char*   what;
        std::uint32_t before;  ///< Records saved first, of 112 bytes of blob.
        std::size_t   blob;    ///< The planted record's blob.
        Plant         how;     ///< How it departs.
    };
    const std::array<Case, 5> cases = { {
        { "another magic", 1, 4, { 0x474F4C4AU, 0, -1 } },
        { "a CRC one off", 1, 4, { flash_log::kMagic, 1, -1 } },
        { "a length of 0", 1, 0, {} },
        { "a length above the largest blob", 1, flash_log::kMaxBlobBytes + 1U, {} },
        // At 3968 a record of the largest blob, 144 bytes, has 128 left: it
        // runs 16 bytes into the second sector.
        { "a record past the sector's end", 31, flash_log::kMaxBlobBytes, {} },
    } };
    for ( const Case& c : cases ) {
        Scope scope;
        fresh( scope );
        for ( std::uint32_t n = 1; n <= c.before; ++n ) {
            REQUIRE( save( blobFor( n, 112 ) ) == flash_log::Status::Ok );
        }
        plantRecord( scope->flash, 0, c.before * 128U, c.before + 1U, blobFor( 99, c.blob ),
                     c.how );
        REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
        CHECK( flash_log::head( kCal ).sequence == c.before );
        CHECK( loaded() == blobFor( c.before, 112 ) );
        REQUIRE( save( blobFor( 100, 4 ) ) == flash_log::Status::Ok );
        const bool moved =
            flash_log::head( kCal ).sector == 1U && flash_log::head( kCal ).offset == 0U;
        CHECK( moved && scope->flash.erases[1] == 1U && scope->flash.zeroToOne == 0 );
        if ( !moved ) {
            (void)unit::check( false, c.what, __FILE__, __LINE__ );
        }
    }
}

TEST( "flash_log", "a flash with no valid record at all starts in the second sector" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    for ( std::uint32_t i = 0; i < 64U; ++i ) {
        scope->flash.at( scope->flash.base + i ) = static_cast<std::uint8_t>( i );
    }
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( !flash_log::head( kCal ).hasRecord );
    REQUIRE( save( blobFor( 1, 12 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 1U && flash_log::head( kCal ).sequence == 1U );
    CHECK( scope->flash.erases[0] == 0U && scope->flash.erases[1] == 1U );
}

TEST( "flash_log", "the last sequence is written once, and then no save is taken" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 0, 0, flash_log::kLastSequence - 1U, blobFor( 1, 4 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    REQUIRE( save( blobFor( 2, 4 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == flash_log::kLastSequence );
    const std::size_t programs = scope->flash.programs;
    CHECK( save( blobFor( 3, 4 ) ) == flash_log::Status::Exhausted );
    CHECK( scope->flash.programs == programs );
    CHECK( loaded() == blobFor( 2, 4 ) );
}

// --- The owner's judgment of a record (the Acceptor, FW-16) ---------------------

namespace {

/// Whether judge() refuses: a blob whose first byte is 0xEE, or every blob
/// while @c refuseAll is set.
bool refuseAll = false;

/// The tests' Acceptor.
bool judge( const std::uint8_t* blob, std::size_t length ) noexcept {
    const bool marked = length > 0U && blob[0] == 0xEEU;
    return !refuseAll && !marked;
}

/// A blob judge() refuses.
Bytes refusedBlob( std::size_t length ) {
    Bytes blob( length, 0x11U );
    blob[0] = 0xEEU;
    return blob;
}

}  // namespace

TEST( "flash_log", "a record the Acceptor refuses is passed over as a bad CRC" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 0, 0, 1, blobFor( 1, 8 ) );
    plantRecord( scope->flash, 0, 24, 2, refusedBlob( 8 ) );
    plantRecord( scope->flash, 0, 48, 3, blobFor( 3, 8 ) );
    // With no Acceptor every whole record counts: sequence 3 wins.
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == 3U && flash_log::head( kCal ).refused == 0U );
    // With one, the scan stops at the refused record: what follows it in the
    // sector is not read, and sequence 1 is the log's.
    REQUIRE( flash_log::mount( kCal, scope->flash.base, &judge ) == flash_log::Status::Ok );
    const flash_log::Head head = flash_log::head( kCal );
    CHECK( head.hasRecord && head.sequence == 1U && head.offset == 0U );
    CHECK( head.refused == 1U && head.refusedSequence == 2U );
    CHECK( loaded() == blobFor( 1, 8 ) );
    // The next save goes to the other sector, numbered above the refused
    // record, so that it is the newest the sectors hold.
    REQUIRE( save( blobFor( 4, 8 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 1U && flash_log::head( kCal ).sequence == 3U );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, &judge ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == 3U && flash_log::head( kCal ).sector == 1U );
    CHECK( loaded() == blobFor( 4, 8 ) );
}

TEST( "flash_log", "a refused record in either sector is counted, the higher sequence kept" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 0, 0, 7, refusedBlob( 4 ) );
    plantRecord( scope->flash, 1, 0, 5, refusedBlob( 4 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, &judge ) == flash_log::Status::Ok );
    CHECK( !flash_log::head( kCal ).hasRecord );
    CHECK( flash_log::head( kCal ).refused == 2U && flash_log::head( kCal ).refusedSequence == 7U );
    std::array<std::uint8_t, 4> out    = {};
    std::size_t                 length = 0;
    CHECK( flash_log::load( kCal, out.data(), out.size(), length ) == flash_log::Status::Empty );
    // Sector 1 alone holds a refused record: its sequence is the one kept.
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 1, 0, 9, refusedBlob( 4 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, &judge ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).refused == 1U && flash_log::head( kCal ).refusedSequence == 9U );
    // The first save starts sector 0 again, numbered 10.
    REQUIRE( save( blobFor( 1, 4 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sector == 0U && flash_log::head( kCal ).sequence == 10U );
}

TEST( "flash_log", "a save the Acceptor refuses writes nothing" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    REQUIRE( flash_log::mount( kCal, scope->flash.base, &judge ) == flash_log::Status::Ok );
    CHECK( save( refusedBlob( 8 ) ) == flash_log::Status::Refused );
    CHECK( scope->flash.programs == 0U && scope->flash.reads == 2U );
    CHECK( save( blobFor( 1, 8 ) ) == flash_log::Status::Ok );
    // Without an Acceptor the same blob is taken.
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( save( refusedBlob( 8 ) ) == flash_log::Status::Ok );
}

TEST( "flash_log", "a load of a record the Acceptor no longer takes reads the sectors again" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 0, 0, 1, blobFor( 1, 8 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, &judge ) == flash_log::Status::Ok );
    refuseAll                          = true;
    std::array<std::uint8_t, 8> out    = {};
    std::size_t                 length = 0;
    CHECK( flash_log::load( kCal, out.data(), out.size(), length )
           == flash_log::Status::FlashError );
    CHECK( !flash_log::head( kCal ).scanned );
    // The next call reads them, and finds the record refused.
    CHECK( flash_log::load( kCal, out.data(), out.size(), length ) == flash_log::Status::Empty );
    CHECK( flash_log::head( kCal ).refused == 1U );
    refuseAll = false;
}

TEST( "flash_log", "a refused record at the last sequence leaves no sequence to save" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 0, 0, 2, blobFor( 2, 4 ) );
    plantRecord( scope->flash, 1, 0, flash_log::kLastSequence, refusedBlob( 4 ) );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, &judge ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == 2U );
    CHECK( save( blobFor( 3, 4 ) ) == flash_log::Status::Exhausted );
    CHECK( scope->flash.programs == 0U );
}

// --- Mounting and the refusals ---------------------------------------------------

TEST( "flash_log", "nothing is taken before a mount, nor after a refused one" ) {
    Scope scope;
    scope->flash                       = sim::Flash{};
    std::array<std::uint8_t, 4> out    = {};
    std::size_t                 length = 0;
    CHECK( flash_log::mount( kCal, scope->flash.base + 1U, nullptr )
           == flash_log::Status::BadAddress );
    CHECK( !flash_log::head( kCal ).mounted );
    CHECK( save( blobFor( 1, 4 ) ) == flash_log::Status::NotMounted );
    CHECK( flash_log::load( kCal, out.data(), out.size(), length )
           == flash_log::Status::NotMounted );
    CHECK( flash_log::mount( kCal, flash_log::kLastBaseAddress + kSector, nullptr )
           == flash_log::Status::BadAddress );
    CHECK( !flash_log::head( kCal ).mounted );
    CHECK( scope->flash.reads == 0U && scope->flash.programs == 0U );
}

TEST( "flash_log", "a log at the top of the address space mounts, saves and loads" ) {
    Scope scope;
    scope->flash      = sim::Flash{};
    scope->flash.base = flash_log::kLastBaseAddress;
    // Not the calib partition: the image's region check would refuse every
    // write and erase there (flash_port.h).
    scope->flash.regionChecked = false;
    REQUIRE( flash_log::mount( kCal, flash_log::kLastBaseAddress, nullptr )
             == flash_log::Status::Ok );
    const std::uint32_t filled = fillFirstSector( 128 );
    for ( std::uint32_t n = filled + 1U; n <= filled + 3U; ++n ) {
        REQUIRE( save( blobFor( n, 128 ) ) == flash_log::Status::Ok );
    }
    CHECK( flash_log::head( kCal ).sector == 1U );
    CHECK( loaded() == blobFor( filled + 3U, 128 ) );
}

TEST( "flash_log", "a mount forgets what the log knew, as a reboot does" ) {
    Scope scope;
    fresh( scope );
    REQUIRE( save( blobFor( 1, 4 ) ) == flash_log::Status::Ok );
    // Another flash under the same address: only what it holds counts.
    scope->flash = sim::Flash{};
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( !flash_log::head( kCal ).hasRecord );
}

// --- Flash failures --------------------------------------------------------------

TEST( "flash_log", "a read that fails at a mount leaves the log to read the sectors again" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    plantRecord( scope->flash, 0, 0, 1, blobFor( 1, 8 ) );
    for ( long long failing = 0; failing < 4; ++failing ) {
        // The record's header and blob, the erased header after it, the
        // second sector's header.
        scope->flash.readsUntilFailure = failing;
        CHECK( flash_log::mount( kCal, scope->flash.base, nullptr )
               == flash_log::Status::FlashError );
        CHECK( flash_log::head( kCal ).mounted && !flash_log::head( kCal ).scanned );
    }
    // load() reads them again before it answers, and so does save().
    CHECK( loaded() == blobFor( 1, 8 ) );
    scope->flash.readsUntilFailure = 0;
    CHECK( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::FlashError );
    REQUIRE( save( blobFor( 2, 8 ) ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == 2U && flash_log::head( kCal ).offset == 24U );
}

TEST( "flash_log", "a scan that fails again fails the save and the load" ) {
    Scope scope;
    scope->flash                   = sim::Flash{};
    scope->flash.readsUntilFailure = 0;
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr )
             == flash_log::Status::FlashError );
    scope->flash.readsUntilFailure = 0;
    CHECK( save( blobFor( 1, 4 ) ) == flash_log::Status::FlashError );
    scope->flash.readsUntilFailure     = 0;
    std::array<std::uint8_t, 4> out    = {};
    std::size_t                 length = 0;
    CHECK( flash_log::load( kCal, out.data(), out.size(), length )
           == flash_log::Status::FlashError );
    CHECK( scope->flash.programs == 0U );
}

TEST( "flash_log", "every failure of a save answers FlashError and the next call reads again" ) {
    enum class Fault : std::uint8_t {
        RoomRead,     ///< The read of the append's room.
        Program,      ///< The write.
        ProgramLost,  ///< A write that reported success and wrote nothing.
        ReadBack,     ///< The read-back.
        StuckBlob,    ///< One byte of the blob never takes the write.
        StuckHeader,  ///< One byte of the header never does.
    };
    for ( const Fault fault : { Fault::RoomRead, Fault::Program, Fault::ProgramLost,
                                Fault::ReadBack, Fault::StuckBlob, Fault::StuckHeader } ) {
        Scope scope;
        fresh( scope );
        REQUIRE( save( blobFor( 1, 20 ) ) == flash_log::Status::Ok );
        sim::Flash& flash = scope->flash;
        switch ( fault ) {
            case Fault::RoomRead:
                flash.readsUntilFailure = 0;
                break;
            case Fault::Program:
                flash.failProgram = true;
                break;
            case Fault::ProgramLost:
                flash.programLost = true;
                break;
            case Fault::ReadBack:
                flash.readsUntilFailure = 1;
                break;
            case Fault::StuckBlob:
                flash.stuckAddress = flash.base + 36U + 16U + 3U;
                break;
            case Fault::StuckHeader:
                flash.stuckAddress = flash.base + 36U + 5U;
                break;
        }
        CHECK( save( blobFor( 2, 20 ) ) == flash_log::Status::FlashError );
        CHECK( !flash_log::head( kCal ).scanned );
        flash.failProgram  = false;
        flash.programLost  = false;
        flash.stuckAddress = -1;
        // The record is not written, torn, or (when only its read-back
        // failed) whole: the next call reads the sectors and finds which.
        const std::uint32_t newest = fault == Fault::ReadBack ? 2U : 1U;
        CHECK( loaded() == blobFor( newest, 20 ) );
        CHECK( flash_log::head( kCal ).sequence == newest && flash.zeroToOne == 0 );
        REQUIRE( save( blobFor( 3, 20 ) ) == flash_log::Status::Ok );
        CHECK( loaded() == blobFor( 3, 20 ) );
        const bool torn = fault == Fault::StuckBlob || fault == Fault::StuckHeader;
        CHECK( flash_log::head( kCal ).sector == ( torn ? 1U : 0U ) );
    }
}

TEST( "flash_log", "every failure of a move to the other sector answers FlashError" ) {
    enum class Fault : std::uint8_t {
        Erase,      ///< The erase reports an error.
        EraseLost,  ///< The erase reports success and erases nothing.
        RoomRead,   ///< The read that checks the erase.
    };
    for ( const Fault fault : { Fault::Erase, Fault::EraseLost, Fault::RoomRead } ) {
        Scope scope;
        fresh( scope );
        // Sector 1 holds an old record, so an erase that does nothing shows;
        // sector 0 the newer ones, to its last byte.
        scope->flash = sim::Flash{};
        plantRecord( scope->flash, 1, 0, 1, blobFor( 99, 4 ) );
        plantRecord( scope->flash, 0, 0, 2, blobFor( 2, 112 ) );
        REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
        const std::uint32_t filled = fillFirstSector( 112, 2 );
        sim::Flash&         flash  = scope->flash;
        REQUIRE( filled == 33U && flash_log::head( kCal ).offset == 31U * 128U );
        switch ( fault ) {
            case Fault::Erase:
                flash.failErase = true;
                break;
            case Fault::EraseLost:
                flash.eraseLost = true;
                break;
            case Fault::RoomRead:
                flash.readsUntilFailure = 0;
                break;
        }
        // The sector cannot take another record of 128 bytes: no room is read
        // before the erase.
        CHECK( save( blobFor( filled + 1U, 112 ) ) == flash_log::Status::FlashError );
        CHECK( flash.zeroToOne == 0 && flash.programs == filled - 2U );
        flash.failErase = false;
        flash.eraseLost = false;
        CHECK( loaded() == blobFor( filled, 112 ) );
        REQUIRE( save( blobFor( filled + 1U, 112 ) ) == flash_log::Status::Ok );
        CHECK( flash_log::head( kCal ).sector == 1U
               && flash_log::head( kCal ).sequence == filled + 1U );
    }
}

TEST( "flash_log", "a load refuses a record that changed under it and then reads again" ) {
    enum class Change : std::uint8_t {
        Corrupted,      ///< A blob byte lost.
        OtherSequence,  ///< Another valid record in its place.
        OtherLength,    ///< The same sequence with another length.
        ReadFails,      ///< The read fails.
    };
    for ( const Change change :
          { Change::Corrupted, Change::OtherSequence, Change::OtherLength, Change::ReadFails } ) {
        Scope scope;
        fresh( scope );
        REQUIRE( save( blobFor( 1, 8 ) ) == flash_log::Status::Ok );
        REQUIRE( save( blobFor( 2, 8 ) ) == flash_log::Status::Ok );
        sim::Flash& flash = scope->flash;
        const auto  wipe  = [&]() {
            for ( std::uint32_t i = 24; i < 24U + 32U; ++i ) {
                flash.at( flash.base + i ) = 0xFF;
            }
        };
        Bytes after = blobFor( 1, 8 );
        switch ( change ) {
            case Change::Corrupted:
                flash.at( flash.base + 24U + 16U ) ^= 0x01U;
                break;
            case Change::OtherSequence:
                wipe();
                plantRecord( flash, 0, 24, 7, blobFor( 7, 8 ) );
                after = blobFor( 7, 8 );
                break;
            case Change::OtherLength:
                wipe();
                plantRecord( flash, 0, 24, 2, blobFor( 2, 12 ) );
                after = blobFor( 2, 12 );
                break;
            case Change::ReadFails:
                flash.readsUntilFailure = 0;
                after                   = blobFor( 2, 8 );
                break;
        }
        std::array<std::uint8_t, 16> out    = {};
        std::size_t                  length = 0;
        CHECK( flash_log::load( kCal, out.data(), out.size(), length )
               == flash_log::Status::FlashError );
        CHECK( !flash_log::head( kCal ).scanned );
        CHECK( loaded() == after );
    }
}

// --- Power cuts --------------------------------------------------------------------

namespace {

/// The length of save @p n of the power-cut run: a mix that moves the log
/// between the sectors several times in 220 saves.
std::size_t runLength( std::uint32_t n ) {
    constexpr std::array<std::size_t, 6> kLengths = { 80, 1, 128, 37, 64, 3 };
    return kLengths[n % kLengths.size()];
}

/// The saves of the power-cut run.
constexpr std::uint32_t kRunSaves = 220;

/// What one change of the run is.
struct Change {
    std::uint32_t save    = 0;      ///< The save it belongs to (1-based).
    bool          erase   = false;  ///< An erase, not a write.
    std::size_t   written = 0;      ///< A write's bytes.
    std::uint32_t address = 0;      ///< Where a write went.
    Bytes         bytes;            ///< What it wrote.
};

/// Every change the uncut run makes, in order.
std::vector<Change> runChanges( Scope& scope ) {
    fresh( scope );
    std::vector<Change> changes;
    for ( std::uint32_t n = 1; n <= kRunSaves; ++n ) {
        const std::size_t erases = scope->flash.erases[0] + scope->flash.erases[1];
        REQUIRE( save( blobFor( n, runLength( n ) ) ) == flash_log::Status::Ok );
        if ( scope->flash.erases[0] + scope->flash.erases[1] != erases ) {
            changes.push_back( { n, true, 0, 0, {} } );
        }
        const flash_log::Head head    = flash_log::head( kCal );
        const std::size_t     written = 16U + runLength( n );
        const std::uint32_t   address = addressOf( scope->flash, head.sector, head.offset );
        Bytes                 bytes( written );
        for ( std::size_t i = 0; i < written; ++i ) {
            bytes[i] = scope->flash.at( address + static_cast<std::uint32_t>( i ) );
        }
        changes.push_back( { n, false, written, address, bytes } );
    }
    REQUIRE( changes.size() == scope->flash.changes );
    return changes;
}

/// Runs the saves with the power cut at change @p at, keeping @p keep bytes
/// of it (torn as @p tears says); then powers on, mounts, and checks that the
/// last complete record is the newest, that the log takes a save after, and
/// that nothing was ever written over a 0.
void cutRun( Scope& scope, const std::vector<Change>& changes, std::size_t at, std::size_t keep,
             bool tears ) {
    scope->flash = sim::Flash{};
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    scope->flash.cutAt    = static_cast<long long>( at );
    scope->flash.cutKeep  = keep;
    scope->flash.cutTears = tears;
    const Change& cut     = changes[at];
    for ( std::uint32_t n = 1; n < cut.save; ++n ) {
        REQUIRE( save( blobFor( n, runLength( n ) ) ) == flash_log::Status::Ok );
    }
    CHECK( save( blobFor( cut.save, runLength( cut.save ) ) ) != flash_log::Status::Ok );
    CHECK( !scope->flash.powered );
    scope->flash.powerOn();
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    // A write that landed whole is the new record; anything less leaves the
    // one before it. Whole is what the flash holds, not what was kept: a
    // byte cut off may already have read as its value.
    bool whole = !cut.erase;
    for ( std::size_t i = 0; whole && i < cut.written; ++i ) {
        whole = scope->flash.at( cut.address + static_cast<std::uint32_t>( i ) ) == cut.bytes[i];
    }
    const std::uint32_t newest = whole ? cut.save : cut.save - 1U;
    CHECK( flash_log::head( kCal ).sequence == newest );
    if ( newest == 0U ) {
        CHECK( !flash_log::head( kCal ).hasRecord );
    } else {
        CHECK( loaded() == blobFor( newest, runLength( newest ) ) );
    }
    REQUIRE( save( blobFor( 1000U + cut.save, 64 ) ) == flash_log::Status::Ok );
    REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr ) == flash_log::Status::Ok );
    CHECK( flash_log::head( kCal ).sequence == newest + 1U );
    CHECK( loaded() == blobFor( 1000U + cut.save, 64 ) );
    CHECK( scope->flash.zeroToOne == 0 );
}

}  // namespace

TEST( "flash_log", "a power cut after any write or erase leaves the last complete record" ) {
    Scope                     scope;
    const std::vector<Change> changes = runChanges( scope );
    std::size_t               erases  = 0;
    for ( const Change& change : changes ) {
        erases += change.erase ? 1U : 0U;
    }
    // The run moves between the sectors more than twice.
    REQUIRE( erases >= 3U );
    for ( std::size_t at = 0; at < changes.size(); ++at ) {
        const Change& change = changes[at];
        if ( change.erase ) {
            for ( const std::size_t keep : { 0U, 1U, 15U, 16U, 17U, 2048U, 4095U, 4096U } ) {
                cutRun( scope, changes, at, keep, false );
                cutRun( scope, changes, at, keep, true );
            }
        } else {
            const std::size_t n = change.written;
            for ( const std::size_t keep :
                  { std::size_t{ 0 }, std::size_t{ 1 }, std::size_t{ 4 }, std::size_t{ 11 },
                    std::size_t{ 12 }, std::size_t{ 15 }, std::size_t{ 16 }, n / 2U, n - 1U, n } ) {
                cutRun( scope, changes, at, keep, false );
                cutRun( scope, changes, at, keep, true );
            }
        }
    }
}

// --- Wear ----------------------------------------------------------------------------

TEST( "flash_log", "10,000 saves keep both sectors consistent and wear them evenly" ) {
    Scope scope;
    fresh( scope );
    std::uint32_t seed     = 12345;
    std::uint32_t switches = 0;
    for ( std::uint32_t n = 1; n <= 10000U; ++n ) {
        seed                         = ( seed * 1103515245U ) + 12345U;
        const std::size_t     size   = 1U + ( ( seed >> 16U ) % flash_log::kMaxBlobBytes );
        const flash_log::Head before = flash_log::head( kCal );
        const std::size_t     erases = scope->flash.erases[0] + scope->flash.erases[1];
        REQUIRE( save( blobFor( n, size ) ) == flash_log::Status::Ok );
        const flash_log::Head after = flash_log::head( kCal );
        REQUIRE( after.sequence == n && loaded() == blobFor( n, size ) );
        // An erase only when the record could not follow the last one, and
        // never of the sector that held the newest record.
        const bool moved = after.sector != before.sector || ( n > 1U && after.offset == 0U );
        const bool fits  = n == 1U
                           || before.offset + flash_log::recordBytes( before.length )
                                      + flash_log::recordBytes( static_cast<std::uint32_t>( size ) )
                                  <= kSector;
        REQUIRE( moved == !fits );
        REQUIRE( ( scope->flash.erases[0] + scope->flash.erases[1] - erases )
                 == ( moved ? 1U : 0U ) );
        switches += moved ? 1U : 0U;
        // Both sectors: records in rising order from the start and nothing
        // but erased bytes after them; every record of the other sector older
        // than the current sector's first.
        if ( n % 50U == 0U || moved ) {
            const Parsed current = parse( scope->flash, after.sector );
            const Parsed other   = parse( scope->flash, 1U - after.sector );
            REQUIRE( current.clean && other.clean );
            REQUIRE( !current.records.empty() && current.records.back().sequence == n );
            for ( std::size_t i = 1; i < current.records.size(); ++i ) {
                REQUIRE( current.records[i].sequence == current.records[i - 1U].sequence + 1U );
            }
            for ( std::size_t i = 1; i < other.records.size(); ++i ) {
                REQUIRE( other.records[i].sequence == other.records[i - 1U].sequence + 1U );
            }
            if ( !other.records.empty() ) {
                REQUIRE( other.records.back().sequence + 1U == current.records.front().sequence );
            }
        }
        if ( n % 997U == 0U ) {
            REQUIRE( flash_log::mount( kCal, scope->flash.base, nullptr )
                     == flash_log::Status::Ok );
            REQUIRE( flash_log::head( kCal ).sequence == n
                     && flash_log::head( kCal ).sector == after.sector );
        }
    }
    CHECK( scope->flash.programs == 10000U && scope->flash.zeroToOne == 0 );
    CHECK( switches == scope->flash.erases[0] + scope->flash.erases[1] );
    CHECK( switches > 150U );
    const std::size_t a = scope->flash.erases[0];
    const std::size_t b = scope->flash.erases[1];
    CHECK( ( a > b ? a - b : b - a ) <= 1U );
}

// --- Two logs (FW-03) ----------------------------------------------------------------

namespace {

/// The pin map's log, the second.
constexpr flash_log::Log kPins = flash_log::Log::Pins;

/// A log and the model's first sector of it: the calibration log's are the
/// window's sectors 0 and 1, the pin map log's 2 and 3.
struct Place {
    flash_log::Log log;
    std::uint32_t  base;
    std::uint32_t  firstSector;
};

/// Both logs where the image puts them.
constexpr std::array<Place, 2> kPlaces = { {
    { kCal, flash_port::kLogBaseAddress, 0 },
    { kPins, flash_port::kPinLogBaseAddress, 2 },
} };

/// Saves @p blob to @p log.
flash_log::Status saveTo( flash_log::Log log, const Bytes& blob ) {
    return flash_log::save( log, blob.data(), blob.size() );
}

/// The newest blob of @p log, or an empty one when load() does not answer Ok.
Bytes loadedFrom( flash_log::Log log ) {
    std::array<std::uint8_t, flash_log::kMaxBlobBytes> out    = {};
    std::size_t                                        length = 0;
    if ( flash_log::load( log, out.data(), out.size(), length ) != flash_log::Status::Ok ) {
        return {};
    }
    return { out.begin(), out.begin() + static_cast<std::ptrdiff_t>( length ) };
}

/// The model's bytes of the two sectors from @p place's base.
Bytes regionOf( const sim::Flash& flash, const Place& place ) {
    const auto first =
        flash.bytes.begin() + static_cast<std::ptrdiff_t>( place.firstSector * kSector );
    return { first, first + static_cast<std::ptrdiff_t>( 2U * kSector ) };
}

/// The length of save @p n of a log's own run: four lengths, one of them
/// padded, that move the log between its sectors three times in kOwnSaves
/// saves.
std::size_t ownLength( std::uint32_t n ) {
    constexpr std::array<std::size_t, 4> kLengths = { 128, 99, 128, 128 };
    return kLengths[n % kLengths.size()];
}

/// The saves of a log's own run.
constexpr std::uint32_t kOwnSaves = 95;

/// The blob of save @p n of @p log's run, apart from the other log's.
Bytes ownBlob( const Place& place, std::uint32_t n ) {
    return blobFor( n + ( place.firstSector * 1000U ), ownLength( n ) );
}

/// One change a save of the uncut run made, and the flash just before that
/// save, which a cut starts from.
struct OwnChange {
    std::uint32_t save    = 0;      ///< The save (1-based).
    std::size_t   index   = 0;      ///< Its change within the save, 0 or 1.
    bool          erase   = false;  ///< An erase, not a write.
    std::size_t   written = 0;      ///< A write's bytes.
    std::uint32_t address = 0;      ///< Where a write went.
    Bytes         bytes;            ///< What it wrote.
};

/// The other log's state after its records were written.
struct Kept {
    flash_log::Head head;
    Bytes           blob;
    Bytes           region;
};

/// Fills the log of @p other with records in both its sectors, and says
/// what it holds.
Kept fillOther( Scope& scope, const Place& other ) {
    REQUIRE( flash_log::mount( other.log, other.base, nullptr ) == flash_log::Status::Ok );
    // 28 records of 144 bytes fill a sector: the 29th and 30th go to the
    // second.
    for ( std::uint32_t n = 1; n <= 30U; ++n ) {
        REQUIRE( saveTo( other.log, blobFor( 5000U + n, 128 ) ) == flash_log::Status::Ok );
    }
    REQUIRE( flash_log::head( other.log ).sector == 1U );
    return { flash_log::head( other.log ), loadedFrom( other.log ),
             regionOf( scope->flash, other ) };
}

/// Checks that @p other's sectors hold what @p kept says, byte for byte,
/// and with @p remount that a mount finds the same newest record in them.
void otherUnchanged( Scope& scope, const Place& other, const Kept& kept, bool remount = true ) {
    const auto first =
        scope->flash.bytes.begin() + static_cast<std::ptrdiff_t>( other.firstSector * kSector );
    REQUIRE( std::equal( kept.region.begin(), kept.region.end(), first ) );
    if ( !remount ) {
        return;
    }
    REQUIRE( flash_log::mount( other.log, other.base, nullptr ) == flash_log::Status::Ok );
    const flash_log::Head head = flash_log::head( other.log );
    REQUIRE( head.sequence == kept.head.sequence && head.sector == kept.head.sector
             && head.offset == kept.head.offset && head.length == kept.head.length );
    REQUIRE( loadedFrom( other.log ) == kept.blob );
}

}  // namespace

TEST( "flash_log", "each log keeps its own state, buffer and Acceptor" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    REQUIRE( flash_log::mount( kCal, flash_port::kLogBaseAddress, nullptr )
             == flash_log::Status::Ok );
    REQUIRE( flash_log::mount( kPins, flash_port::kPinLogBaseAddress, &judge )
             == flash_log::Status::Ok );
    // The pin log's Acceptor refuses; the calibration log, mounted with none,
    // takes the same blob.
    CHECK( saveTo( kPins, refusedBlob( 8 ) ) == flash_log::Status::Refused );
    CHECK( saveTo( kCal, refusedBlob( 8 ) ) == flash_log::Status::Ok );
    REQUIRE( saveTo( kPins, blobFor( 1, 8 ) ) == flash_log::Status::Ok );
    REQUIRE( saveTo( kPins, blobFor( 2, 12 ) ) == flash_log::Status::Ok );
    // Each its own sequence, place and newest blob.
    CHECK( flash_log::head( kCal ).sequence == 1U && flash_log::head( kCal ).length == 8U );
    CHECK( flash_log::head( kPins ).sequence == 2U && flash_log::head( kPins ).offset == 24U );
    CHECK( loadedFrom( kCal ) == refusedBlob( 8 ) );
    CHECK( loadedFrom( kPins ) == blobFor( 2, 12 ) );
    CHECK( scope->flash.at( flash_port::kPinLogBaseAddress + 16U ) == blobFor( 1, 8 )[0] );
    // A mount of one forgets nothing of the other.
    REQUIRE( flash_log::mount( kCal, flash_port::kLogBaseAddress, nullptr )
             == flash_log::Status::Ok );
    CHECK( flash_log::head( kPins ).sequence == 2U && flash_log::head( kPins ).scanned );
    CHECK( saveTo( kPins, refusedBlob( 4 ) ) == flash_log::Status::Refused );
    // A failure of one leaves the other read.
    scope->flash.failProgram = true;
    CHECK( saveTo( kPins, blobFor( 3, 8 ) ) == flash_log::Status::FlashError );
    scope->flash.failProgram = false;
    CHECK( !flash_log::head( kPins ).scanned && flash_log::head( kCal ).scanned );
    CHECK( scope->flash.refusedByRegion == 0 && scope->flash.zeroToOne == 0 );
}

TEST( "flash_log", "saves to both logs in turn keep each one's sectors its own" ) {
    Scope scope;
    scope->flash = sim::Flash{};
    for ( const Place& place : kPlaces ) {
        REQUIRE( flash_log::mount( place.log, place.base, nullptr ) == flash_log::Status::Ok );
    }
    std::uint32_t                seed  = 777;
    std::array<std::uint32_t, 2> saves = {};
    for ( std::uint32_t n = 1; n <= 3000U; ++n ) {
        seed                     = ( seed * 1103515245U ) + 12345U;
        const std::size_t which  = ( seed >> 20U ) % 2U;
        const Place&      place  = kPlaces[which];
        const std::size_t length = 1U + ( ( seed >> 8U ) % flash_log::kMaxBlobBytes );
        ++saves[which];
        REQUIRE( saveTo( place.log,
                         blobFor( saves[which] + ( 1000000U * static_cast<std::uint32_t>( which ) ),
                                  length ) )
                 == flash_log::Status::Ok );
        if ( n % 250U == 0U ) {
            for ( std::size_t i = 0; i < kPlaces.size(); ++i ) {
                REQUIRE( flash_log::mount( kPlaces[i].log, kPlaces[i].base, nullptr )
                         == flash_log::Status::Ok );
                REQUIRE( flash_log::head( kPlaces[i].log ).sequence == saves[i] );
                const Parsed current =
                    parse( scope->flash,
                           kPlaces[i].firstSector + flash_log::head( kPlaces[i].log ).sector );
                REQUIRE( current.clean && !current.records.empty() );
                REQUIRE( current.records.back().sequence == saves[i] );
            }
        }
    }
    CHECK( saves[0] > 1000U && saves[1] > 1000U );
    CHECK( scope->flash.erases[0] + scope->flash.erases[1] > 10U );
    CHECK( scope->flash.erases[2] + scope->flash.erases[3] > 10U );
    CHECK( scope->flash.refusedByRegion == 0 && scope->flash.outOfWindow == 0
           && scope->flash.zeroToOne == 0 );
}

TEST( "flash_log",
      "a power cut after every byte of every write and erase of either log spares the other" ) {
    for ( const Place& place : kPlaces ) {
        const Place& other = kPlaces[place.firstSector == 0U ? 1U : 0U];
        Scope        scope;
        scope->flash    = sim::Flash{};
        const Kept kept = fillOther( scope, other );
        // The uncut run: the flash before each save, and every change.
        REQUIRE( flash_log::mount( place.log, place.base, nullptr ) == flash_log::Status::Ok );
        std::vector<sim::Flash> before;
        std::vector<OwnChange>  changes;
        std::size_t             moves = 0;
        for ( std::uint32_t n = 1; n <= kOwnSaves; ++n ) {
            before.push_back( scope->flash );
            const std::size_t erased = scope->flash.erases[place.firstSector]
                                       + scope->flash.erases[place.firstSector + 1U];
            REQUIRE( saveTo( place.log, ownBlob( place, n ) ) == flash_log::Status::Ok );
            std::size_t index = 0;
            if ( scope->flash.erases[place.firstSector]
                     + scope->flash.erases[place.firstSector + 1U]
                 != erased ) {
                changes.push_back( { n, index++, true, 0, 0, {} } );
                ++moves;
            }
            const flash_log::Head head    = flash_log::head( place.log );
            const std::size_t     written = 16U + ownLength( n );
            const std::uint32_t   address = place.base + ( head.sector * kSector ) + head.offset;
            Bytes                 bytes( written );
            for ( std::size_t i = 0; i < written; ++i ) {
                bytes[i] = scope->flash.at( address + static_cast<std::uint32_t>( i ) );
            }
            changes.push_back( { n, index, false, written, address, bytes } );
        }
        REQUIRE( moves >= 3U );
        otherUnchanged( scope, other, kept );
        // Every byte of every change, cut there, torn and not.
        std::size_t cuts = 0;
        for ( const OwnChange& change : changes ) {
            const std::size_t last = change.erase ? kSector : change.written;
            for ( std::size_t keep = 0; keep <= last; ++keep ) {
                for ( const bool tears : { false, true } ) {
                    const sim::Flash& start = before[change.save - 1U];
                    scope->flash            = start;
                    REQUIRE( flash_log::mount( place.log, place.base, nullptr )
                             == flash_log::Status::Ok );
                    scope->flash.cutAt    = static_cast<long long>( start.changes + change.index );
                    scope->flash.cutKeep  = keep;
                    scope->flash.cutTears = tears;
                    REQUIRE( saveTo( place.log, ownBlob( place, change.save ) )
                             != flash_log::Status::Ok );
                    REQUIRE( !scope->flash.powered );
                    REQUIRE( scope->flash.changes == start.changes + change.index + 1U );
                    scope->flash.powerOn();
                    REQUIRE( flash_log::mount( place.log, place.base, nullptr )
                             == flash_log::Status::Ok );
                    bool whole = !change.erase;
                    for ( std::size_t i = 0; whole && i < change.written; ++i ) {
                        whole = scope->flash.at( change.address + static_cast<std::uint32_t>( i ) )
                                == change.bytes[i];
                    }
                    const std::uint32_t newest = whole ? change.save : change.save - 1U;
                    REQUIRE( flash_log::head( place.log ).sequence == newest );
                    if ( newest == 0U ) {
                        REQUIRE( !flash_log::head( place.log ).hasRecord );
                    } else {
                        REQUIRE( loadedFrom( place.log ) == ownBlob( place, newest ) );
                    }
                    // The bytes after every cut; a mount of them at each
                    // change's first and last byte (the same bytes mount
                    // the same way).
                    otherUnchanged( scope, other, kept, keep == 0U || keep == last );
                    REQUIRE( saveTo( place.log, blobFor( 9000U, 64 ) ) == flash_log::Status::Ok );
                    REQUIRE( flash_log::mount( place.log, place.base, nullptr )
                             == flash_log::Status::Ok );
                    REQUIRE( flash_log::head( place.log ).sequence == newest + 1U );
                    REQUIRE( loadedFrom( place.log ) == blobFor( 9000U, 64 ) );
                    REQUIRE( scope->flash.zeroToOne == 0 && scope->flash.refusedByRegion == 0 );
                    ++cuts;
                }
            }
        }
        CHECK( cuts > 20000U );
    }
}
