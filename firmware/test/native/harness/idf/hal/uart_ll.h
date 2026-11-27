// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// UART0's low-level layer, as ESP-IDF 6.0.3's hal/uart_ll.h for the ESP32-S3
// offers it (components/esp_hal_uart/esp32s3/include), on the simulated
// board's UART0 (sim::World::uart, FW-12): the bus clock and reset, the clock
// source and divider, the frame, the two 128-byte FIFOs, the receive
// thresholds and the interrupt registers. The types and constants are the
// ones hal/uart_types.h, soc/clk_tree_defs.h and soc/uart_struct.h give it,
// with ESP-IDF's values. The functions the firmware does not call are not
// faked.
#include <cstdint>

/// UART0's register block (`uart_dev_t`, soc/uart_struct.h).
struct uart_dev_t {
    int unused;
};
extern uart_dev_t UART0;

typedef int uart_port_t;
#define UART_NUM_0 0

/// The clock sources a module can take (`soc_module_clk_t`, soc/clk_tree_defs.h).
typedef enum {
    SOC_MOD_CLK_APB  = 1,
    SOC_MOD_CLK_XTAL = 6,
} soc_module_clk_t;

typedef enum {
    UART_DATA_5_BITS = 0,
    UART_DATA_8_BITS = 3,
} uart_word_length_t;
typedef enum {
    UART_STOP_BITS_1 = 1,
} uart_stop_bits_t;
typedef enum {
    UART_PARITY_DISABLE = 0,
} uart_parity_t;
typedef enum {
    UART_HW_FLOWCTRL_DISABLE = 0,
} uart_hw_flowcontrol_t;

#define UART_LL_FIFO_DEF_LEN 128
#define UART_LL_INTR_MASK ( 0x7ffff )

typedef enum {
    UART_INTR_RXFIFO_FULL  = ( 0x1 << 0 ),
    UART_INTR_TXFIFO_EMPTY = ( 0x1 << 1 ),
    UART_INTR_RXFIFO_OVF   = ( 0x1 << 4 ),
    UART_INTR_RXFIFO_TOUT  = ( 0x1 << 8 ),
} uart_intr_t;

/// The bus clock (a macro on the device, which requires PERIPH_RCC_ATOMIC()'s
/// scope; the harness counts a call outside periph_rcc_enter()).
void uart_ll_enable_bus_clock( uart_port_t uart_num, bool enable );
/// The module's reset, pulsed (the same lock).
void uart_ll_reset_register( uart_port_t uart_num );
void uart_ll_set_sclk( uart_dev_t* hw, soc_module_clk_t source_clk );
/// The divider for @p baud from @p sclk_freq; false when no divider makes it.
bool _uart_ll_set_baudrate( uart_dev_t* hw, std::uint32_t baud, std::uint32_t sclk_freq );
#define uart_ll_set_baudrate( ... ) _uart_ll_set_baudrate( __VA_ARGS__ )
void uart_ll_set_mode_normal( uart_dev_t* hw );
void uart_ll_set_data_bit_num( uart_dev_t* hw, uart_word_length_t data_bit );
void uart_ll_set_parity( uart_dev_t* hw, uart_parity_t parity_mode );
void uart_ll_set_stop_bits( uart_dev_t* hw, uart_stop_bits_t stop_bit );
void uart_ll_set_hw_flow_ctrl( uart_dev_t* hw, uart_hw_flowcontrol_t flow_ctrl,
                               std::uint32_t rx_thrs );
void uart_ll_set_tx_idle_num( uart_dev_t* hw, std::uint32_t idle_num );
void uart_ll_rxfifo_rst( uart_dev_t* hw );
void uart_ll_txfifo_rst( uart_dev_t* hw );
void uart_ll_set_rxfifo_full_thr( uart_dev_t* hw, std::uint16_t full_thrhd );
/// The receive time-out in bit times; 0 turns it off.
void uart_ll_set_rx_tout( uart_dev_t* hw, std::uint16_t tout_thrd );
void uart_ll_ena_intr_mask( uart_dev_t* hw, std::uint32_t mask );
void uart_ll_disable_intr_mask( uart_dev_t* hw, std::uint32_t mask );
/// The raw status of the enabled interrupts.
std::uint32_t uart_ll_get_intsts_mask( uart_dev_t* hw );
void          uart_ll_clr_intsts_mask( uart_dev_t* hw, std::uint32_t mask );
/// The bytes the receive FIFO holds.
std::uint32_t uart_ll_get_rxfifo_len( uart_dev_t* hw );
void          uart_ll_read_rxfifo( uart_dev_t* hw, std::uint8_t* buf, std::uint32_t rd_len );
/// The room the transmit FIFO has.
std::uint32_t uart_ll_get_txfifo_len( uart_dev_t* hw );
void          uart_ll_write_txfifo( uart_dev_t* hw, const std::uint8_t* buf, std::uint32_t wr_len );
