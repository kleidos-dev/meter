// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file flash_port.cpp
 * @ingroup flash_log
 * @brief The record log's port on `esp_flash_*`, and the static region check
 *        the image links in place of ESP-IDF's.
 *
 * Nothing is allocated (FW-16): the three port functions hand ESP-IDF the
 * log's own buffer, which is in internal RAM (a read refuses any other
 * destination, for which ESP-IDF would take a bounce buffer from the heap),
 * and the region check is a comparison with the two logs' regions.
 */
#include "flash_port.h"

#include <esp_err.h>
#include <esp_flash.h>
#include <esp_memory_utils.h>

#include <cstddef>
#include <cstdint>

namespace kleidos::meter {

namespace flash_port {

namespace {

/// @brief Whether [@p address, @p address + @p length) lies inside the log
///        region that starts at @p first.
///
/// Formed so that nothing overflows: the start inside the region, and the
/// length within what is left of it from there.
/// @param[in] address The first byte's flash address.
/// @param[in] length  How many bytes.
/// @param[in] first   The region's first byte: a log's base address.
/// @return true if every byte lies in [@p first, @p first + kLogBytes).
constexpr bool inRegion( std::size_t address, std::size_t length, std::size_t first ) noexcept {
    constexpr std::size_t kBytes = kLogBytes;
    return address >= first && address - first <= kBytes && length <= kBytes - ( address - first );
}

}  // namespace

bool writable( std::size_t address, std::size_t length ) noexcept {
    return inRegion( address, length, kLogBaseAddress )
           || inRegion( address, length, kPinLogBaseAddress );
}

}  // namespace flash_port

namespace flash_log::port {

bool read( std::uint32_t address, std::uint8_t* data, std::size_t length ) noexcept {
    // ESP-IDF reads straight into a destination in internal RAM, and into any
    // other through a bounce buffer it takes from the heap and frees
    // (`get_temp_buffer`, esp_flash_api.c): so no other destination is
    // given it. The log reads only into its own static buffer.
    if ( !esp_ptr_in_dram( data ) ) {
        return false;
    }
    return esp_flash_read( esp_flash_default_chip, data, address,
                           static_cast<std::uint32_t>( length ) )
           == ESP_OK;
}

bool program( std::uint32_t address, const std::uint8_t* data, std::size_t length ) noexcept {
    return esp_flash_write( esp_flash_default_chip, data, address,
                            static_cast<std::uint32_t>( length ) )
           == ESP_OK;
}

bool eraseSector( std::uint32_t address ) noexcept {
    return esp_flash_erase_region( esp_flash_default_chip, address, kSectorBytes ) == ESP_OK;
}

}  // namespace flash_log::port

}  // namespace kleidos::meter

// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" bool __wrap_esp_partition_is_flash_region_writable( std::size_t addr,
                                                               std::size_t size ) noexcept {
    return kleidos::meter::flash_port::writable( addr, size );
}
