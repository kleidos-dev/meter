// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The main watchdog timer's low-level layer, as ESP-IDF 6.0.3's hal/mwdt_ll.h
// for the ESP32-S3 offers it (components/esp_hal_wdt/esp32s3/include), on the
// simulated board's Timer Group 0 MWDT (sim::World::mwdt): the write-protect
// key, the enable, the four stages, the interrupt, the reset lengths, the
// prescaler, the flash boot protection and the feed. A register write while
// the key is not written is lost, as on the device, and counted
// (sim::Mwdt::lockedWrites); every Bench and Scope requires none. The
// functions the firmware does not call are not faked.
#include <hal/wdt_types.h>

#include <cstdint>

/// A timer group's register block (`timg_dev_t`, soc/timer_group_struct.h).
struct timg_dev_t {
    int group;  ///< 0 or 1: which of the simulated groups this block is.
};
extern timg_dev_t TIMERG0;
extern timg_dev_t TIMERG1;

#define MWDT_LL_DEFAULT_CLK_PRESCALER 40000
#define MWDT_LL_WKEY_VALUE 0x50D83AA1

void mwdt_ll_enable( timg_dev_t* hw );
void mwdt_ll_disable( timg_dev_t* hw );
bool mwdt_ll_check_if_enabled( timg_dev_t* hw );
void mwdt_ll_config_stage( timg_dev_t* hw, wdt_stage_t stage, std::uint32_t timeout,
                           wdt_stage_action_t behavior );
void mwdt_ll_disable_stage( timg_dev_t* hw, std::uint32_t stage );
void mwdt_ll_set_level_intr( timg_dev_t* hw, bool enable );
void mwdt_ll_set_cpu_reset_length( timg_dev_t* hw, wdt_reset_sig_length_t length );
void mwdt_ll_set_sys_reset_length( timg_dev_t* hw, wdt_reset_sig_length_t length );
void mwdt_ll_set_flashboot_en( timg_dev_t* hw, bool enable );
void mwdt_ll_set_prescaler( timg_dev_t* hw, std::uint32_t prescaler );
void mwdt_ll_feed( timg_dev_t* hw );
void mwdt_ll_write_protect_enable( timg_dev_t* hw );
void mwdt_ll_write_protect_disable( timg_dev_t* hw );
void mwdt_ll_clear_intr_status( timg_dev_t* hw );
void mwdt_ll_set_intr_enable( timg_dev_t* hw, bool enable );
