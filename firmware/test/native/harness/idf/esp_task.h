// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The main task's stack, as ESP-IDF 6.0.3's esp_task.h sizes it.
#include "sdkconfig.h"

/// What ESP-IDF adds to a system task's configured stack (no stack
/// overflow guard by canary on this build).
#define TASK_EXTRA_STACK_SIZE ( 512 )
/// The main task's stack, in bytes.
#define ESP_TASK_MAIN_STACK ( CONFIG_ESP_MAIN_TASK_STACK_SIZE + TASK_EXTRA_STACK_SIZE )
