// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file calibration.cpp
 * @ingroup calibration_store
 * @brief The calibration's record in the flash log, the store's judgment of
 *        a record, and the `q=` token.
 */
#include "calibration.h"

#include "bits.h"
#include "flash_log.h"
#include "flash_port.h"

#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <span>

namespace kleidos::meter {

namespace {

/// Stamped into the blob so a future layout change is detected rather than
/// misread as a valid calibration.
constexpr std::uint32_t kBlobMagic = 0x494E4132;  // "INA2"

/// The layout's version. 2 when `shuntMeasured` joined the blob (it occupies
/// padding the layout already had, so sizeof is unchanged and the length
/// check alone would have taken a v1 blob and read an indeterminate byte as
/// the flag), 3 when each range's own shunt and the calibration date joined
/// (firmware 0.4.0). The NVS blob of 0.4.0 to 0.16.6 was this layout; the
/// record log (FW-16) keeps it, and takes no other: nothing older than the
/// log can be in it.
constexpr std::uint32_t kBlobVersion = 3U;

/// The record's layout. Explicitly sized fields and an explicit version, so
/// that what is read back is what was written and nothing is inferred from a
/// compiler's padding choices.
struct StoredBlob {
    std::uint32_t magic;               ///< kBlobMagic.
    std::uint16_t version;             ///< kBlobVersion.
    std::uint8_t  vbusSide;            ///< VbusSide, as its value.
    std::uint8_t  nulledMask;          ///< bit 0: ADCRANGE 0 nulled, bit 1: ADCRANGE 1 nulled.
    std::uint32_t shuntMicroOhm;       ///< The common shunt, micro-ohms.
    std::uint8_t  shuntMeasured;       ///< 1 once a shunt measurement was written.
    std::uint8_t  reserved[3];         ///< Named, so the alignment padding is not implicit.
    std::int64_t  offsetPicovolts[2];  ///< Each range's null, picovolts.
    std::int32_t  offsetDieMilliCelsius[2];  ///< The die temperature of each null, m°C.
    std::uint32_t shuntRangeMicroOhm[2];     ///< 0: the range uses shuntMicroOhm.
    std::uint32_t date;                      ///< yyyymmdd, 0 for none.
    char          tag[kCalTagCapacity + 1];  ///< The calibration tag, terminated.
    std::uint8_t  tail[3];                   ///< Named, so the trailing padding is not implicit.
};
static_assert( sizeof( StoredBlob ) == 80U, "the v3 layout is 80 bytes with no implicit padding" );
static_assert( sizeof( StoredBlob ) <= flash_log::kMaxBlobBytes, "the blob fits one record" );

/// The bytes of one record's blob.
using RawBlob = std::array<std::uint8_t, sizeof( StoredBlob )>;

/// @brief A range's bit in StoredBlob::nulledMask.
/// @param[in] index The range index, 0 or 1.
/// @return The bit.
constexpr std::uint32_t rangeBit( std::size_t index ) noexcept {
    return static_cast<std::uint8_t>( 1U << index );
}

/// @brief Whether a range's own shunt is one the store takes: 0 (the range
///        uses the common value) or a value in the window.
/// @param[in] microOhm The range's stored value.
/// @return true to take it.
constexpr bool rangeShuntTaken( std::uint32_t microOhm ) noexcept {
    return microOhm == 0U || shuntInWindow( microOhm );
}

/// @brief The store's judgment of a record's blob (flash_log::Acceptor): the
///        current layout, and shunts this firmware could have measured.
///
/// Refused, and so passed over for the record before it: a blob of another
/// length, magic or version (another layout), and one whose common shunt is
/// outside the window or whose range shunt is neither 0 nor in it. Such a
/// value is never clamped into the window: a clamped shunt would convert
/// every reading with a resistance nobody measured, under `q=cal`.
/// @param[in] blob   The record's blob.
/// @param[in] length Its length.
/// @return true to take the record.
bool acceptBlob( const std::uint8_t* blob, std::size_t length ) noexcept {
    if ( length != sizeof( StoredBlob ) ) {
        return false;
    }
    RawBlob raw = {};
    (void)std::copy_n( blob, raw.size(), raw.begin() );
    const auto stored = std::bit_cast<StoredBlob>( raw );
    return stored.magic == kBlobMagic && stored.version == kBlobVersion
           && shuntInWindow( stored.shuntMicroOhm )
           && rangeShuntTaken( stored.shuntRangeMicroOhm[0] )
           && rangeShuntTaken( stored.shuntRangeMicroOhm[1] );
}

/// @brief The calibration a blob the store accepted holds.
/// @param[in] blob A blob acceptBlob() took.
/// @return Its calibration.
Calibration fromBlob( const StoredBlob& blob ) noexcept {
    Calibration value   = {};
    value.shuntMicroOhm = blob.shuntMicroOhm;
    value.shuntMeasured = blob.shuntMeasured != 0U;
    for ( std::size_t i = 0; i < 2U; ++i ) {
        value.offsetPicovolts[i]       = blob.offsetPicovolts[i];
        value.offsetDieMilliCelsius[i] = blob.offsetDieMilliCelsius[i];
        value.nulled[i]                = bits::hasBit( blob.nulledMask, rangeBit( i ) );
        value.shuntRangeMicroOhm[i]    = blob.shuntRangeMicroOhm[i];
    }
    value.vbusSide = ( blob.vbusSide == static_cast<std::uint32_t>( VbusSide::Source ) )
                         ? VbusSide::Source
                         : VbusSide::Unknown;
    value.date     = blob.date;
    static_assert( sizeof( blob.tag ) == sizeof( value.tag ), "the tag has one capacity" );
    const std::span<const char> tag( blob.tag );
    (void)std::copy_n( tag.begin(), tag.size(), std::span<char>( value.tag ).begin() );
    value.tag[kCalTagCapacity] = '\0';
    return value;
}

/// @brief The blob that stores @p value.
/// @param[in] value The calibration.
/// @return Its blob, every byte of padding zero.
StoredBlob toBlob( const Calibration& value ) noexcept {
    StoredBlob blob    = {};
    blob.magic         = kBlobMagic;
    blob.version       = kBlobVersion;
    blob.vbusSide      = static_cast<std::uint8_t>( value.vbusSide );
    blob.shuntMicroOhm = value.shuntMicroOhm;
    blob.shuntMeasured = value.shuntMeasured ? 1U : 0U;
    for ( std::size_t i = 0; i < 2U; ++i ) {
        blob.offsetPicovolts[i]       = value.offsetPicovolts[i];
        blob.offsetDieMilliCelsius[i] = value.offsetDieMilliCelsius[i];
        if ( value.nulled[i] ) {
            blob.nulledMask = static_cast<std::uint8_t>( blob.nulledMask | rangeBit( i ) );
        }
        blob.shuntRangeMicroOhm[i] = value.shuntRangeMicroOhm[i];
    }
    blob.date = value.date;
    const std::span<const char> tag( value.tag );
    (void)std::copy_n( tag.begin(), tag.size(), std::span<char>( blob.tag ).begin() );
    return blob;
}

}  // namespace

const char* qualityName( Quality quality ) noexcept {
    switch ( quality ) {
        case Quality::Uncal:
            return "uncal";
        case Quality::Stale:
            return "stale";
        case Quality::Cal:
            return "cal";
        case Quality::Invalid:
            return "invalid";
    }
    return "uncal";
}

bool CalibrationStore::mountLog() noexcept {
    return flash_log::mount( flash_log::Log::Calibration, flash_port::kLogBaseAddress, &acceptBlob )
           == flash_log::Status::Ok;
}

void CalibrationStore::ensureMounted() noexcept {
    // A mount that failed a read still mounts: the log reads the sectors
    // again at its next call, so the store needs no second mount.
    if ( !mounted_ ) {
        (void)mountLog();
        mounted_ = true;
    }
}

bool CalibrationStore::storedReadable() noexcept {
    mounted_ = true;
    if ( !mountLog() ) {
        return false;
    }
    // A record the store refuses that is newer than the one it takes is a
    // calibration stored and not given back; one older than it is history
    // the log has passed over.
    const flash_log::Head log = flash_log::head( flash_log::Log::Calibration );
    if ( log.refused != 0U && log.refusedSequence >= log.sequence ) {
        return false;
    }
    RawBlob                 raw    = {};
    std::size_t             length = 0;
    const flash_log::Status status =
        flash_log::load( flash_log::Log::Calibration, raw.data(), raw.size(), length );
    return status == flash_log::Status::Ok || status == flash_log::Status::Empty;
}

bool CalibrationStore::load() noexcept {
    mounted_ = true;
    if ( !mountLog() ) {
        return false;
    }
    RawBlob     raw    = {};
    std::size_t length = 0;
    if ( flash_log::load( flash_log::Log::Calibration, raw.data(), raw.size(), length )
         != flash_log::Status::Ok ) {
        return false;
    }
    // The log gave out only a blob acceptBlob() took: of the current layout's
    // length, magic and version.
    value_ = fromBlob( std::bit_cast<StoredBlob>( raw ) );
    return true;
}

bool CalibrationStore::save() noexcept {
    ensureMounted();
    const auto raw = std::bit_cast<RawBlob>( toBlob( value_ ) );
    return flash_log::save( flash_log::Log::Calibration, raw.data(), raw.size() )
           == flash_log::Status::Ok;
}

void CalibrationStore::setShuntMicroOhm( std::uint32_t microOhm ) noexcept {
    value_.shuntMicroOhm         = microOhm;
    value_.shuntRangeMicroOhm[0] = 0;
    value_.shuntRangeMicroOhm[1] = 0;
    value_.shuntMeasured         = true;
}

void CalibrationStore::setShuntRangeMicroOhm( ina228::ShuntRange range,
                                              std::uint32_t      microOhm ) noexcept {
    value_.shuntRangeMicroOhm[static_cast<std::size_t>( range )] = microOhm;
    value_.shuntMeasured                                         = true;
}

void CalibrationStore::setDate( std::uint32_t yyyymmdd ) noexcept {
    value_.date = yyyymmdd;
}

void CalibrationStore::setOffset( ina228::ShuntRange range, std::int64_t picovolts,
                                  std::int32_t dieMilliCelsius ) noexcept {
    const auto index                    = static_cast<std::size_t>( range );
    value_.offsetPicovolts[index]       = picovolts;
    value_.offsetDieMilliCelsius[index] = dieMilliCelsius;
    value_.nulled[index]                = true;
}

void CalibrationStore::setVbusSide( VbusSide side ) noexcept {
    value_.vbusSide = side;
}

void CalibrationStore::setTag( const char* tag ) noexcept {
    if ( tag == nullptr ) {
        value_.tag[0] = '\0';
        return;
    }
    std::size_t i = 0;
    while ( i < kCalTagCapacity && tag[i] != '\0' ) {
        value_.tag[i] = tag[i];
        ++i;
    }
    value_.tag[i] = '\0';
}

Quality CalibrationStore::quality( ina228::ShuntRange range, std::int32_t dieMilliCelsius,
                                   bool mathOverflow ) const noexcept {
    if ( mathOverflow ) {
        return Quality::Invalid;
    }
    if ( !nulled( range ) || !value_.shuntMeasured ) {
        return Quality::Uncal;
    }

    // Both temperatures have to be real. The stored one is
    // kDieMilliCelsiusUnknown (INT32_MIN) whenever the read at the end of
    // `CALibration:ZERO` failed, and `live - INT32_MIN` is signed overflow -- so the test
    // is skipped rather than answered from a sentinel, and the arithmetic that
    // does run is int64 so that neither the subtraction nor the negation can
    // overflow.
    const std::int32_t nullDieMilliCelsius = offsetDieMilliCelsius( range );
    if ( dieMilliCelsius != kDieMilliCelsiusUnknown
         && nullDieMilliCelsius != kDieMilliCelsiusUnknown ) {
        const std::int64_t drift  = static_cast<std::int64_t>( dieMilliCelsius )
                                    - static_cast<std::int64_t>( nullDieMilliCelsius );
        const std::int64_t spread = ( drift < 0 ) ? -drift : drift;
        if ( spread > kStaleDieMilliCelsius ) {
            return Quality::Stale;
        }
    }
    return Quality::Cal;
}

}  // namespace kleidos::meter
