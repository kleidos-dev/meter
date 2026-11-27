// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
// The RMT's low-level layer, as ESP-IDF 6.0.3's hal/rmt_ll.h for the ESP32-S3
// offers it (components/esp_hal_rmt/esp32s3/include/hal/rmt_ll.h), on the
// simulated board's RMT (sim::World::rmt): the bus clock and reset, the
// group's clock and memory, transmit channel 0 to 3's configuration, the
// interrupt enable and raw status, and the transmission itself, which the
// model plays from RMTMEM onto the pads the matrix gives the channel to.
// The functions the firmware does not call are not faked.
#include <cstdint>

/// The RMT's register block (`RMT`, soc/rmt_struct.h).
struct rmt_dev_t {
    int unused;
};
extern rmt_dev_t RMT;

/// The group's source clocks (soc/clk_tree_defs.h), as named there.
typedef enum {
    RMT_CLK_SRC_APB     = 1,
    RMT_CLK_SRC_RC_FAST = 2,
    RMT_CLK_SRC_XTAL    = 3,
} rmt_clock_source_t;

#define RMT_LL_EVENT_TX_DONE( channel ) ( 1 << ( channel ) )
#define RMT_LL_EVENT_TX_THRES( channel ) ( 1 << ( ( channel ) + 8 ) )
#define RMT_LL_EVENT_TX_LOOP_END( channel ) ( 1 << ( ( channel ) + 12 ) )
#define RMT_LL_EVENT_TX_ERROR( channel ) ( 1 << ( ( channel ) + 4 ) )
#define RMT_LL_EVENT_TX_MASK( channel )                                                            \
    ( RMT_LL_EVENT_TX_DONE( channel ) | RMT_LL_EVENT_TX_THRES( channel )                           \
      | RMT_LL_EVENT_TX_LOOP_END( channel ) )
#define RMT_LL_MAX_LOOP_COUNT_PER_BATCH 1023
#define RMT_LL_CHANNEL_CLOCK_MAX_PRESCALE 256

// The two the device wraps in a macro of the same name that requires
// PERIPH_RCC_ATOMIC()'s scope; here they are plain functions, and each
// counts a call made outside periph_rcc_enter() (sim::Rmt::rccViolations).
void rmt_ll_enable_bus_clock( int group_id, bool enable );
void rmt_ll_reset_register( int group_id );

void rmt_ll_mem_force_power_on( rmt_dev_t* dev );
void rmt_ll_mem_force_low_power( rmt_dev_t* dev );
void rmt_ll_mem_power_by_pmu( rmt_dev_t* dev );
void rmt_ll_enable_mem_access_nonfifo( rmt_dev_t* dev, bool enable );
void rmt_ll_set_group_clock_src( rmt_dev_t* dev, std::uint32_t channel, rmt_clock_source_t src,
                                 std::uint32_t divider_integral, std::uint32_t divider_denominator,
                                 std::uint32_t divider_numerator );
void rmt_ll_enable_group_clock( rmt_dev_t* dev, bool en );
void rmt_ll_enable_interrupt( rmt_dev_t* dev, std::uint32_t mask, bool enable );
void rmt_ll_clear_interrupt_status( rmt_dev_t* dev, std::uint32_t mask );
std::uint32_t rmt_ll_tx_get_interrupt_status_raw( rmt_dev_t* dev, std::uint32_t channel );

void rmt_ll_tx_reset_channels_clock_div( rmt_dev_t* dev, std::uint32_t channel_mask );
void rmt_ll_tx_set_channel_clock_div( rmt_dev_t* dev, std::uint32_t channel, std::uint32_t div );
void rmt_ll_tx_reset_pointer( rmt_dev_t* dev, std::uint32_t channel );
void rmt_ll_tx_start( rmt_dev_t* dev, std::uint32_t channel );
void rmt_ll_tx_stop( rmt_dev_t* dev, std::uint32_t channel );
void rmt_ll_tx_set_mem_blocks( rmt_dev_t* dev, std::uint32_t channel, std::uint8_t block_num );
void rmt_ll_tx_enable_wrap( rmt_dev_t* dev, std::uint32_t channel, bool enable );
void rmt_ll_tx_enable_loop( rmt_dev_t* dev, std::uint32_t channel, bool enable );
void rmt_ll_tx_set_loop_count( rmt_dev_t* dev, std::uint32_t channel, std::uint32_t count );
void rmt_ll_tx_reset_loop_count( rmt_dev_t* dev, std::uint32_t channel );
void rmt_ll_tx_enable_loop_count( rmt_dev_t* dev, std::uint32_t channel, bool enable );
void rmt_ll_tx_enable_loop_autostop( rmt_dev_t* dev, std::uint32_t channel, bool enable );
void rmt_ll_tx_fix_idle_level( rmt_dev_t* dev, std::uint32_t channel, std::uint8_t level,
                               bool enable );
void rmt_ll_tx_enable_carrier_modulation( rmt_dev_t* dev, std::uint32_t channel, bool enable );
