// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The shared peripheral modules ESP-IDF 6.0.3's clock control counts
// references to (components/soc/esp32s3/include/soc/periph_defs.h), as far as
// the firmware names them.

/// A peripheral module whose bus clock ESP-IDF reference-counts.
typedef enum {
    PERIPH_TIMG0_MODULE = 3,
    PERIPH_TIMG1_MODULE = 4,
    PERIPH_MODULE_MAX   = 12,
} shared_periph_module_t;
