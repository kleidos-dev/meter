// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file esp_flash.h
 * @brief The three calls of ESP-IDF's SPI flash API the calibration record
 *        log's port makes (flash_port.cpp), faked on the world's model of
 *        the flash (sim::Flash) as ESP-IDF 6.0.3 makes them on the main
 *        flash: a write or an erase asks the region check first, which the
 *        image links wrapped (`__wrap_esp_partition_is_flash_region_writable()`),
 *        and is refused with ESP_ERR_NOT_ALLOWED where it answers false.
 */
#pragma once
#include "esp_err.h"

#include <cstdint>

/// The flash chip ESP-IDF's API takes; the harness has the one.
typedef struct esp_flash_t {
    int unused;  ///< A chip has nothing the fakes read.
} esp_flash_t;

/// The main flash, as ESP-IDF declares it.
extern esp_flash_t* esp_flash_default_chip;

esp_err_t esp_flash_read( esp_flash_t* chip, void* buffer, std::uint32_t address,
                          std::uint32_t length );
esp_err_t esp_flash_write( esp_flash_t* chip, const void* buffer, std::uint32_t address,
                           std::uint32_t length );
esp_err_t esp_flash_erase_region( esp_flash_t* chip, std::uint32_t start, std::uint32_t len );
