// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
// The meter's sdkconfig.defaults, as far as the sources read it.
#pragma once
#define CONFIG_FREERTOS_HZ 1000
#define CONFIG_RTC_CLK_SRC_INT_RC 1
#define CONFIG_ESP_MAIN_TASK_STACK_SIZE 8192
#define CONFIG_ESP_IPC_TASK_STACK_SIZE 4096
#define CONFIG_ESP_MAIN_TASK_AFFINITY 0x0
