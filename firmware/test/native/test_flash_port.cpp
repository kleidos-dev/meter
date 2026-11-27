// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_flash_port.cpp
 * @brief flash_port.cpp: the static region check each of its conditions
 *        decides on its own, over both logs' regions and nothing between or
 *        across them, the wrapped symbol ESP-IDF calls answers as it does,
 *        and the logs' port reaches the flash through `esp_flash_*` only
 *        inside the `calib` and `pins` partitions (FW-16, FW-03).
 */
#include "flash_log.h"
#include "flash_port.h"
#include "scope.h"
#include "unit.h"

#include <esp_err.h>
#include <esp_flash.h>

#include <array>
#include <cstddef>
#include <cstdint>

using namespace kleidos::meter;
using bench::Scope;

namespace {

/// The calibration's log, the one these cases run on.
constexpr flash_log::Log kCal = flash_log::Log::Calibration;

constexpr std::uint32_t kBase = flash_port::kLogBaseAddress;
constexpr std::uint32_t kEnd  = kBase + flash_port::kLogBytes;

/// The pin map log's region.
constexpr std::uint32_t kPins    = flash_port::kPinLogBaseAddress;
constexpr std::uint32_t kPinsEnd = kPins + flash_port::kLogBytes;

}  // namespace

TEST( "flash_port", "the log sits in the calib partition, two sectors after the table" ) {
    CHECK( kBase == 0x9000U );
    CHECK( flash_port::kLogBytes == 0x2000U );
}

TEST( "flash_port", "the pin map log sits in the pins partition, right after calib" ) {
    CHECK( kPins == 0xB000U && kPins == kEnd );
    CHECK( kPinsEnd == 0xD000U && kPinsEnd <= flash_port::kImageAddress );
}

TEST( "flash_port", "the region check: each condition refuses on its own (MC/DC)" ) {
    // Inside: the whole log, its first and last byte, and an empty range at
    // its end.
    CHECK( flash_port::writable( kBase, flash_port::kLogBytes ) );
    CHECK( flash_port::writable( kBase, 1 ) );
    CHECK( flash_port::writable( kEnd - 1U, 1 ) );
    CHECK( flash_port::writable( kEnd, 0 ) );
    // The start before the log: the bootloader, the table, the byte before.
    CHECK( !flash_port::writable( 0x0, 16 ) );
    CHECK( !flash_port::writable( 0x8000, 0x1000 ) );
    CHECK( !flash_port::writable( kBase - 1U, 1 ) );
    // The start past both logs' end: the image and everything after it.
    CHECK( !flash_port::writable( kPinsEnd + 1U, 0 ) );
    CHECK( !flash_port::writable( 0x10000, 4096 ) );
    // A start inside and a length that runs past the end, or wraps.
    CHECK( !flash_port::writable( kBase, flash_port::kLogBytes + 1U ) );
    CHECK( !flash_port::writable( kEnd - 1U, 2 ) );
    CHECK( !flash_port::writable( kBase + 16U, static_cast<std::size_t>( -1 ) ) );
}

TEST( "flash_port", "the region check takes the pin map log's sectors, and nothing across" ) {
    // Inside the second region: the whole of it, its first and last byte, an
    // empty range at its end, and each sector as the log erases it.
    CHECK( flash_port::writable( kPins, flash_port::kLogBytes ) );
    CHECK( flash_port::writable( kPins, 1 ) );
    CHECK( flash_port::writable( kPinsEnd - 1U, 1 ) );
    CHECK( flash_port::writable( kPinsEnd, 0 ) );
    CHECK( flash_port::writable( kPins + 4096U, 4096 ) );
    // Past it, and a length that runs past its end or wraps.
    CHECK( !flash_port::writable( kPinsEnd + 1U, 0 ) );
    CHECK( !flash_port::writable( kPinsEnd, 1 ) );
    CHECK( !flash_port::writable( kPins, flash_port::kLogBytes + 1U ) );
    CHECK( !flash_port::writable( kPinsEnd - 1U, 2 ) );
    CHECK( !flash_port::writable( kPins + 16U, static_cast<std::size_t>( -1 ) ) );
    // From the calibration log into the pin map's: each region alone takes
    // its part, the check takes neither the whole.
    CHECK( flash_port::writable( kEnd - 1U, 1 ) && flash_port::writable( kPins, 1 ) );
    CHECK( !flash_port::writable( kEnd - 1U, 2 ) );
    CHECK( !flash_port::writable( kBase, 2U * flash_port::kLogBytes ) );
    CHECK( !flash_port::writable( kBase + 4096U, 8192 ) );
}

TEST( "flash_port", "ESP-IDF's wrapped region check answers as the table does" ) {
    CHECK( __wrap_esp_partition_is_flash_region_writable( kBase, 4096 ) );
    CHECK( __wrap_esp_partition_is_flash_region_writable( kPins, 4096 ) );
    CHECK( !__wrap_esp_partition_is_flash_region_writable( kBase + 4096U, 8192 ) );
    CHECK( !__wrap_esp_partition_is_flash_region_writable( 0x10000, 4096 ) );
}

TEST( "flash_port", "the pin map log reaches its sectors through esp_flash, and no other" ) {
    Scope scope;
    REQUIRE( flash_log::mount( flash_log::Log::Pins, kPins, nullptr ) == flash_log::Status::Ok );
    const std::array<std::uint8_t, 4> blob = { 5, 6, 7, 8 };
    REQUIRE( flash_log::save( flash_log::Log::Pins, blob.data(), blob.size() )
             == flash_log::Status::Ok );
    CHECK( scope->flash.at( kPins + 16U ) == 5U && scope->flash.at( kPins + 19U ) == 8U );
    CHECK( scope->flash.at( kBase ) == 0xFFU && scope->flash.refusedByRegion == 0 );
    CHECK( flash_log::port::eraseSector( kPins + 4096U ) );
    CHECK( scope->flash.erases[3] == 1U && scope->flash.erases[0] == 0U );
}

TEST( "flash_port", "the port reads, writes and erases the log's sectors through esp_flash" ) {
    Scope scope;
    REQUIRE( flash_log::mount( kCal, kBase, nullptr ) == flash_log::Status::Ok );
    const std::array<std::uint8_t, 4> blob = { 1, 2, 3, 4 };
    REQUIRE( flash_log::save( kCal, blob.data(), blob.size() ) == flash_log::Status::Ok );
    CHECK( scope->flash.at( kBase + 16U ) == 1U && scope->flash.at( kBase + 19U ) == 4U );
    CHECK( flash_log::port::eraseSector( kBase ) );
    CHECK( scope->flash.at( kBase + 16U ) == 0xFFU );
    CHECK( scope->flash.erases[0] == 1U );
    // A failed flash is a failed call, whichever it is.
    scope->flash.failErase = true;
    CHECK( !flash_log::port::eraseSector( kBase ) );
    scope->flash.failErase   = false;
    scope->flash.failProgram = true;
    CHECK( !flash_log::port::program( kBase, blob.data(), blob.size() ) );
    scope->flash.failProgram        = false;
    std::array<std::uint8_t, 4> out = {};
    scope->flash.readsUntilFailure  = 0;
    CHECK( !flash_log::port::read( kBase, out.data(), out.size() ) );
    CHECK( flash_log::port::read( kBase, out.data(), out.size() ) );
}

TEST( "flash_port", "a read into memory outside internal RAM is refused before ESP-IDF" ) {
    Scope scope;
    REQUIRE( flash_log::mount( kCal, kBase, nullptr ) == flash_log::Status::Ok );
    // ESP-IDF would read into a PSRAM destination (an EXT_RAM_BSS_ATTR array,
    // as the TRACE ring is) through a buffer it takes from the heap: the port
    // refuses one, and esp_flash_read() is never called with it.
    std::array<std::uint8_t, 16> psram = {};
    scope->externalRam.emplace_back( psram.data(), psram.size() );
    const std::size_t before = scope->idfLedger.size();
    CHECK( !flash_log::port::read( kBase, psram.data(), psram.size() ) );
    CHECK( !flash_log::port::read( kBase, psram.data() + 8, 4 ) );
    CHECK( scope->idfLedger.size() == before );
    CHECK( scope->flash.bounceBufferReads == 0 );
    // Internal RAM, as the log's own buffer is, is read.
    std::array<std::uint8_t, 16> internal = {};
    CHECK( flash_log::port::read( kBase, internal.data(), internal.size() ) );
    CHECK( scope->idfLedger.size() == before + 1U );
    CHECK( scope->idfLedger.back() == "esp_flash_read" );
    scope->externalRam.clear();
}

TEST( "flash_port", "a write or an erase outside the partition never reaches the flash" ) {
    Scope scope;
    // The fakes ask the region check as ESP-IDF does; the model's window is
    // moved over the image so that a write there would land.
    scope->flash.base                   = 0x10000U;
    const std::array<std::uint8_t, 4> b = { 0, 0, 0, 0 };
    CHECK( esp_flash_write( esp_flash_default_chip, b.data(), 0x10000U, 4 )
           == ESP_ERR_NOT_ALLOWED );
    CHECK( esp_flash_erase_region( esp_flash_default_chip, 0x10000U, 4096 )
           == ESP_ERR_NOT_ALLOWED );
    CHECK( scope->flash.programs == 0U && scope->flash.changes == 0U );
    CHECK( scope->flash.at( 0x10000U ) == 0xFFU );
    CHECK( scope->flash.refusedByRegion == 2 );
    // An erase of other than one sector inside it is the port's mistake.
    scope->flash.base = kBase;
    CHECK( esp_flash_erase_region( esp_flash_default_chip, kBase, 8192 ) == ESP_ERR_INVALID_ARG );
    CHECK( scope->flash.unalignedErase == 1 );
    // Both are the firmware's defects, which every Scope requires none of:
    // made here on purpose, and taken back.
    scope->flash.refusedByRegion = 0;
    scope->flash.unalignedErase  = 0;
}
