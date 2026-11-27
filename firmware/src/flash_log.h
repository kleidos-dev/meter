// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file flash_log.h
 * @ingroup flash_log
 * @brief Append-only logs of records, each over two 4 KiB sectors of raw
 *        flash, whose newest complete record survives a power cut at any
 *        point of a save, with nothing allocated.
 *
 * The calibration was kept in NVS until FW-16, and every NVS save allocated.
 * This module replaced it: one blob of at most kMaxBlobBytes bytes, written
 * as a new record each time, in two sectors of flash the log has to itself
 * (FW-15), the `calib` partition (`flash_port.h`), which `CalibrationStore`
 * loads from and saves to (FW-16). From 0.18.0 there are two logs of the
 * same kind (FW-03), chosen by the caller with a Log: the calibration's, and
 * the pin map's in the `pins` partition (`pinmap_store.h`).
 *
 *  - **A record** is a header of kHeaderBytes and the blob, at a 4-byte
 *    aligned offset of its sector: the magic kMagic, a sequence number one
 *    above the newest record's (1 for the first), the blob's length, and a
 *    CRC-32 (`crc32.h`) over the three words and the blob, each word stored
 *    least significant byte first. The bytes after the blob up to the next
 *    4-byte boundary are left erased.
 *  - **The newest valid record wins.** mount() reads both sectors from their
 *    first byte, record after record, until an erased header (the sector's
 *    free space starts there), a header or CRC that does not hold (the rest
 *    of the sector is not trusted), or the sector's end (less room than the
 *    smallest record takes); of every record that holds, the one with the
 *    highest sequence is the log's.
 *  - **The owner of the blob can refuse a record** (FW-16): mount() takes an
 *    Acceptor, which judges each record's blob once its header and CRC hold,
 *    and a record it refuses is treated as one whose CRC does not hold: the
 *    scan stops there, and the newest record it accepted wins. save()
 *    writes nothing the Acceptor refuses (Status::Refused).
 *  - **A save appends** the new record where the newest one's sector's
 *    records end, when the record fits before the sector's end and the bytes
 *    it will take read erased; so nothing is appended after a record that
 *    did not hold, whose header does not read erased. Otherwise, and only
 *    then, it erases the other sector and writes the record at its start. So
 *    the sector that holds the newest record is never erased, and is never
 *    written but where it reads erased.
 *  - **Each save reads its record back** and compares it with what it wrote,
 *    byte for byte, before it answers Status::Ok; a load reads the record
 *    again and checks its header and CRC before it gives the blob out.
 *
 * **Power cuts.** A save makes at most two changes to the flash: the erase of
 * the other sector, when the newest one's is full or not trusted, and the
 * write of the record. A cut during the erase leaves the newest record where
 * it was; a cut during the write leaves a record that is either complete
 * (the new newest) or fails its CRC (the old one stays the newest, and that
 * sector takes nothing more). The next mount() finds the last complete
 * record in every case (`firmware/test/native/test_flash_log.cpp` cuts the
 * power after every byte of every write and every erase of a run that
 * changes sectors several times).
 *
 * **What it does not survive.** A bit the flash loses inside a record that a
 * later record follows, in the same sector, hides that later record: the
 * scan stops at the first record that does not hold. A power cut cannot
 * cause that (it only ever tears the last record a sector takes); a failing
 * flash can, and the calibration registry, not this log, is the calibration
 * (`calibration/README.md`).
 *
 * **The flash.** The log reads, writes and erases through three functions
 * the port defines (flash_log::port): `flash_port.cpp`'s, on ESP-IDF's
 * `esp_flash_read()`, `esp_flash_write()` and `esp_flash_erase_region()` on
 * `esp_flash_default_chip`, with no memory map and no `esp_partition_*` call,
 * and with ESP-IDF's region check, whose partition iterator is allocated,
 * replaced at link time by a static one (`flash_port.h`). The host tests
 * compile the same port against the harness's fakes of those three calls,
 * on its model of the flash (`sim::Flash`). Every byte passes through one
 * static buffer of kMaxRecordBytes in internal RAM, which ESP-IDF's read
 * fills directly and its write takes as it is.
 *
 * **Two logs, one caller each.** Each Log keeps its state, its buffer and
 * its Acceptor in a static object of its own, and the two share nothing but
 * the port: a call on one reads and writes only the two sectors its mount
 * was given, so a save of one, or a power cut during it, never changes what
 * the other mounts (`test_flash_log.cpp` cuts each log's every write and
 * erase and reads the other after each cut). Neither is reentrant: one task
 * at a time mounts, saves and loads (the console, which owns the calibration
 * and the pin map; `app_main()` before the console exists).
 */
#ifndef KLEIDOS_METER_FLASH_LOG_H
#define KLEIDOS_METER_FLASH_LOG_H

#include <cstddef>
#include <cstdint>

namespace kleidos::meter::flash_log {

/// @addtogroup flash_log
/// @{

/// @brief One sector of the flash, the unit of an erase: 4 KiB.
constexpr std::uint32_t kSectorBytes = 4096;

/// @brief The log's sectors: two, side by side.
constexpr std::uint32_t kSectorCount = 2;

/// @brief The first word of every record: the bytes `K`, `L`, `O`, `G` in
///        the order they are stored.
constexpr std::uint32_t kMagic = 0x474F4C4BU;

/// @brief A record's header: the magic, the sequence, the blob's length and
///        the CRC, four 32-bit words.
constexpr std::size_t kHeaderBytes = 16;

/// @brief The largest blob a record holds: 128 bytes, room beside the
///        calibration's 80 (`calibration.cpp`, `StoredBlob`).
constexpr std::size_t kMaxBlobBytes = 128;

/// @brief The largest record, and the size of the module's one buffer.
constexpr std::size_t kMaxRecordBytes = kHeaderBytes + kMaxBlobBytes;

/// @brief The boundary every record starts on.
constexpr std::uint32_t kRecordAlignBytes = 4;

/// @brief The highest flash address the log's first sector may start at: its
///        two sectors end at the top of the 32-bit address space.
constexpr std::uint32_t kLastBaseAddress = 0xFFFFFFFFU - ( kSectorCount * kSectorBytes ) + 1U;

/// @brief The highest sequence number: a log whose newest record has it
///        takes no other (Status::Exhausted). At one save a second it is
///        136 years away.
constexpr std::uint32_t kLastSequence = 0xFFFFFFFFU;

static_assert( kMaxRecordBytes <= kSectorBytes, "a record fits a sector" );
static_assert( kHeaderBytes % kRecordAlignBytes == 0U, "a blob starts aligned" );
static_assert( kMaxBlobBytes % kRecordAlignBytes == 0U, "the largest record needs no padding" );

/// @brief The logs, one for each owner of a record; each is mounted on its
///        own two sectors and keeps its own state, buffer and Acceptor.
enum class Log : std::uint8_t {
    Calibration,  ///< The calibration's (`calibration.h`), in the `calib` partition.
    Pins,         ///< The pin map's (`pinmap_store.h`), in the `pins` partition.
};

/// @brief How many logs there are: one State each.
constexpr std::size_t kLogCount = 2;

static_assert( static_cast<std::size_t>( Log::Pins ) + 1U == kLogCount, "one State per Log" );

/// @brief What a call of the log answers.
enum class Status : std::uint8_t {
    Ok,          ///< Done.
    Empty,       ///< load(): the log holds no valid record.
    NotMounted,  ///< mount() has not been given a base address.
    BadAddress,  ///< mount(): the base is not a sector boundary, or too high.
    BadLength,   ///< A blob of 0 bytes or above kMaxBlobBytes, a null pointer, or no room.
    FlashError,  ///< A read, write or erase failed, or a record did not read back.
    Exhausted,   ///< save(): the newest record has kLastSequence.
    Refused,     ///< save(): the Acceptor refuses the blob; nothing was written.
};

/// @brief The owner's judgment of a record's blob, beyond its CRC: true to
///        take it.
///
/// Called with the blob in the log's buffer, its length 1 to kMaxBlobBytes;
/// it must not call the log.
using Acceptor = bool ( * )( const std::uint8_t* blob, std::size_t length ) noexcept;

/// @brief Where the log stands, for a test or a report.
struct Head {
    bool          mounted   = false;  ///< mount() was given a base address.
    bool          scanned   = false;  ///< The sectors were read since, and no save failed after.
    bool          hasRecord = false;  ///< A valid record was found or written.
    std::uint32_t sequence  = 0;      ///< The newest record's sequence (0 with none).
    std::uint32_t sector    = 0;      ///< Its sector, 0 or 1 (0 with none).
    std::uint32_t offset    = 0;      ///< Its offset in that sector.
    std::uint32_t length    = 0;      ///< Its blob's length.
    /// Records the last scan found whole (header and CRC) and the Acceptor
    /// refused: each ended its sector's scan.
    std::uint32_t refused = 0;
    /// The highest sequence among them (0 with none). The next save numbers
    /// its record above it as well as above the newest record's.
    std::uint32_t refusedSequence = 0;
};

/// @brief A record's size in its sector: the header and the blob, rounded up
///        to kRecordAlignBytes.
/// @param[in] length The blob's length, at most kMaxBlobBytes.
/// @return The bytes from the record's offset to where the next one starts.
constexpr std::uint32_t recordBytes( std::uint32_t length ) noexcept {
    return static_cast<std::uint32_t>( kHeaderBytes )
           + ( ( length + kRecordAlignBytes - 1U ) / kRecordAlignBytes ) * kRecordAlignBytes;
}

static_assert( recordBytes( 1 ) == 20U && recordBytes( 4 ) == 20U && recordBytes( 5 ) == 24U,
               "a blob is padded to the next word" );
static_assert( recordBytes( kMaxBlobBytes ) == kMaxRecordBytes, "the largest record" );

/// @brief Takes @p log over the two sectors from @p baseAddress and reads
///        them for the newest valid record that @p accept takes.
///
/// Forgets whatever the log knew before, as a reboot does. A read that fails
/// leaves the log mounted but unread: the next save() or load() reads the
/// sectors again first. @p accept stays the log's until the next mount().
/// The other log is not touched.
///
/// @param[in] log         Which log.
/// @param[in] baseAddress The flash address of the first sector: a multiple of
///                        kSectorBytes, at most kLastBaseAddress.
/// @param[in] accept      The owner's judgment of each record's blob, or
///                        nullptr to take every record whose header and CRC
///                        hold.
/// @retval Status::Ok         Read; head() says what was found.
/// @retval Status::BadAddress Not a sector boundary, or too high; the log is
///                            not mounted and nothing was read.
/// @retval Status::FlashError A read failed.
Status mount( Log log, std::uint32_t baseAddress, Acceptor accept ) noexcept;

/// @brief Writes @p blob as the new newest record of @p log.
///
/// Appends it after the newest record, or erases the other sector and writes
/// it there when the newest record's sector cannot take it (the file comment
/// says when). Reads the record back and compares it with what it wrote
/// before it answers.
///
/// @param[in] log    Which log.
/// @param[in] blob   The bytes to keep.
/// @param[in] length How many: 1 to kMaxBlobBytes.
/// @retval Status::Ok         Written and read back; it is the newest record.
/// @retval Status::NotMounted mount() has not been given a base address.
/// @retval Status::BadLength  @p blob is null, or @p length is 0 or above
///                            kMaxBlobBytes; nothing was written.
/// @retval Status::Exhausted  The newest record, or a refused one, has
///                            kLastSequence; nothing was written.
/// @retval Status::Refused    The mount's Acceptor refuses @p blob; nothing
///                            was written.
/// @retval Status::FlashError A read, the erase or the write failed, the
///                            erased sector did not read erased, or the record
///                            did not read back; the log reads the sectors
///                            again at its next call.
Status save( Log log, const std::uint8_t* blob, std::size_t length ) noexcept;

/// @brief Copies the blob of the newest record of @p log into @p out.
///
/// Reads the record from the flash again and checks its header and CRC, that
/// the Acceptor still takes it, and that it is still the newest record's
/// sequence and length.
///
/// @param[in]  log      Which log.
/// @param[out] out      Where the blob goes.
/// @param[in]  capacity The bytes @p out has room for.
/// @param[out] length   The blob's length, set only with Status::Ok.
/// @retval Status::Ok         Copied.
/// @retval Status::NotMounted mount() has not been given a base address.
/// @retval Status::Empty      The log holds no valid record.
/// @retval Status::BadLength  @p out is null or has no room for the blob.
/// @retval Status::FlashError A read failed, or the record no longer holds;
///                            the log reads the sectors again at its next
///                            call.
Status load( Log log, std::uint8_t* out, std::size_t capacity, std::size_t& length ) noexcept;

/// @brief Where @p log stands.
/// @param[in] log Which log.
/// @return The newest record's place, and whether the log is mounted and read.
Head head( Log log ) noexcept;

/// @brief The raw flash under the log, which the port defines:
///        `flash_port.cpp`, on ESP-IDF's `esp_flash_*` calls on
///        `esp_flash_default_chip` (FW-16), which the host tests fake on the
///        harness's model of the flash.
///
/// Every address is absolute and every range lies inside the two sectors of
/// the log that makes the call. Each call is complete when it returns.
namespace port {

/// @brief Reads @p length bytes at @p address into @p data.
/// @param[in]  address The first byte's flash address.
/// @param[out] data    Where they go: the log's buffer, in internal RAM.
/// @param[in]  length  How many, at most kMaxRecordBytes.
/// @return true if every byte was read; false, with nothing read, for a
///         destination outside internal RAM, which ESP-IDF would read
///         through a buffer it allocates.
bool read( std::uint32_t address, std::uint8_t* data, std::size_t length ) noexcept;

/// @brief Writes @p length bytes from @p data at @p address. Flash only
///        clears bits: the log writes only where it read erased.
/// @param[in] address The first byte's flash address.
/// @param[in] data    The bytes: the log's buffer.
/// @param[in] length  How many, at most kMaxRecordBytes.
/// @return true if the write was made.
bool program( std::uint32_t address, const std::uint8_t* data, std::size_t length ) noexcept;

/// @brief Erases the sector at @p address: every byte reads 0xFF after.
/// @param[in] address The sector's first byte, a multiple of kSectorBytes.
/// @return true if the erase was made.
bool eraseSector( std::uint32_t address ) noexcept;

}  // namespace port

/// @}

}  // namespace kleidos::meter::flash_log

#endif  // KLEIDOS_METER_FLASH_LOG_H
