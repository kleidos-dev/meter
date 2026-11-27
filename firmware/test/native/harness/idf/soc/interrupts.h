// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The ESP32-S3's interrupt matrix sources the firmware and ESP-IDF's startup route, numbered as
// ESP-IDF 6.0.3's soc/esp32s3/include/soc/interrupts.h numbers them.
typedef enum {
    ETS_GPIO_INTR_SOURCE             = 16,  ///< The GPIO's interrupt, level.
    ETS_UART0_INTR_SOURCE            = 27,  ///< UART0's interrupt, level.
    ETS_SYSTIMER_TARGET0_INTR_SOURCE = 57,  ///< The system timer's alarm 0: Core 0's tick.
    ETS_SYSTIMER_TARGET1_INTR_SOURCE = 58,  ///< The system timer's alarm 1: Core 1's tick.
    ETS_FROM_CPU_INTR0_SOURCE        = 79,  ///< Core 0's cross-core interrupt.
    ETS_FROM_CPU_INTR1_SOURCE        = 80,  ///< Core 1's cross-core interrupt.
} periph_interrupt_t;
