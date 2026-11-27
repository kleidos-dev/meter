// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The watchdog timers' stage, action and reset-length codes, as ESP-IDF
// 6.0.3's hal/wdt_types.h (components/esp_hal_wdt/include) names and numbers
// them: each is the value its register field takes.

/// The four stages of a watchdog timer.
typedef enum {
    WDT_STAGE0 = 0,
    WDT_STAGE1 = 1,
    WDT_STAGE2 = 2,
    WDT_STAGE3 = 3,
} wdt_stage_t;

/// What a stage does when its hold runs out.
typedef enum {
    WDT_STAGE_ACTION_OFF          = 0,
    WDT_STAGE_ACTION_INT          = 1,
    WDT_STAGE_ACTION_RESET_CPU    = 2,
    WDT_STAGE_ACTION_RESET_SYSTEM = 3,
    WDT_STAGE_ACTION_RESET_RTC    = 4,
} wdt_stage_action_t;

/// The length of the CPU and system reset signals.
typedef enum {
    WDT_RESET_SIG_LENGTH_100ns = 0,
    WDT_RESET_SIG_LENGTH_200ns = 1,
    WDT_RESET_SIG_LENGTH_300ns = 2,
    WDT_RESET_SIG_LENGTH_400ns = 3,
    WDT_RESET_SIG_LENGTH_500ns = 4,
    WDT_RESET_SIG_LENGTH_800ns = 5,
    WDT_RESET_SIG_LENGTH_1_6us = 6,
    WDT_RESET_SIG_LENGTH_3_2us = 7,
} wdt_reset_sig_length_t;
