// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The brownout detector's HAL, as ESP-IDF 6.0.3's hal/brownout_hal.h offers
// it (components/esp_hal_pmu/include), on the simulated board's detector
// (sim::World::brownout): the one call the firmware makes records the
// configuration it was given (FW-18).
#include <cstdint>

/// The detector's configuration (`brownout_hal_config_t`).
struct brownout_hal_config_t {
    std::uint8_t threshold;
    bool         enabled;
    bool         reset_enabled;
    bool         flash_power_down;
    bool         rf_power_down;
};

/// Writes the configuration to the detector.
void brownout_hal_config( const brownout_hal_config_t* cfg );
