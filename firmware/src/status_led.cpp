// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file status_led.cpp
 * @ingroup status_led
 * @brief The WS2812 off frame, written on the RMT's registers, and the plain
 *        low level left behind it.
 *
 * No driver object, queue, encoder or interrupt (FW-10): the RMT is set up
 * through ESP-IDF's low-level layer (`hal/rmt_ll.h`), one `0` bit is written
 * into transmit channel 0's own RAM, and the channel sends it once per bit of
 * the frame in its loop mode, counted and stopped by the hardware
 * (`docs/architecture.md` SS 8). The end is polled. Nothing is allocated.
 */
#include "status_led.h"

#include "text.h"

#include <esp_private/periph_ctrl.h>
#include <esp_rom_sys.h>
#include <hal/gpio_ll.h>
#include <hal/rmt_ll.h>
#include <soc/gpio_sig_map.h>
#include <soc/io_mux_reg.h>

#include <cstddef>
#include <cstdint>

namespace kleidos::meter::led {

namespace {

/// The transmit channel the frame goes out on, and the one memory block it
/// owns (the first of the eight, `RMTMEM`'s first 48 words).
constexpr std::uint32_t kTxChannel = 0;

/// Words of RMT RAM per channel block (`SOC_RMT_MEM_WORDS_PER_CHANNEL`, 48 on
/// the ESP32-S3), and the blocks of the group (`RMT_LL_CHANS_PER_INST`).
constexpr std::size_t kRamWordsPerChannel = 48;
/// Blocks in the group's RAM, one per channel.
constexpr std::size_t kRamChannels = 8;
/// Words in the group's RAM.
constexpr std::size_t kRamWords = kRamChannels * kRamWordsPerChannel;

}  // namespace

}  // namespace kleidos::meter::led

/// The RMT's symbol RAM, eight blocks of 48 words. ESP-IDF gives its address
/// in the ESP32-S3's linker script (`esp32s3.peripherals.ld`:
/// `PROVIDE ( RMTMEM = 0x60016800 )`) and declares it only in its RMT
/// driver's private header, so the firmware declares the words itself; a
/// word is written whole, as the APB's non-FIFO access requires.
// The name is the linker script's (docs/misra.md D7).
// NOLINTNEXTLINE(readability-identifier-naming): the linker script's name (D7)
extern "C" volatile std::uint32_t RMTMEM[kleidos::meter::led::kRamWords];

namespace kleidos::meter::led {

namespace {

/// @brief Duration of a symbol half, in RMT ticks.
/// @param[in] nanoseconds The duration.
/// @return The ticks, truncated.
consteval std::uint32_t ticksFromNanoseconds( std::uint32_t nanoseconds ) noexcept {
    return nanoseconds / kNanosecondsPerTick;
}

static_assert( ticksFromNanoseconds( kT0HighNanoseconds ) == 6U, "T0H = 300 ns = 6 ticks" );
static_assert( ticksFromNanoseconds( kT0LowNanoseconds ) == 19U, "T0L = 950 ns = 19 ticks" );

/// @brief One RMT symbol as the RAM holds it: the first half's duration in
/// bits 0 to 14 and its level in bit 15, the second half's in bits 16 to 30
/// and 31 (`rmt_symbol_word_t`).
/// @param[in] ticks0 The first half's duration, in ticks.
/// @param[in] level0 The first half's level, 0 or 1.
/// @param[in] ticks1 The second half's duration, in ticks.
/// @param[in] level1 The second half's level, 0 or 1.
/// @return The word.
consteval std::uint32_t symbolWord( std::uint32_t ticks0, std::uint32_t level0,
                                    std::uint32_t ticks1, std::uint32_t level1 ) noexcept {
    return ( ticks0 & 0x7FFFU ) | ( ( level0 & 1U ) << 15U ) | ( ( ticks1 & 0x7FFFU ) << 16U )
           | ( ( level1 & 1U ) << 31U );
}

/// The `0` bit, high for T0H then low for T0L: the only symbol an off frame
/// emits. A `1` bit is never sent, so none is defined (status_led.h: the two
/// revisions of the part disagree on it).
constexpr std::uint32_t kZeroBit = symbolWord( ticksFromNanoseconds( kT0HighNanoseconds ), 1U,
                                               ticksFromNanoseconds( kT0LowNanoseconds ), 0U );

/// The end marker: a zero duration ends a pass through the RAM. In loop mode
/// the channel starts the next pass at the first word.
constexpr std::uint32_t kEndMarker = 0;

static_assert( kZeroBit == 0x00138006U, "6 ticks high, then 19 low" );

/// Bits in the frame: every color byte of every pixel, all zero. The channel
/// sends kZeroBit this many times, one pass of its RAM per bit.
constexpr std::uint32_t kFrameBits =
    static_cast<std::uint32_t>( kLedBlankPixels * kLedBytesPerPixel ) * 8U;

static_assert( kFrameBits == 96U, "four pixels of 24 bits" );
static_assert( kFrameBits <= static_cast<std::uint32_t>( RMT_LL_MAX_LOOP_COUNT_PER_BATCH ),
               "the hardware counts the whole frame in one batch" );

/// The RMT's source clock: the 80 MHz APB clock, which the ESP32-S3 keeps at
/// 80 MHz at every CPU frequency this image runs (240 MHz, no power
/// management in `sdkconfig.defaults`).
constexpr std::uint32_t kApbHz = 80000000;

/// The channel's divider from the group's clock (the APB's, undivided) to
/// kRmtResolutionHz.
constexpr std::uint32_t kChannelDivider = kApbHz / kRmtResolutionHz;

static_assert( kChannelDivider * kRmtResolutionHz == kApbHz, "the tick is a whole APB divider" );
static_assert( kChannelDivider >= 1U
                   && kChannelDivider
                          <= static_cast<std::uint32_t>( RMT_LL_CHANNEL_CLOCK_MAX_PRESCALE ),
               "the channel's divider register holds it" );

/// The loop's end in the raw interrupt status: the hardware raises it when it
/// has counted kFrameBits passes and stopped (loop auto-stop). Polled; no
/// interrupt is enabled.
constexpr std::uint32_t kLoopEndBit = static_cast<std::uint32_t>( RMT_LL_EVENT_TX_LOOP_END( 0 ) )
                                      << kTxChannel;

/// Every transmit event of the channel, cleared before a frame starts.
constexpr std::uint32_t kChannelEvents =
    static_cast<std::uint32_t>( RMT_LL_EVENT_TX_MASK( 0 ) | RMT_LL_EVENT_TX_ERROR( 0 ) )
    << kTxChannel;

/// How long a frame may take before the channel is stopped by hand: 96 bits
/// at 1.25 us are 120 us, and the poll waits up to eight times that, one
/// microsecond at a time.
constexpr std::uint32_t kFrameBudgetMicroseconds = 1000;

static_assert( kFrameBudgetMicroseconds * 1000U
                   > kFrameBits * ( kT0HighNanoseconds + kT0LowNanoseconds ),
               "the budget covers the frame" );

/// @brief Gates the RMT's bus clock, and on the way up pulses its reset,
/// under ESP-IDF's peripheral clock lock.
///
/// The parentheses around each LL function's name call the function itself
/// rather than the macro of the same name, whose only addition is to require
/// `PERIPH_RCC_ATOMIC()`'s scope (a `for` with a comma expression, MISRA's to
/// refuse); `periph_rcc_enter()` and `periph_rcc_exit()` are that scope's lock.
/// @param[in] on Whether the clock runs.
void setBusClock( bool on ) noexcept {
    periph_rcc_enter();
    ( rmt_ll_enable_bus_clock )( 0, on );
    if ( on ) {
        ( rmt_ll_reset_register )( 0 );
    }
    periph_rcc_exit();
}

/// @brief Brings the RMT up and sets channel 0 for the frame: the APB clock
/// divided to kRmtResolutionHz, one memory block holding kZeroBit and the end
/// marker, no carrier, the idle level fixed low, and loop mode stopped by the
/// hardware's count. No interrupt is enabled.
void configureChannel() noexcept {
    setBusClock( true );
    rmt_ll_mem_power_by_pmu( &RMT );
    rmt_ll_enable_mem_access_nonfifo( &RMT, true );
    rmt_ll_enable_interrupt( &RMT, UINT32_MAX, false );
    rmt_ll_clear_interrupt_status( &RMT, UINT32_MAX );
    rmt_ll_set_group_clock_src( &RMT, kTxChannel, RMT_CLK_SRC_APB, 1, 1, 0 );
    rmt_ll_enable_group_clock( &RMT, true );

    rmt_ll_tx_reset_channels_clock_div( &RMT, 1U << kTxChannel );
    rmt_ll_tx_set_channel_clock_div( &RMT, kTxChannel, kChannelDivider );
    rmt_ll_tx_set_mem_blocks( &RMT, kTxChannel, 1 );
    rmt_ll_tx_enable_carrier_modulation( &RMT, kTxChannel, false );
    rmt_ll_tx_fix_idle_level( &RMT, kTxChannel, 0, true );
    rmt_ll_tx_enable_wrap( &RMT, kTxChannel, false );
    rmt_ll_tx_enable_loop( &RMT, kTxChannel, true );
    rmt_ll_tx_enable_loop_autostop( &RMT, kTxChannel, true );

    const std::size_t first = static_cast<std::size_t>( kTxChannel ) * kRamWordsPerChannel;
    RMTMEM[first]           = kZeroBit;
    RMTMEM[first + 1U]      = kEndMarker;
}

/// @brief Takes the RMT down again: its group clock off, its RAM to low
/// power and its bus clock gated, as ESP-IDF's driver leaves it when its last
/// channel is deleted.
void releaseChannel() noexcept {
    rmt_ll_tx_enable_loop( &RMT, kTxChannel, false );
    rmt_ll_tx_enable_loop_count( &RMT, kTxChannel, false );
    rmt_ll_clear_interrupt_status( &RMT, kChannelEvents );
    rmt_ll_enable_group_clock( &RMT, false );
    rmt_ll_mem_force_low_power( &RMT );
    setBusClock( false );
}

/// @brief Waits for the hardware to have counted the frame's last bit.
/// @return true when the loop ended within kFrameBudgetMicroseconds.
bool frameSent() noexcept {
    for ( std::uint32_t waited = 0; waited < kFrameBudgetMicroseconds; ++waited ) {
        if ( ( rmt_ll_tx_get_interrupt_status_raw( &RMT, kTxChannel ) & kLoopEndBit ) != 0U ) {
            return true;
        }
        esp_rom_delay_us( 1 );
    }
    return false;
}

/// @brief Sends the off frame on one data line, then leaves it a plain
/// output driven low.
///
/// The pad is a GPIO-function output whose output the matrix takes from
/// channel 0; the channel idles low, which holds the leading reset code. It
/// counts kFrameBits passes of kZeroBit and stops; the idle level holds the
/// trailing reset code, which is what latches the zeros. Only then does the
/// pad go back to the GPIO's own output register, already low: routing the
/// pad away first would blank nothing, silently.
///
/// Both reset codes are busy-waits, not `vTaskDelay( 1 )`: a one-tick delay
/// blocks only until the next tick boundary, so its real length is anywhere
/// in (0, 1 ms] and can be a few microseconds, well under the >= 280 us the
/// WS2812B-V5 revision needs (status_led.h). This runs once per pin at boot,
/// before either task exists, so a 1 ms spin costs nothing.
/// @param[in] pin The data line.
void blankOne( gpio_num_t pin ) noexcept {
    const auto io = static_cast<std::uint32_t>( pin );
    gpio_ll_set_level( &GPIO, io, 0 );
    gpio_ll_func_sel( &GPIO, static_cast<std::uint8_t>( io ), PIN_FUNC_GPIO );
    gpio_ll_input_disable( &GPIO, io );
    gpio_ll_pullup_dis( &GPIO, io );
    gpio_ll_pulldown_dis( &GPIO, io );
    gpio_ll_od_disable( &GPIO, io );
    gpio_ll_set_output_enable_ctrl( &GPIO, static_cast<std::uint8_t>( io ), false, false );
    gpio_ll_set_output_signal_matrix_source(
        &GPIO, io, static_cast<std::uint32_t>( RMT_SIG_OUT0_IDX ) + kTxChannel, false );
    gpio_ll_output_enable( &GPIO, io );

    esp_rom_delay_us( kResetMicroseconds );

    rmt_ll_tx_reset_pointer( &RMT, kTxChannel );
    rmt_ll_tx_reset_loop_count( &RMT, kTxChannel );
    rmt_ll_tx_enable_loop_count( &RMT, kTxChannel, true );
    rmt_ll_tx_set_loop_count( &RMT, kTxChannel, kFrameBits );
    rmt_ll_clear_interrupt_status( &RMT, kChannelEvents );
    rmt_ll_tx_start( &RMT, kTxChannel );
    if ( !frameSent() ) {
        // A channel that never counted out: stop it, so the line is back at
        // the idle level for the trailing reset code.
        rmt_ll_tx_stop( &RMT, kTxChannel );
    }

    esp_rom_delay_us( kResetMicroseconds );

    gpio_ll_set_output_signal_matrix_source( &GPIO, io, SIG_GPIO_OUT_IDX, false );
}

}  // namespace

void blankAll() noexcept {
    configureChannel();
    for ( const gpio_num_t pin : kLedDataGpios ) {
        blankOne( pin );
    }
    releaseChannel();
}

void writeBlankedList( text::Writer& w ) noexcept {
    for ( std::size_t i = 0; i < kLedDataGpios.size(); ++i ) {
        w.text( ( i == 0U ) ? "" : "," );
        w.signedDecimal( static_cast<std::int64_t>( kLedDataGpios[i] ) );
    }
}

void writeReport( text::Writer& w ) noexcept {
    // One loop over the pins, in writeBlankedList(), for this report and for
    // the list `SYSTem:LED?` writes ahead of it, so the two cannot disagree
    // about which pins were blanked.
    w.text( "blanked=" );
    writeBlankedList( w );
    w.field( "pixels", kLedBlankPixels );
    w.field( "t0h_ns", kT0HighNanoseconds );
    w.field( "t0l_ns", kT0LowNanoseconds );
    w.field( "res_us", kResetMicroseconds );
    w.field( "held", "low" );
}

}  // namespace kleidos::meter::led
