// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// ESP-IDF 6.0.3's idle hooks (components/esp_system/include/esp_freertos_hooks.h)
// on the simulated board: a registered hook of Core 0 runs once a simulated
// millisecond, as that core's idle task would between its tasks, unless a
// test starves it (sim::World::idleStarved).
#include "freertos/FreeRTOS.h"

#include <esp_err.h>

/// An idle hook: returns whether the idle task may wait for an interrupt.
typedef bool ( *esp_freertos_idle_cb_t )( void );

/// Registers @p new_idle_cb on core @p cpuid's idle task, in a static table
/// of eight; ESP_ERR_NO_MEM when it is full (sim::World::failIdleHook stands
/// for that), ESP_ERR_INVALID_ARG for a core that does not exist.
esp_err_t esp_register_freertos_idle_hook_for_cpu( esp_freertos_idle_cb_t new_idle_cb,
                                                   UBaseType_t            cpuid );
