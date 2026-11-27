// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file identity.cpp
 * @ingroup instrument_identity
 * @brief The serial, read from the factory MAC.
 */
#include "identity.h"

#include <esp_mac.h>

#include <array>

namespace kleidos::meter::identity {

bool readSerial( char ( &out )[kSerialLength + 1] ) noexcept {
    std::array<std::uint8_t, 6> mac = {};
    if ( esp_efuse_mac_get_default( mac.data() ) != ESP_OK ) {
        out[0] = '0';
        out[1] = '\0';
        return false;
    }
    formatSerial( mac, out );
    return true;
}

}  // namespace kleidos::meter::identity
