// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file pinmap_store.cpp
 * @ingroup pin_map
 * @brief The pin map's blob, the store's judgment of a record, and the load
 *        and the save over the pin map log.
 */
#include "pinmap_store.h"

#include "flash_log.h"
#include "flash_port.h"
#include "pin_table.h"
#include "relay.h"

#include <algorithm>
#include <bit>
#include <initializer_list>

namespace kleidos::meter::pinmap_store {

namespace {

/// The record's layout. Explicitly sized fields, the padding named, so that
/// what is read back is what was written and nothing is inferred from a
/// compiler's padding choices.
struct StoredMap {
    std::uint32_t                                   magic{};        ///< kBlobMagic.
    std::uint32_t                                   version{};      ///< kBlobVersion.
    std::uint32_t                                   tableDigest{};  ///< pin_table::kPinTableDigest.
    std::array<std::int32_t, pin_table::kRoleCount> pins{};         ///< Each role's GPIO.
    std::uint32_t                                   sclHz{};        ///< The boot clock, Hz.
    std::uint8_t                                    pullSda{};      ///< 1: Pull::Up, 0: Pull::Off.
    std::uint8_t                                    pullScl{};      ///< As pullSda.
    std::uint8_t                                    pullAlert{};    ///< As pullSda.
    std::uint8_t                relay1Drive{};  ///< 1: Drive::ActiveHigh, 0: Drive::ActiveLow.
    std::uint8_t                relay2Drive{};  ///< As relay1Drive.
    std::uint8_t                address{};      ///< The INA228's address, or pinmap::kAddressAuto.
    std::array<std::uint8_t, 2> tail{};         ///< Named padding, written and read 0.
};
static_assert( sizeof( StoredMap ) == kBlobBytes,
               "the layout is 44 bytes with no implicit padding" );
static_assert( kBlobBytes <= flash_log::kMaxBlobBytes, "the blob fits one record" );

/// @brief The byte that stores @p pull.
/// @param[in] pull The pull.
/// @return 1 for Pull::Up, 0 for Pull::Off.
constexpr std::uint8_t pullByte( pinmap::Pull pull ) noexcept {
    return ( pull == pinmap::Pull::Up ) ? std::uint8_t{ 1U } : std::uint8_t{ 0U };
}

/// @brief The byte that stores @p drive.
/// @param[in] drive The relay's active level.
/// @return 1 for Drive::ActiveHigh, 0 for Drive::ActiveLow.
constexpr std::uint8_t driveByte( relay::Drive drive ) noexcept {
    return ( drive == relay::Drive::ActiveHigh ) ? std::uint8_t{ 1U } : std::uint8_t{ 0U };
}

/// @brief The pull a byte the store accepted names.
/// @param[in] byte 0 or 1.
/// @return Pull::Up for 1, Pull::Off for 0.
constexpr pinmap::Pull pullOf( std::uint8_t byte ) noexcept {
    return ( byte == 1U ) ? pinmap::Pull::Up : pinmap::Pull::Off;
}

/// @brief The active level a byte the store accepted names.
/// @param[in] byte 0 or 1.
/// @return Drive::ActiveHigh for 1, Drive::ActiveLow for 0.
constexpr relay::Drive driveOf( std::uint8_t byte ) noexcept {
    return ( byte == 1U ) ? relay::Drive::ActiveHigh : relay::Drive::ActiveLow;
}

static_assert( pullOf( pullByte( pinmap::Pull::Up ) ) == pinmap::Pull::Up
                   && pullOf( pullByte( pinmap::Pull::Off ) ) == pinmap::Pull::Off
                   && driveOf( driveByte( relay::Drive::ActiveHigh ) ) == relay::Drive::ActiveHigh
                   && driveOf( driveByte( relay::Drive::ActiveLow ) ) == relay::Drive::ActiveLow,
               "a pull and a drive read back as they were stored" );

/// @brief The map a blob holds, its pull and drive bytes each 0 or 1.
/// @param[in] blob The record's blob.
/// @return Its map.
pinmap::PinMap fromBlob( const StoredMap& blob ) noexcept {
    pinmap::PinMap map = {};
    map.pins           = blob.pins;
    map.pullSda        = pullOf( blob.pullSda );
    map.pullScl        = pullOf( blob.pullScl );
    map.pullAlert      = pullOf( blob.pullAlert );
    map.relay1Drive    = driveOf( blob.relay1Drive );
    map.relay2Drive    = driveOf( blob.relay2Drive );
    map.address        = blob.address;
    map.sclHz          = blob.sclHz;
    return map;
}

/// @brief The store's judgment of a record's blob (flash_log::Acceptor):
///        what this image would have written.
///
/// Refused, and so read as no record (load() says when an older one is not
/// taken instead): another length, magic or version, another board table's
/// digest, a pull or drive byte other than 0 or 1, a byte of padding other
/// than 0, and a map pinmap::validate() refuses.
/// @param[in] blob   The record's blob.
/// @param[in] length Its length.
/// @return true to take the record.
bool acceptBlob( const std::uint8_t* blob, std::size_t length ) noexcept {
    if ( length != kBlobBytes ) {
        return false;
    }
    Blob raw = {};
    (void)std::copy_n( blob, raw.size(), raw.begin() );
    const auto stored = std::bit_cast<StoredMap>( raw );
    if ( stored.magic != kBlobMagic || stored.version != kBlobVersion
         || stored.tableDigest != pin_table::kPinTableDigest ) {
        return false;
    }
    for ( const std::uint8_t flag : { stored.pullSda, stored.pullScl, stored.pullAlert,
                                      stored.relay1Drive, stored.relay2Drive } ) {
        if ( flag > 1U ) {
            return false;
        }
    }
    if ( stored.tail != std::array<std::uint8_t, 2>{} ) {
        return false;
    }
    return pinmap::validate( fromBlob( stored ) ).problem == pinmap::Problem::None;
}

/// @brief Takes the pin map log over the `pins` partition with the store's
///        judgment, reading it as a reboot would.
/// @return The mount's answer.
flash_log::Status mountLog() noexcept {
    return flash_log::mount( flash_log::Log::Pins, flash_port::kPinLogBaseAddress, &acceptBlob );
}

}  // namespace

Blob encode( const pinmap::PinMap& map ) noexcept {
    StoredMap blob   = {};
    blob.magic       = kBlobMagic;
    blob.version     = kBlobVersion;
    blob.tableDigest = pin_table::kPinTableDigest;
    blob.pins        = map.pins;
    blob.sclHz       = map.sclHz;
    blob.pullSda     = pullByte( map.pullSda );
    blob.pullScl     = pullByte( map.pullScl );
    blob.pullAlert   = pullByte( map.pullAlert );
    blob.relay1Drive = driveByte( map.relay1Drive );
    blob.relay2Drive = driveByte( map.relay2Drive );
    blob.address     = map.address;
    return std::bit_cast<Blob>( blob );
}

Loaded load() noexcept {
    Loaded out;
    if ( mountLog() != flash_log::Status::Ok ) {
        out.found = Found::Unreadable;
        return out;
    }
    // A refused record not older than the one taken is the map last applied
    // under another table, layout or rule: nobody chose the older map after
    // it, so the defaults run, as with no record.
    const flash_log::Head log = flash_log::head( flash_log::Log::Pins );
    if ( log.refused != 0U && log.refusedSequence >= log.sequence ) {
        out.found = Found::Refused;
        return out;
    }
    Blob                    raw    = {};
    std::size_t             length = 0;
    const flash_log::Status status =
        flash_log::load( flash_log::Log::Pins, raw.data(), raw.size(), length );
    if ( status == flash_log::Status::Empty ) {
        out.found = Found::Empty;
        return out;
    }
    if ( status != flash_log::Status::Ok ) {
        out.found = Found::Unreadable;
        return out;
    }
    // The log gave out only a blob acceptBlob() took: of this layout's
    // length, magic, version and table, with a map validate() takes.
    out.found = Found::Stored;
    out.map   = fromBlob( std::bit_cast<StoredMap>( raw ) );
    return out;
}

Saved save( const pinmap::PinMap& map ) noexcept {
    // A mount that failed a read still mounts: the save reads the sectors
    // again first, and fails if that read fails too.
    (void)mountLog();
    const Blob              raw = encode( map );
    const flash_log::Status status =
        flash_log::save( flash_log::Log::Pins, raw.data(), raw.size() );
    if ( status == flash_log::Status::Ok ) {
        return Saved::Ok;
    }
    if ( status == flash_log::Status::Refused ) {
        return Saved::Refused;
    }
    return Saved::Failed;
}

}  // namespace kleidos::meter::pinmap_store
