// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file brownout.cpp
 * @ingroup brownout_detector
 * @brief The detector's one configuration, written through the HAL.
 */
#include "brownout.h"

#include <hal/brownout_hal.h>

namespace kleidos::meter::brownout {

void arm() noexcept {
    // ESP-IDF's own configuration for a detector without an interrupt
    // (esp_brownout_init(), the `#else` of CONFIG_ESP_BROWNOUT_USE_INTR).
    brownout_hal_config_t config{};
    config.threshold        = kThreshold;
    config.enabled          = true;
    config.reset_enabled    = true;
    config.flash_power_down = true;
    config.rf_power_down    = true;
    brownout_hal_config( &config );
}

}  // namespace kleidos::meter::brownout
