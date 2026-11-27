// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file idf_ledger.h
 * @brief The ESP-IDF entry points that allocate or free on the device, the
 *        ones the image's configuration makes safe, and the ledger their
 *        fakes keep.
 *
 * The fakes in `idf/` do not allocate as ESP-IDF does (a handle is a host
 * object, a queue a deque), so the harness's heap counts cannot see what an
 * ESP-IDF driver costs the device. Instead, every fake of an entry point on
 * kAllocatingIdf calls noteIdfAllocation() with its name, and the world keeps
 * the calls in order (sim::World::idfLedger). A world starts empty, so its
 * ledger holds what ran since the case's `app_main()` (Bench::boot(), or the
 * real one in the boot suite); bench::Bench and bench::Scope fail the case
 * whose ledger names an entry point that is not on kMadeSafeIdf.
 *
 * There is no allow-list (FW-17): the zero-allocation work packages took its
 * entries off as they removed the drivers behind them, FW-16 the last, and
 * FW-17 removed the empty list, so a call of any other listed entry point
 * fails its case whatever the source gates say. The lists are the same as the
 * source gate's (`tests/firmware/test_no_dynamic_memory.py`) and the object
 * gate's (`scripts/check_heap_objects.py`), which also say in which source
 * each made-safe call may stand and still hold an allow-list of their own,
 * empty; `tests/firmware/test_idf_allocation_lists.py` holds the three equal,
 * the gates' allow-lists empty, every fake of a listed entry point to a call
 * of noteIdfAllocation(), and each list's history to an allow-list that only
 * shrinks and a deny list that only grows.
 *
 * A third list, kMadeSafeIdf (FW-16), names the deny-list entry points the
 * firmware calls because the image's configuration takes the allocation out
 * of them: they allocate in ESP-IDF's default configuration, so they stay on
 * the deny list, which only grows, and the gates accept them only from the
 * source the source and object gates name, each with the mechanism that
 * makes them safe, which `tests/firmware/test_idf_allocation_lists.py` holds
 * in the tree.
 */
#pragma once

#include <array>
#include <string>
#include <string_view>
#include <vector>

namespace sim {

/// The deny list: ESP-IDF 6.0.3 entry points that allocate or free on the
/// heap when the firmware calls them (directly, through the partition
/// iterator a flash write's region check takes, or through a lazily created
/// C library lock). Not on it, because they take only what an entry on it
/// made: NVS's reads and `nvs_commit()` (NVS's lock, which
/// `nvs_flash_init()` creates; the commit is a no-op in 6.0.3),
/// `gpio_isr_handler_add()` (the service's table), the I2C and UART
/// transfers, and `esp_ipc_call_blocking()` (static semaphores).
inline constexpr auto kAllocatingIdf = std::to_array<std::string_view>( {
    // I2C master
    "i2c_new_master_bus",
    "i2c_del_master_bus",
    "i2c_master_bus_add_device",
    "i2c_master_bus_rm_device",
    // UART
    "uart_param_config",
    "uart_driver_install",
    "uart_driver_delete",
    // GPIO interrupts
    "gpio_install_isr_service",
    "gpio_uninstall_isr_service",
    "gpio_isr_register",
    // RMT
    "rmt_new_tx_channel",
    "rmt_new_rx_channel",
    "rmt_del_channel",
    "rmt_new_bytes_encoder",
    "rmt_new_copy_encoder",
    "rmt_new_simple_encoder",
    "rmt_del_encoder",
    // NVS
    "nvs_flash_init",
    "nvs_flash_init_partition",
    "nvs_flash_deinit",
    "nvs_flash_erase",
    "nvs_flash_erase_partition",
    "nvs_open",
    "nvs_open_from_partition",
    "nvs_close",
    "nvs_set_blob",
    "nvs_set_str",
    "nvs_set_u8",
    "nvs_set_u16",
    "nvs_set_u32",
    "nvs_set_u64",
    "nvs_set_i8",
    "nvs_set_i16",
    "nvs_set_i32",
    "nvs_set_i64",
    "nvs_erase_key",
    "nvs_erase_all",
    // Task watchdog
    "esp_task_wdt_init",
    "esp_task_wdt_deinit",
    "esp_task_wdt_add",
    "esp_task_wdt_add_user",
    "esp_task_wdt_delete",
    "esp_task_wdt_delete_user",
    // Interrupt allocator
    "esp_intr_alloc",
    "esp_intr_alloc_intrstatus",
    "esp_intr_alloc_bind",
    "esp_intr_alloc_intrstatus_bind",
    "esp_intr_free",
    // Partitions and flash
    "esp_partition_find",
    "esp_partition_find_first",
    "esp_partition_iterator_release",
    "esp_partition_mmap",
    "esp_partition_write",
    "esp_partition_write_raw",
    "esp_partition_erase_range",
    "esp_flash_read",
    "esp_flash_write",
    "esp_flash_erase_region",
    "spi_flash_mmap",
    "spi_flash_munmap",
    // Timers, ring buffers, FreeRTOS objects with capabilities, threads, PSA
    "esp_timer_create",
    "esp_timer_delete",
    "xRingbufferCreate",
    "vRingbufferDelete",
    "xQueueCreateWithCaps",
    "vQueueDeleteWithCaps",
    "xSemaphoreCreateBinaryWithCaps",
    "xSemaphoreCreateMutexWithCaps",
    "xSemaphoreCreateCountingWithCaps",
    "vSemaphoreDeleteWithCaps",
    "xTaskCreatePinnedToCoreWithCaps",
    "vTaskDeleteWithCaps",
    "pthread_create",
    "psa_crypto_init",
} );

/// The entry points of kAllocatingIdf the image's configuration makes
/// allocate nothing (FW-16): the calibration record log's write and erase,
/// whose only allocation in ESP-IDF 6.0.3 is the region check's partition
/// iterator, a check the image links wrapped onto a static table
/// (`flash_port.h`), with `CONFIG_SPI_FLASH_DANGEROUS_WRITE_ALLOWED` leaving
/// out the other, and whose bounce buffer a buffer in internal RAM never
/// needs.
inline constexpr auto kMadeSafeIdf = std::to_array<std::string_view>( {
    "esp_flash_read",
    "esp_flash_write",
    "esp_flash_erase_region",
} );

/// Records, on the current world's ledger, one call of the fake of
/// @p entry, which must be on kAllocatingIdf (the harness aborts otherwise:
/// a fake that names an entry point the lists do not know is the harness's
/// own mistake).
void noteIdfAllocation( std::string_view entry );

/// The calls of @p ledger whose entry point is not on kMadeSafeIdf, in
/// order, one per call; empty when every call is made safe.
std::vector<std::string> unlistedIdfAllocations( const std::vector<std::string_view>& ledger );

/// The calls of @p ledger whose entry point is not on kMadeSafeIdf, joined
/// with ", ": what a failing case prints ("" when every call is made safe).
std::string unlistedIdfAllocationText( const std::vector<std::string_view>& ledger );

}  // namespace sim
