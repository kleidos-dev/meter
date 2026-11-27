// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// I2C0's low-level layer, as ESP-IDF 6.0.3's hal/i2c_ll.h for the ESP32-S3
// offers it (components/esp_hal_i2c/esp32s3/include), on the simulated
// board's controller (sim::World::i2c, FW-13): the bus clock and reset, the
// controller's configuration, the timing registers and their latch, the two
// 32-byte FIFOs, the eight command registers, the interrupt status, the bus's
// busy flag and the bus clear's pulses. The command type, the opcodes, the
// status bits and the constants are ESP-IDF's, with its values. The timing
// arithmetic (i2c_ll_master_cal_bus_clk(), i2c_ll_calculate_timeout_us_to_reg_val())
// is ESP-IDF's, restated. The functions the firmware does not call are not
// faked.
#include "hal/i2c_types.h"

#include <cstdint>

/// I2C0's register block (`i2c_dev_t`, soc/i2c_struct.h).
struct i2c_dev_t {
    int unused;
};
extern i2c_dev_t I2C0;

#define I2C_LL_FIFO_LEN 32
#define I2C_LL_CMD_REG_NUM 8
#define I2C_LL_RESET_SLV_SCL_PULSE_NUM_DEFAULT ( 9 )
#define I2C_LL_INTR_MASK ( 0x3fff )

/// One command register's fields (i2c_ll_hw_cmd_t, a union of bit-fields and
/// the register word on the device): the firmware writes the fields by name,
/// and i2c_ll_master_write_cmd_reg() packs them at the hardware's bits.
struct i2c_ll_hw_cmd_t {
    std::uint32_t byte_num = 0;  ///< Bits 0-7: the bytes the command moves.
    std::uint32_t ack_en   = 0;  ///< Bit 8: check each byte written is acknowledged.
    std::uint32_t ack_exp  = 0;  ///< Bit 9: the level expected.
    std::uint32_t ack_val  = 0;  ///< Bit 10: the level sent after a byte read.
    std::uint32_t op_code  = 0;  ///< Bits 11-13: the command.
};

#define I2C_LL_CMD_RESTART 6
#define I2C_LL_CMD_WRITE 1
#define I2C_LL_CMD_READ 3
#define I2C_LL_CMD_STOP 2
#define I2C_LL_CMD_END 4

typedef enum {
    I2C_LL_INTR_NACK         = ( 1 << 10 ),
    I2C_LL_INTR_TIMEOUT      = ( 1 << 8 ),
    I2C_LL_INTR_MST_COMPLETE = ( 1 << 7 ),
    I2C_LL_INTR_ARBITRATION  = ( 1 << 5 ),
    I2C_LL_INTR_END_DETECT   = ( 1 << 3 ),
} i2c_ll_master_intr_t;

/// ESP-IDF's arithmetic for the timing of @p bus_freq from @p source_clk.
void i2c_ll_master_cal_bus_clk( std::uint32_t source_clk, std::uint32_t bus_freq,
                                i2c_hal_clk_config_t* clk_cal );
/// The time-out register's value for @p timeout_us (2^value source cycles).
std::uint32_t i2c_ll_calculate_timeout_us_to_reg_val( std::uint32_t src_clk_hz,
                                                      std::uint32_t timeout_us );

/// The bus clock and the reset (macros on the device, which require
/// PERIPH_RCC_ATOMIC()'s scope; the harness counts a call outside
/// periph_rcc_enter()).
void i2c_ll_enable_bus_clock( int i2c_port, bool enable );
void i2c_ll_reset_register( int i2c_port );

void i2c_ll_set_source_clk( i2c_dev_t* hw, i2c_clock_source_t src_clk );
void i2c_ll_enable_controller_clock( i2c_dev_t* hw, bool en );
void i2c_ll_set_mode( i2c_dev_t* hw, i2c_bus_mode_t mode );
void i2c_ll_enable_pins_open_drain( i2c_dev_t* hw, bool enable_od );
void i2c_ll_enable_arbitration( i2c_dev_t* hw, bool enable_arbi );
void i2c_ll_master_rx_full_ack_level( i2c_dev_t* hw, int ack_level );
void i2c_ll_set_data_mode( i2c_dev_t* hw, i2c_trans_mode_t tx_mode, i2c_trans_mode_t rx_mode );
void i2c_ll_enable_fifo_mode( i2c_dev_t* hw, bool fifo_mode_en );
/// The timing registers, staged until i2c_ll_update().
void i2c_ll_master_set_bus_timing( i2c_dev_t* hw, i2c_hal_clk_config_t* bus_cfg );
void i2c_ll_set_tout( i2c_dev_t* hw, int tout );
void i2c_ll_master_set_fractional_divider( i2c_dev_t* hw, std::uint8_t div_a, std::uint8_t div_b );
void i2c_ll_master_set_filter( i2c_dev_t* hw, std::uint8_t filter_num );
/// Latches the staged configuration into the controller's clock domain.
void i2c_ll_update( i2c_dev_t* hw );

void i2c_ll_txfifo_rst( i2c_dev_t* hw );
void i2c_ll_rxfifo_rst( i2c_dev_t* hw );
void i2c_ll_write_txfifo( i2c_dev_t* hw, const std::uint8_t* ptr, std::uint8_t len );
void i2c_ll_read_rxfifo( i2c_dev_t* hw, std::uint8_t* ptr, std::uint8_t len );
void i2c_ll_master_write_cmd_reg( i2c_dev_t* hw, i2c_ll_hw_cmd_t cmd, int cmd_idx );
/// Runs the command list from register 0.
void i2c_ll_start_trans( i2c_dev_t* hw );

void i2c_ll_enable_intr_mask( i2c_dev_t* hw, std::uint32_t mask );
void i2c_ll_disable_intr_mask( i2c_dev_t* hw, std::uint32_t mask );
void i2c_ll_clear_intr_mask( i2c_dev_t* hw, std::uint32_t mask );
/// The raw status of the enabled interrupts.
void i2c_ll_get_intr_mask( i2c_dev_t* hw, std::uint32_t* intr_status );
bool i2c_ll_is_bus_busy( i2c_dev_t* hw );
/// Clocks @p slave_pulses SCL pulses while the master is idle.
void i2c_ll_master_clr_bus( i2c_dev_t* hw, std::uint32_t slave_pulses, bool enable );
