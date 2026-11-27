// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file crc32.h
 * @ingroup console_dispatch
 * @brief CRC-32 (IEEE 802.3, the zlib and PNG one) over the bytes of a ring
 *        dump, so that a host can hold a filed dump to its bytes, and over
 *        each record of the calibration log (`flash_log.h`).
 *
 * Reflected polynomial 0xEDB88320, initial value and final XOR 0xFFFFFFFF:
 * `zlib.crc32()` and `binascii.crc32()` give the same figure. The 256-entry
 * table is built at compile time (1 KiB of flash); one table lookup per byte
 * costs the dump nothing next to the 6 Mbps line it is written to.
 */
#ifndef KLEIDOS_METER_CRC32_H
#define KLEIDOS_METER_CRC32_H

#include "chars.h"

#include <array>
#include <cstddef>
#include <cstdint>

namespace kleidos::meter::crc32 {

/// @addtogroup console_dispatch
/// @{

/// The CRC's initial value, and the value XOR-ed in at the end.
constexpr std::uint32_t kInitial = 0xFFFFFFFFU;

/// @brief The lookup table, one entry per byte value.
/// @return The 256 entries of the reflected 0xEDB88320 CRC.
consteval std::array<std::uint32_t, 256> makeTable() noexcept {
    std::array<std::uint32_t, 256> table = {};
    for ( std::uint32_t byte = 0; byte < 256U; ++byte ) {
        std::uint32_t value = byte;
        for ( std::uint32_t bit = 0; bit < 8U; ++bit ) {
            value = ( ( value & 1U ) != 0U ) ? ( ( value >> 1U ) ^ 0xEDB88320U ) : ( value >> 1U );
        }
        table[byte] = value;
    }
    return table;
}

/// The table, built once at compile time.
inline constexpr std::array<std::uint32_t, 256> kTable = makeTable();

/// @brief Feeds one byte into a running CRC.
/// @param[in] running The running value.
/// @param[in] byte    The byte.
/// @return The running value after it.
constexpr std::uint32_t step( std::uint32_t running, std::uint8_t byte ) noexcept {
    return kTable[( running ^ byte ) & 0xFFU] ^ ( running >> 8U );
}

/// @brief Feeds @p length bytes into a running CRC.
///
/// Start from kInitial; the CRC of the bytes fed is `running ^ kInitial`.
///
/// @param[in] running The running value (kInitial before the first byte).
/// @param[in] data    The bytes; may be null when @p length is 0.
/// @param[in] length  How many.
/// @return The running value after them.
constexpr std::uint32_t update( std::uint32_t running, const char* data,
                                std::size_t length ) noexcept {
    for ( std::size_t i = 0; i < length; ++i ) {
        running = step( running, chars::byteOf( data[i] ) );
    }
    return running;
}

/// @brief Feeds @p length bytes into a running CRC: update() for the bytes of
///        a binary buffer (the calibration log's records, `flash_log.cpp`).
/// @param[in] running The running value (kInitial before the first byte).
/// @param[in] data    The bytes; may be null when @p length is 0.
/// @param[in] length  How many.
/// @return The running value after them.
constexpr std::uint32_t update( std::uint32_t running, const std::uint8_t* data,
                                std::size_t length ) noexcept {
    for ( std::size_t i = 0; i < length; ++i ) {
        running = step( running, data[i] );
    }
    return running;
}

static_assert( ( update( kInitial, "123456789", 9 ) ^ kInitial ) == 0xCBF43926U,
               "the IEEE CRC-32 check value" );

/// The check string's bytes, for the binary overload's own check.
inline constexpr std::array<std::uint8_t, 9> kCheckBytes = { 0x31, 0x32, 0x33, 0x34, 0x35,
                                                             0x36, 0x37, 0x38, 0x39 };

static_assert( ( update( kInitial, kCheckBytes.data(), kCheckBytes.size() ) ^ kInitial )
                   == 0xCBF43926U,
               "the IEEE CRC-32 check value, from bytes" );

/// @}

}  // namespace kleidos::meter::crc32

#endif  // KLEIDOS_METER_CRC32_H
