// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file flash_port.h
 * @ingroup flash_log
 * @brief Where the two record logs live in the flash, the device's port of
 *        flash_log.h on `esp_flash_*`, and the static table that lets the
 *        image write those four sectors and no other.
 *
 * **The places.** The calibration log's two sectors are the `calib`
 * partition of `firmware/partitions.csv`, at kLogBaseAddress, right after
 * the partition table (FW-16); the pin map log's are the `pins` partition,
 * at kPinLogBaseAddress, right after `calib` (FW-03). Each is a fixed offset
 * the firmware knows as a constant, so that nothing looks a partition up
 * (`esp_partition_find()` takes an iterator from the heap).
 * `tests/firmware/test_partition_table.py` holds the CSV's two rows to these
 * constants.
 *
 * **The port.** flash_log::port reads, writes and erases through
 * `esp_flash_read()`, `esp_flash_write()` and `esp_flash_erase_region()` on
 * `esp_flash_default_chip`, every byte through the log's static buffer in
 * internal RAM. A read is the one call that would allocate otherwise: ESP-IDF
 * reads into a destination outside internal RAM through a bounce buffer it
 * takes from the heap (`get_temp_buffer`), so the port's read refuses such a
 * destination before it calls `esp_flash_read()`. A write copies through a
 * buffer on ESP-IDF's stack and an erase moves no data, whatever the source.
 *
 * **The region check, without the heap.** ESP-IDF checks every write and
 * erase of the main flash before it makes it (`main_flash_region_protected()`
 * in `spi_flash_os_func_app.c`): `esp_partition_is_flash_region_writable()`,
 * which walks the partition table with an iterator it allocates (and, the
 * first time, loads the table into a list it allocates), and, unless
 * `CONFIG_SPI_FLASH_DANGEROUS_WRITE_ALLOWED`, a second check against the
 * bootloader, the table and the running image. The image is linked with
 * `-Wl,--wrap=esp_partition_is_flash_region_writable`
 * (`firmware/src/CMakeLists.txt`), so the first check is
 * `__wrap_esp_partition_is_flash_region_writable()` below, writable() on a
 * static table of the two logs' regions; and `sdkconfig.defaults` sets
 * `CONFIG_SPI_FLASH_DANGEROUS_WRITE_ALLOWED`, so the second never runs. The
 * table is narrower than both checks were: a write or an erase anywhere else
 * in the flash, the bootloader, the table and the image included, and one
 * that runs from one log's region into the other's, is refused with
 * `ESP_ERR_NOT_ALLOWED` before the flash is touched.
 */
#ifndef KLEIDOS_METER_FLASH_PORT_H
#define KLEIDOS_METER_FLASH_PORT_H

#include "flash_log.h"

#include <cstddef>
#include <cstdint>

namespace kleidos::meter::flash_port {

/// @addtogroup flash_log
/// @{

/// @brief The flash address of the calibration log's first sector
///        (flash_log::Log::Calibration): the `calib` partition's offset in
///        `firmware/partitions.csv`, right after the partition table at
///        0x8000.
constexpr std::uint32_t kLogBaseAddress = 0x9000U;

/// @brief The bytes of each log: its two sectors, the size of the `calib`
///        partition and of the `pins` one.
constexpr std::uint32_t kLogBytes = flash_log::kSectorCount * flash_log::kSectorBytes;

/// @brief The flash address of the pin map log's first sector
///        (flash_log::Log::Pins): the `pins` partition's offset, right after
///        `calib`, in what 0x9000 to 0xFFFF left unused (FW-03).
constexpr std::uint32_t kPinLogBaseAddress = kLogBaseAddress + kLogBytes;

/// @brief Where the image's partition starts: the logs end before it.
constexpr std::uint32_t kImageAddress = 0x10000U;

static_assert( kLogBaseAddress % flash_log::kSectorBytes == 0U
                   && kPinLogBaseAddress % flash_log::kSectorBytes == 0U,
               "each log starts on a sector boundary" );
static_assert( kPinLogBaseAddress == 0xB000U && kPinLogBaseAddress + kLogBytes <= kImageAddress,
               "the pin map log is the 8 KiB after calib, clear of the image" );
static_assert( kPinLogBaseAddress <= flash_log::kLastBaseAddress,
               "the logs fit the address space" );

/// @brief Whether the image may write or erase [@p address, @p address +
///        @p length): only inside one log's two sectors.
///
/// The one table the image's region check reads (the file comment says how
/// it reaches ESP-IDF's). An empty range at a writable address is writable,
/// as ESP-IDF's own check answers it.
///
/// @param[in] address The first byte's flash address.
/// @param[in] length  How many bytes.
/// @retval true  The range lies inside [kLogBaseAddress, kLogBaseAddress +
///               kLogBytes) or inside [kPinLogBaseAddress,
///               kPinLogBaseAddress + kLogBytes).
/// @retval false Any byte of it lies outside both, it runs from one into
///               the other, or it wraps the address space.
bool writable( std::size_t address, std::size_t length ) noexcept;

/// @}

}  // namespace kleidos::meter::flash_port

/// @addtogroup flash_log
/// @{

/// @brief ESP-IDF's region check, as the image links it:
///        `-Wl,--wrap=esp_partition_is_flash_region_writable` sends every
///        call of `esp_partition_is_flash_region_writable()` here.
///
/// The name is the linker's convention for a wrapped symbol and its
/// signature ESP-IDF's (`esp_partition.h`), neither of them ours to choose
/// (`docs/misra.md` D7).
///
/// @param[in] addr The first byte's flash address.
/// @param[in] size How many bytes.
/// @return kleidos::meter::flash_port::writable() of the range.
// NOLINTNEXTLINE(bugprone-reserved-identifier,cert-dcl37-c,cert-dcl51-cpp,readability-identifier-naming): the linker's --wrap name (D7)
extern "C" bool __wrap_esp_partition_is_flash_region_writable( std::size_t addr,
                                                               std::size_t size ) noexcept;

/// @}

#endif  // KLEIDOS_METER_FLASH_PORT_H
