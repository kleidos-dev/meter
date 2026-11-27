// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The I2C types ESP-IDF 6.0.3's hal/i2c_types.h gives the ESP32-S3
// (components/esp_hal_i2c/include), with ESP-IDF's values where the firmware
// or the harness compares them: the clock sources (soc/clk_tree_defs.h's
// soc_periph_i2c_clk_src_t), the bus mode, the bit order, and the structure
// i2c_ll_master_cal_bus_clk() fills.
#include <cstdint>

/// The controller's clock sources: the crystal (SOC_MOD_CLK_XTAL) and the
/// RC oscillator (SOC_MOD_CLK_RC_FAST); the default is the crystal.
typedef enum {
    I2C_CLK_SRC_XTAL    = 6,
    I2C_CLK_SRC_RC_FAST = 8,
    I2C_CLK_SRC_DEFAULT = 6,
} i2c_clock_source_t;

typedef enum {
    I2C_BUS_MODE_MASTER = 0,
    I2C_BUS_MODE_SLAVE  = 1,
} i2c_bus_mode_t;

typedef enum {
    I2C_DATA_MODE_MSB_FIRST = 0,
    I2C_DATA_MODE_LSB_FIRST = 1,
} i2c_trans_mode_t;

/// The bus timing, in source-clock cycles (i2c_hal_clk_config_t).
typedef struct {
    std::uint16_t clkm_div;
    std::uint16_t scl_low;
    std::uint16_t scl_high;
    std::uint16_t scl_wait_high;
    std::uint16_t sda_hold;
    std::uint16_t sda_sample;
    std::uint16_t setup;
    std::uint16_t hold;
    std::uint16_t tout;
} i2c_hal_clk_config_t;
