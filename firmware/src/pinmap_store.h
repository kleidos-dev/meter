// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file pinmap_store.h
 * @ingroup pin_map
 * @brief The pin map's record in its own flash log: the blob, the store's
 *        judgment of a record, the load and the save.
 *
 * The map a person applies is kept as the newest record of the second record
 * log (flash_log::Log::Pins), in the `pins` partition at
 * flash_port::kPinLogBaseAddress (FW-03), apart from the calibration's: a pin
 * save never writes a sector the calibration log reads, and a calibration
 * restore never rewrites the pins (`docs/architecture.md` §2.5).
 *
 * **The blob** is kBlobBytes, every field explicitly sized and the padding
 * named, in the CPU's byte order (little-endian on the device and on every
 * host the tests run on): the magic kBlobMagic, the layout's kBlobVersion,
 * the board table's digest (pin_table::kPinTableDigest) it was written
 * against, the five roles' GPIOs, the boot clock, the three pulls, the two
 * relays' active levels and the address.
 *
 * **A record is taken only if this image would have written it.** The log's
 * Acceptor refuses another length, magic or version, another table's digest
 * (an image built from another `firmware/board/pins.toml`), a pull or a drive
 * byte that names no value, a byte of padding that is not zero, and a map
 * pinmap::validate() refuses. load() goes further: a refused record newer
 * than the one taken, or not older than it, means the map last applied is not
 * one this image takes, and the store answers Found::Refused with the
 * defaults rather than an older map, which nobody chose last.
 *
 * Nothing is allocated, and the store keeps no state of its own: each load()
 * and save() mounts the log again, reading the flash as a reboot would. One
 * task at a time calls it (flash_log.h, "Two logs, one caller each").
 */
#ifndef KLEIDOS_METER_PINMAP_STORE_H
#define KLEIDOS_METER_PINMAP_STORE_H

#include "pinmap.h"

#include <array>
#include <cstddef>
#include <cstdint>

namespace kleidos::meter::pinmap_store {

/// @addtogroup pin_map
/// @{

/// @brief The blob's first word: the bytes `P`, `I`, `N`, `M` in the order
///        they are stored.
constexpr std::uint32_t kBlobMagic = 0x4D4E4950U;

/// @brief The blob's layout: 1, the first.
constexpr std::uint32_t kBlobVersion = 1U;

/// @brief The blob's bytes: the header (magic, version, the table's
///        digest), the five GPIOs, the clock, the six one-byte fields and two
///        named bytes of padding.
constexpr std::size_t kBlobBytes = 44;

/// @brief The bytes of one record's blob.
using Blob = std::array<std::uint8_t, kBlobBytes>;

/// @brief What load() found.
enum class Found : std::uint8_t {
    Stored,      ///< The newest record holds a map this image takes: it is the map.
    Empty,       ///< The log holds no record: the defaults.
    Refused,     ///< The newest record is not one this image takes: the defaults.
    Unreadable,  ///< A read of the flash failed: the defaults.
};

/// @brief What load() answers: what it found, and the map to run.
struct Loaded {
    Found          found = Found::Empty;         ///< What the log held.
    pinmap::PinMap map   = pinmap::kDefaultMap;  ///< The stored map, else kDefaultMap.
};

/// @brief What save() answers.
enum class Saved : std::uint8_t {
    Ok,       ///< Written and read back: the newest record.
    Refused,  ///< A map this image would not take back; nothing was written.
    Failed,   ///< The flash failed, or the log has no sequence left.
};

/// @brief The blob that stores @p map, against this image's table.
/// @param[in] map The map, valid or not.
/// @return Its bytes, every byte of padding zero.
[[nodiscard]] Blob encode( const pinmap::PinMap& map ) noexcept;

/// @brief Mounts the pin map log and reads its newest record, as the boot
///        does.
/// @return What was found, and the stored map or the defaults.
[[nodiscard]] Loaded load() noexcept;

/// @brief Writes @p map as the log's new newest record.
///
/// Mounts the log first (so the judgment is this store's whatever mounted it
/// before), then appends the record; the log reads it back byte for byte
/// before it answers Ok.
/// @param[in] map The map to keep.
/// @retval Saved::Ok      Written and read back.
/// @retval Saved::Refused The store's judgment refuses it (pinmap::validate()
///                        does); nothing was written.
/// @retval Saved::Failed  A read, the erase or the write failed, the record
///                        did not read back, or no sequence is left.
[[nodiscard]] Saved save( const pinmap::PinMap& map ) noexcept;

/// @}

}  // namespace kleidos::meter::pinmap_store

#endif  // KLEIDOS_METER_PINMAP_STORE_H
