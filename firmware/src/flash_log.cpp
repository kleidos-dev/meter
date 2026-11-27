// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file flash_log.cpp
 * @ingroup flash_log
 * @brief The record logs: the scan of both sectors, the append or the
 *        switch to the other sector, the read-back, and the load.
 *
 * Nothing is allocated (FW-15): each log's state and its one buffer are a
 * static object in internal RAM (FW-03: one per Log), every flash access
 * goes through the three functions of flash_log::port, and the copy a save
 * checks its read-back against is on the caller's stack.
 */
#include "flash_log.h"

#include "crc32.h"

#include <algorithm>
#include <array>

namespace kleidos::meter::flash_log {

namespace {

/// The value of an erased byte.
constexpr std::uint32_t kErased = 0xFFU;

/// Where each header word sits in a record.
constexpr std::size_t kMagicAt    = 0;   ///< The magic.
constexpr std::size_t kSequenceAt = 4;   ///< The sequence.
constexpr std::size_t kLengthAt   = 8;   ///< The blob's length.
constexpr std::size_t kCrcAt      = 12;  ///< The CRC.

/// The header bytes the CRC covers before the blob: the magic, the sequence
/// and the length.
constexpr std::size_t kCrcCoveredHeaderBytes = kCrcAt;

/// The smallest record: a header and one byte of blob, padded.
constexpr std::uint32_t kMinRecordBytes = recordBytes( 1 );

/// The bits of one byte, for the shifts that store a word.
constexpr std::uint32_t kByteBits = 8;

/// A byte's mask.
constexpr std::uint32_t kByteMask = 0xFFU;

/// What one sector holds, as the scan found it.
struct SectorScan {
    std::uint32_t end             = 0;      ///< Where the records the scan read end.
    bool          hasRecord       = false;  ///< A record held.
    std::uint32_t sequence        = 0;      ///< The highest sequence among them.
    std::uint32_t offset          = 0;      ///< That record's offset.
    std::uint32_t length          = 0;      ///< Its blob's length.
    bool          refused         = false;  ///< The scan ended at a record the Acceptor refused.
    std::uint32_t refusedSequence = 0;      ///< That record's sequence (0 with none).
};

/// One log's state and its buffer, in an object reached through a function
/// so that no variable has namespace scope (docs/misra.md, rule 6.7.2). Its
/// fields are the newest record's place, as Head has them, and where its
/// sector's records end.
struct State {
    std::uint32_t base      = 0;        ///< The first sector's flash address.
    bool          mounted   = false;    ///< mount() took @c base.
    bool          scanned   = false;    ///< The fields below are the flash's.
    bool          hasRecord = false;    ///< A valid record is the newest.
    std::uint32_t sequence  = 0;        ///< Its sequence.
    std::uint32_t sector    = 0;        ///< Its sector (0 with none: the next save's).
    std::uint32_t offset    = 0;        ///< Its offset in the sector.
    std::uint32_t length    = 0;        ///< Its blob's length.
    std::uint32_t end       = 0;        ///< Where that sector's records end.
    Acceptor      accept    = nullptr;  ///< The mount's judgment of a blob, or none.
    std::uint32_t refused   = 0;        ///< Records the last scan's Acceptor refused.
    /// The highest sequence among them (0 with none): the next save's
    /// record is numbered above it too, so that it is newer than anything
    /// the sectors hold.
    std::uint32_t refusedSequence = 0;
    /// Every byte read or written passes through it: internal RAM, which
    /// ESP-IDF's flash read fills directly (no `EXT_RAM_BSS_ATTR`).
    std::array<std::uint8_t, kMaxRecordBytes> buffer = {};
};

/// @brief The State of one log: each Log has its own.
/// @param[in] log Which log.
/// @return That log's state.
State& state( Log log ) noexcept {
    static constinit std::array<State, kLogCount> instance;
    return instance[static_cast<std::size_t>( log )];
}

/// What one place of a sector holds.
enum class Probe : std::uint8_t {
    Valid,    ///< A record that holds and the Acceptor takes; the buffer has it.
    Refused,  ///< A record that holds and the Acceptor refuses.
    Invalid,  ///< Anything else, an erased header included.
    Failed,   ///< A read failed.
};

/// @brief A stored word of the buffer.
/// @param[in] own The state.
/// @param[in] at  The word's first byte.
/// @return The four bytes from @p at, least significant first.
constexpr std::uint32_t wordAt( const State& own, std::size_t at ) noexcept {
    std::uint32_t value = 0;
    for ( std::size_t i = 4; i > 0U; --i ) {
        value = ( value << kByteBits ) | static_cast<std::uint32_t>( own.buffer[at + i - 1U] );
    }
    return value;
}

/// @brief Stores @p value in the buffer, least significant byte first.
/// @param[in,out] own   The state.
/// @param[in]     at    The word's first byte.
/// @param[in]     value The word.
void putWord( State& own, std::size_t at, std::uint32_t value ) noexcept {
    for ( std::size_t i = 0; i < 4U; ++i ) {
        own.buffer[at + i] =
            static_cast<std::uint8_t>( ( value >> ( kByteBits * i ) ) & kByteMask );
    }
}

/// @brief The CRC of the record in the buffer: its magic, sequence and length
///        words, then @p length bytes of blob.
/// @param[in] own    The state.
/// @param[in] length The blob's length, at most kMaxBlobBytes.
/// @return The CRC-32.
std::uint32_t recordCrc( const State& own, std::size_t length ) noexcept {
    const std::uint32_t head =
        crc32::update( crc32::kInitial, own.buffer.data(), kCrcCoveredHeaderBytes );
    return crc32::update( head, &own.buffer[kHeaderBytes], length ) ^ crc32::kInitial;
}

/// @brief Whether the first @p length bytes of the buffer read erased.
/// @param[in] own    The state.
/// @param[in] length How many, at most kMaxRecordBytes.
/// @return true if every one is 0xFF.
bool bufferErased( const State& own, std::size_t length ) noexcept {
    return std::all_of( own.buffer.cbegin(),
                        own.buffer.cbegin() + static_cast<std::ptrdiff_t>( length ),
                        []( std::uint8_t byte ) noexcept { return byte == kErased; } );
}

/// @brief The flash address of @p offset in @p sector.
/// @param[in] own    The state.
/// @param[in] sector 0 or 1.
/// @param[in] offset The offset in the sector.
/// @return The absolute address.
std::uint32_t addressOf( const State& own, std::uint32_t sector, std::uint32_t offset ) noexcept {
    return own.base + ( sector * kSectorBytes ) + offset;
}

/// @brief Whether the mount's Acceptor takes @p blob: always, with none.
/// @param[in] own    The state.
/// @param[in] blob   The blob.
/// @param[in] length Its length, 1 to kMaxBlobBytes.
/// @return true to keep it.
bool taken( const State& own, const std::uint8_t* blob, std::size_t length ) noexcept {
    if ( own.accept == nullptr ) {
        return true;
    }
    return own.accept( blob, length );
}

/// @brief Reads what the place at @p offset of @p sector holds into the
///        buffer, and judges it.
/// @param[in,out] own    The state; its buffer takes the header, and the
///                       record when it is valid.
/// @param[in]     sector 0 or 1.
/// @param[in]     offset A 4-byte aligned offset with a header's room left.
/// @return What is there.
Probe probe( State& own, std::uint32_t sector, std::uint32_t offset ) noexcept {
    const std::uint32_t address = addressOf( own, sector, offset );
    if ( !port::read( address, own.buffer.data(), kHeaderBytes ) ) {
        return Probe::Failed;
    }
    const std::uint32_t length = wordAt( own, kLengthAt );
    // The length is checked against the largest blob before it is used, so
    // recordBytes() cannot overflow and the room check is exact.
    if ( wordAt( own, kMagicAt ) != kMagic || length == 0U || length > kMaxBlobBytes ) {
        return Probe::Invalid;
    }
    if ( recordBytes( length ) > kSectorBytes - offset ) {
        return Probe::Invalid;
    }
    if ( !port::read( address + static_cast<std::uint32_t>( kHeaderBytes ),
                      &own.buffer[kHeaderBytes], length ) ) {
        return Probe::Failed;
    }
    if ( recordCrc( own, length ) != wordAt( own, kCrcAt ) ) {
        return Probe::Invalid;
    }
    return taken( own, &own.buffer[kHeaderBytes], length ) ? Probe::Valid : Probe::Refused;
}

/// @brief Reads @p sector record after record, as the file comment of
///        flash_log.h describes.
/// @param[in,out] own    The state (its buffer is used).
/// @param[in]     sector 0 or 1.
/// @param[out]    found  What the sector holds.
/// @return false if a read failed.
bool scanSector( State& own, std::uint32_t sector, SectorScan& found ) noexcept {
    found        = SectorScan{};
    bool reading = true;
    while ( reading && found.end + kMinRecordBytes <= kSectorBytes ) {
        const Probe what = probe( own, sector, found.end );
        if ( what == Probe::Failed ) {
            return false;
        }
        if ( what == Probe::Valid ) {
            const std::uint32_t sequence = wordAt( own, kSequenceAt );
            const std::uint32_t length   = wordAt( own, kLengthAt );
            if ( !found.hasRecord || sequence > found.sequence ) {
                found.hasRecord = true;
                found.sequence  = sequence;
                found.offset    = found.end;
                found.length    = length;
            }
            found.end += recordBytes( length );
        } else {
            // An erased header ends the records (its magic reads 0xFFFFFFFF),
            // and anything else ends what the scan trusts, a refused record
            // as a bad CRC does. A save reads the bytes from here erased
            // before it appends, which a header that is not erased never
            // does, so a sector that holds something invalid takes nothing
            // more.
            if ( what == Probe::Refused ) {
                found.refused         = true;
                found.refusedSequence = wordAt( own, kSequenceAt );
            }
            reading = false;
        }
    }
    return true;
}

/// @brief Reads both sectors and makes the newest valid record the log's.
/// @param[in,out] own The state.
/// @return false if a read failed; the state is left unread.
bool scan( State& own ) noexcept {
    std::array<SectorScan, kSectorCount> sectors = {};
    own.scanned                                  = false;
    for ( std::uint32_t sector = 0; sector < kSectorCount; ++sector ) {
        if ( !scanSector( own, sector, sectors[sector] ) ) {
            return false;
        }
    }
    // Sector 1 holds the newest record only if it holds one with a higher
    // sequence than sector 0's, or sector 0 holds none. With no record at
    // all, sector 0 is the one the first save tries.
    const SectorScan&   first  = sectors[0];
    const SectorScan&   second = sectors[1];
    const std::uint32_t newest =
        ( second.hasRecord && ( !first.hasRecord || second.sequence > first.sequence ) ) ? 1U : 0U;
    const SectorScan& chosen = sectors[newest];
    own.hasRecord            = chosen.hasRecord;
    own.sequence             = chosen.sequence;
    own.sector               = newest;
    own.offset               = chosen.offset;
    own.length               = chosen.length;
    own.end                  = chosen.end;
    own.refused              = ( first.refused ? 1U : 0U ) + ( second.refused ? 1U : 0U );
    own.refusedSequence      = std::max( first.refusedSequence, second.refusedSequence );
    own.scanned              = true;
    return true;
}

/// @brief Reads the sectors when the state is not the flash's.
/// @param[in,out] own The state.
/// @return false if a read failed.
bool ensureScanned( State& own ) noexcept {
    if ( own.scanned ) {
        return true;
    }
    return scan( own );
}

/// What reading a range for erased bytes found.
enum class Room : std::uint8_t {
    Erased,  ///< Every byte reads 0xFF.
    Used,    ///< Some byte does not.
    Failed,  ///< The read failed.
};

/// @brief Whether @p length bytes at @p offset of @p sector read erased.
/// @param[in,out] own    The state (its buffer is used).
/// @param[in]     sector 0 or 1.
/// @param[in]     offset The first byte's offset in the sector.
/// @param[in]     length How many, at most kMaxRecordBytes.
/// @return What the range holds.
Room roomAt( State& own, std::uint32_t sector, std::uint32_t offset,
             std::uint32_t length ) noexcept {
    if ( !port::read( addressOf( own, sector, offset ), own.buffer.data(), length ) ) {
        return Room::Failed;
    }
    return bufferErased( own, length ) ? Room::Erased : Room::Used;
}

/// @brief Fails a save or a load: the state is no longer known to be the
///        flash's, and the next call reads the sectors again.
/// @param[in,out] own The state.
/// @return Status::FlashError.
Status lost( State& own ) noexcept {
    own.scanned = false;
    return Status::FlashError;
}

}  // namespace

Status mount( Log log, std::uint32_t baseAddress, Acceptor accept ) noexcept {
    State& own = state( log );
    own        = State{};
    if ( baseAddress % kSectorBytes != 0U || baseAddress > kLastBaseAddress ) {
        return Status::BadAddress;
    }
    own.base    = baseAddress;
    own.accept  = accept;
    own.mounted = true;
    return scan( own ) ? Status::Ok : Status::FlashError;
}

Status save( Log log, const std::uint8_t* blob, std::size_t length ) noexcept {
    State& own = state( log );
    if ( !own.mounted ) {
        return Status::NotMounted;
    }
    if ( blob == nullptr || length == 0U || length > kMaxBlobBytes ) {
        return Status::BadLength;
    }
    // What the log would refuse to give back is not written: it would hide
    // the record before it at the next mount, behind the newest in RAM.
    if ( !taken( own, blob, length ) ) {
        return Status::Refused;
    }
    if ( !ensureScanned( own ) ) {
        return Status::FlashError;
    }
    // With no record the sequence is 0, and the first record's is 1. A
    // refused record numbered higher is passed too, so that the new record
    // is the newest the sectors hold.
    const std::uint32_t last = std::max( own.sequence, own.refusedSequence );
    if ( last == kLastSequence ) {
        return Status::Exhausted;
    }
    const auto          size   = static_cast<std::uint32_t>( length );
    const std::uint32_t bytes  = recordBytes( size );
    std::uint32_t       sector = own.sector;
    std::uint32_t       offset = own.end;
    // The newest record's sector takes the record when it fits and its bytes
    // read erased: never after something the scan did not trust, whose
    // header reads otherwise.
    Room room = Room::Used;
    if ( bytes <= kSectorBytes - offset ) {
        room = roomAt( own, sector, offset, bytes );
    }
    if ( room == Room::Failed ) {
        return lost( own );
    }
    if ( room == Room::Used ) {
        // The other sector holds nothing newer than the newest record: erase
        // it, and check that it reads erased where the record goes.
        sector = kSectorCount - 1U - own.sector;
        offset = 0;
        if ( !port::eraseSector( addressOf( own, sector, 0 ) ) ) {
            return lost( own );
        }
        if ( roomAt( own, sector, 0, bytes ) != Room::Erased ) {
            return lost( own );
        }
    }
    const std::uint32_t sequence = last + 1U;
    putWord( own, kMagicAt, kMagic );
    putWord( own, kSequenceAt, sequence );
    putWord( own, kLengthAt, size );
    (void)std::copy_n( blob, length, &own.buffer[kHeaderBytes] );
    putWord( own, kCrcAt, recordCrc( own, length ) );
    // Only the header and the blob are written: the padding stays erased. A
    // copy of what is written is what the read-back must match, byte for
    // byte.
    const std::uint32_t                             address  = addressOf( own, sector, offset );
    const std::size_t                               written  = kHeaderBytes + length;
    const std::array<std::uint8_t, kMaxRecordBytes> expected = own.buffer;
    if ( !port::program( address, own.buffer.data(), written ) ) {
        return lost( own );
    }
    if ( !port::read( address, own.buffer.data(), written )
         || !std::equal( expected.cbegin(),
                         expected.cbegin() + static_cast<std::ptrdiff_t>( written ),
                         own.buffer.cbegin() ) ) {
        return lost( own );
    }
    own.hasRecord = true;
    own.sequence  = sequence;
    own.sector    = sector;
    own.offset    = offset;
    own.length    = size;
    own.end       = offset + bytes;
    return Status::Ok;
}

Status load( Log log, std::uint8_t* out, std::size_t capacity, std::size_t& length ) noexcept {
    State& own = state( log );
    if ( !own.mounted ) {
        return Status::NotMounted;
    }
    if ( !ensureScanned( own ) ) {
        return Status::FlashError;
    }
    if ( !own.hasRecord ) {
        return Status::Empty;
    }
    if ( out == nullptr || capacity < own.length ) {
        return Status::BadLength;
    }
    // The record is read again: the flash under it may have changed since
    // the scan, and only a record that still holds is given out.
    if ( probe( own, own.sector, own.offset ) != Probe::Valid
         || wordAt( own, kSequenceAt ) != own.sequence || wordAt( own, kLengthAt ) != own.length ) {
        return lost( own );
    }
    (void)std::copy_n( &own.buffer[kHeaderBytes], own.length, out );
    length = own.length;
    return Status::Ok;
}

Head head( Log log ) noexcept {
    const State& own = state( log );
    Head         now;
    now.mounted         = own.mounted;
    now.scanned         = own.scanned;
    now.hasRecord       = own.hasRecord;
    now.sequence        = own.sequence;
    now.sector          = own.sector;
    now.offset          = own.offset;
    now.length          = own.length;
    now.refused         = own.refused;
    now.refusedSequence = own.refusedSequence;
    return now;
}

}  // namespace kleidos::meter::flash_log
