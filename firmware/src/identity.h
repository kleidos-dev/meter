// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file identity.h
 * @ingroup instrument_identity
 * @brief Which instrument this is: its serial, and the image it runs.
 *
 * The calibration registry in this repository (`calibration/README.md`) is
 * the calibration; the record the instrument keeps in its flash (the
 * calibration record log, `flash_log.h`) is only a working copy of it, and a
 * full flash erase loses that copy. So the instrument needs a name that
 * survives `esptool erase_flash`, and the flash cannot hold it. The serial is
 * derived from the ESP32-S3's factory MAC (eFuse block 1, written by
 * Espressif):
 *
 *     serial = "KM-" + upper-hex( first 5 bytes of
 *                     SHA-256( "kleidos-meter-serial-v1" || mac[0..5] ) )
 *
 * A digest and not the MAC, because a MAC never enters a tracked file
 * (`AGENTS.md`). It is a pseudonym, not a secret: 48 bits can be searched.
 * 40 bits leave no realistic collision among instruments, and the registry's
 * check refuses a duplicate anyway.
 *
 * The serial names the assembly, not the calibrated parts: a replaced
 * controller is a new serial, which starts with a new calibration.
 */
#ifndef KLEIDOS_METER_IDENTITY_H
#define KLEIDOS_METER_IDENTITY_H

#include "bits.h"
#include "chars.h"
#include "sha256.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>

namespace kleidos::meter::identity {

/// @addtogroup instrument_identity
/// @{

/// @brief The domain string hashed ahead of the MAC; the `-v1` names this scheme.
constexpr char kSerialDomain[] = "kleidos-meter-serial-v1";

/// @brief The serial's length, excluding the NUL: `KM-` and ten hex digits.
constexpr std::size_t kSerialLength = 13;

/// @brief The image's name, as `SYSTem:INFormation?` reports it in `image=`:
///        the PlatformIO environment it was built as.
///
/// What the image knows about the rig is only what it drives, so this is the
/// one configuration fact the instrument can state for itself; the registry
/// names the rest.
///
/// From 0.8.0 there is one image, `meter`, and it drives the bypass on the
/// Relay 2 Click. Before 0.8.0 `meter` drove it on the HW-316, and the Click's
/// image was `meter_relay2` (0.3.0 to 0.7.0): the registry tells the two apart
/// by version (`calibration/README.md`).
constexpr const char* kImageName = "meter";

/// @brief Writes the serial of the instrument whose factory MAC is @p mac into
///        @p out, NUL-terminated.
/// @param[in]  mac The factory MAC, six bytes.
/// @param[out] out The serial, `KM-` and ten upper-case hex digits; it holds
///                 kSerialLength + 1 bytes.
inline void formatSerial( std::span<const std::uint8_t, 6> mac,
                          char ( &out )[kSerialLength + 1] ) noexcept {
    std::array<std::uint8_t, sizeof( kSerialDomain ) - 1U + mac.size()> message = {};
    for ( std::size_t i = 0; i < sizeof( kSerialDomain ) - 1U; ++i ) {
        message[i] = chars::byteOf( kSerialDomain[i] );
    }
    for ( std::size_t i = 0; i < mac.size(); ++i ) {
        message[sizeof( kSerialDomain ) - 1U + i] = mac[i];
    }
    sha256::Hasher hasher;
    hasher.update( message.data(), message.size() );
    std::uint8_t digest[sha256::kDigestBytes] = {};
    hasher.finish( digest );
    constexpr char kHex[] = "0123456789ABCDEF";
    out[0]                = 'K';
    out[1]                = 'M';
    out[2]                = '-';
    for ( std::size_t i = 0; i < 5U; ++i ) {
        out[3U + 2U * i] = kHex[bits::field( digest[i], 0xF0U, 4U )];
        out[4U + 2U * i] = kHex[digest[i] & 0x0FU];
    }
    out[kSerialLength] = '\0';
}

/// @brief Reads the factory MAC and writes this instrument's serial into
///        @p out.
/// @param[out] out The serial, NUL-terminated; `0` when the read fails.
/// @retval true  The serial was written.
/// @retval false The eFuse read failed; @p out is set to `0` (the IEEE 488.2
///               "no serial").
bool readSerial( char ( &out )[kSerialLength + 1] ) noexcept;

/// @}

}  // namespace kleidos::meter::identity

#endif  // KLEIDOS_METER_IDENTITY_H
