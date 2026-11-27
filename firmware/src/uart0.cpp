// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file uart0.cpp
 * @ingroup console_uart
 * @brief UART0 set through its low-level layer, its receive interrupt and
 *        ring, and the polled transmit.
 *
 * No driver object, queue, ring buffer of ESP-IDF's or allocated interrupt
 * (FW-12): the port is set through `hal/uart_ll.h` and `hal/gpio_ll.h`, its
 * interrupt is routed by interrupt.cpp, and the ring, its indices, the two
 * flags and the semaphore are one static object in internal RAM.
 */
#include "uart0.h"

#include "chars.h"
#include "interrupt.h"

#include <esp_attr.h>
#include <esp_private/periph_ctrl.h>
#include <esp_timer.h>
#include <freertos/semphr.h>
#include <hal/gpio_ll.h>
#include <hal/uart_ll.h>
#include <soc/gpio_sig_map.h>
#include <soc/interrupts.h>
#include <soc/uart_pins.h>

#include <algorithm>
#include <array>

namespace kleidos::meter::uart0 {

namespace {

/// The receive interrupts the handler serves: the FIFO above its threshold,
/// the line idle after a byte, and the FIFO's overflow.
constexpr std::uint32_t kRxInterrupts = static_cast<std::uint32_t>( UART_INTR_RXFIFO_FULL )
                                        | static_cast<std::uint32_t>( UART_INTR_RXFIFO_TOUT )
                                        | static_cast<std::uint32_t>( UART_INTR_RXFIFO_OVF );

/// The two that say data is waiting, turned off while the ring is full. The
/// overflow stays on, so a loss is still seen.
constexpr std::uint32_t kRxDataInterrupts = static_cast<std::uint32_t>( UART_INTR_RXFIFO_FULL )
                                            | static_cast<std::uint32_t>( UART_INTR_RXFIFO_TOUT );

/// The ring's index mask: an index modulo kRxRingBytes.
constexpr std::uint32_t kRingMask = static_cast<std::uint32_t>( kRxRingBytes ) - 1U;

/// The ring, its two free-running indices, the two flags and the semaphore,
/// in one object reached through a function so that no variable has
/// namespace scope (docs/misra.md, rule 6.7.2). Plain fields: the handler
/// runs on the console's core, and the task side touches them only with that
/// core's interrupts masked (uart0.h).
struct State {
    std::array<char, kRxRingBytes> ring{};        ///< The received bytes.
    std::uint32_t                  head = 0;      ///< Bytes put into the ring, ever.
    std::uint32_t                  tail = 0;      ///< Bytes taken from it, ever.
    bool                           held = false;  ///< The ring filled: the data interrupts are off.
    bool              overrun = false;    ///< The FIFO overflowed since takeOverrun() last looked.
    SemaphoreHandle_t rxReady = nullptr;  ///< Given by the handler when it put bytes in.
    StaticSemaphore_t rxReadyBuffer{};    ///< The semaphore's static storage.
};

/// @brief The one State.
/// @return The ring, its indices, the flags and the semaphore.
IRAM_ATTR State& state() noexcept {
    static constinit State instance;
    return instance;
}

/// @brief Moves what the receive FIFO holds into the ring, as far as the ring
///        has room. In IRAM: the handler calls it.
/// @param[in,out] own The state.
/// @retval true  The FIFO is empty.
/// @retval false The ring is full and bytes are still in the FIFO.
IRAM_ATTR bool drainFifo( State& own ) noexcept {
    const std::uint32_t waiting = uart_ll_get_rxfifo_len( &UART0 );
    const std::uint32_t room = static_cast<std::uint32_t>( kRxRingBytes ) - ( own.head - own.tail );
    const std::uint32_t moved = std::min( waiting, room );
    for ( std::uint32_t i = 0; i < moved; ++i ) {
        std::uint8_t byte = 0;
        uart_ll_read_rxfifo( &UART0, &byte, 1 );
        own.ring[own.head & kRingMask] = chars::fromByte( byte );
        ++own.head;
    }
    return moved == waiting;
}

/// @brief UART0's interrupt: latches an overflow (and empties the FIFO, as
///        ESP-IDF's driver did), moves the FIFO into the ring, turns the data
///        interrupts off when the ring is full, clears what it served, and
///        gives the semaphore when it put bytes in.
///
/// The line is level-triggered, and every bit it reads is cleared before it
/// returns; the FIFO's threshold is a level that the drain itself lowers.
/// @param[in] context Unused.
IRAM_ATTR void onInterrupt( void* context ) noexcept {
    (void)context;
    State&              own     = state();
    const std::uint32_t pending = uart_ll_get_intsts_mask( &UART0 );
    if ( ( pending & static_cast<std::uint32_t>( UART_INTR_RXFIFO_OVF ) ) != 0U ) {
        own.overrun = true;
        uart_ll_rxfifo_rst( &UART0 );
    }
    const std::uint32_t before = own.head;
    if ( !drainFifo( own ) ) {
        uart_ll_disable_intr_mask( &UART0, kRxDataInterrupts );
        own.held = true;
    }
    uart_ll_clr_intsts_mask( &UART0, pending );
    if ( own.head != before ) {
        BaseType_t higherPriorityWoken = pdFALSE;
        (void)xSemaphoreGiveFromISR( own.rxReady, &higherPriorityWoken );
        if ( higherPriorityWoken == pdTRUE ) {
            portYIELD_FROM_ISR();
        }
    }
}

/// @brief Copies up to @p capacity bytes out of the ring, and refills it from
///        the FIFO when it had filled, with this core's interrupts masked.
/// @param[in,out] own      The state.
/// @param[out]    out      Where the bytes go.
/// @param[in]     capacity The room at @p out.
/// @return The bytes copied.
std::size_t take( State& own, char* out, std::size_t capacity ) noexcept {
    const UBaseType_t   masked = portSET_INTERRUPT_MASK_FROM_ISR();
    const std::uint32_t queued = own.head - own.tail;
    const std::size_t   count  = std::min( static_cast<std::size_t>( queued ), capacity );
    for ( std::size_t i = 0; i < count; ++i ) {
        out[i] = own.ring[own.tail & kRingMask];
        ++own.tail;
    }
    // What the handler left in the FIFO goes into the room just made; the data
    // interrupts come back on only once nothing is left there, or a byte that
    // stays below the threshold with its time-out already served would wait
    // for the next one.
    if ( own.held ) {
        if ( drainFifo( own ) ) {
            own.held = false;
            uart_ll_ena_intr_mask( &UART0, kRxDataInterrupts );
        }
    }
    portCLEAR_INTERRUPT_MASK_FROM_ISR( masked );
    return count;
}

/// @brief Gates UART0's bus clock on and pulses its reset, under ESP-IDF's
///        peripheral clock lock.
///
/// The parentheses around each LL function's name call the function itself
/// rather than the macro of the same name, whose only addition is to require
/// `PERIPH_RCC_ATOMIC()`'s scope (a `for` with a comma expression, MISRA's to
/// refuse); `periph_rcc_enter()` and `periph_rcc_exit()` are that scope's lock.
void resetPort() noexcept {
    periph_rcc_enter();
    ( uart_ll_enable_bus_clock )( UART_NUM_0, true );
    ( uart_ll_reset_register )( UART_NUM_0 );
    periph_rcc_exit();
}

/// @brief Gives GPIO43 and GPIO44 to UART0 through the IO MUX, as ESP-IDF's
///        driver gives a port its default pins (`gpio_iomux_output()`,
///        `gpio_iomux_input()`): TX's output enable is then the peripheral's,
///        and RX's signal bypasses the GPIO matrix.
void connectPins() noexcept {
    gpio_ll_func_sel( &GPIO, static_cast<std::uint8_t>( kTxGpio ), U0TXD_MUX_FUNC );
    gpio_ll_set_input_signal_from( &GPIO, U0RXD_IN_IDX, false );
    gpio_ll_input_enable( &GPIO, kRxGpio );
    gpio_ll_func_sel( &GPIO, static_cast<std::uint8_t>( kRxGpio ), U0RXD_MUX_FUNC );
}

static_assert( kTxGpio == static_cast<std::uint32_t>( U0TXD_GPIO_NUM )
                   && kRxGpio == static_cast<std::uint32_t>( U0RXD_GPIO_NUM ),
               "the console's pins are UART0's IO MUX pins" );

}  // namespace

esp_err_t begin( std::uint32_t baud ) noexcept {
    State& own  = state();
    own.head    = 0;
    own.tail    = 0;
    own.held    = false;
    own.overrun = false;

    resetPort();
    uart_ll_set_sclk( &UART0, SOC_MOD_CLK_APB );
    if ( !uart_ll_set_baudrate( &UART0, baud, kSourceClockHz ) ) {
        return ESP_ERR_INVALID_ARG;
    }
    uart_ll_set_mode_normal( &UART0 );
    uart_ll_set_data_bit_num( &UART0, UART_DATA_8_BITS );
    uart_ll_set_parity( &UART0, UART_PARITY_DISABLE );
    uart_ll_set_stop_bits( &UART0, UART_STOP_BITS_1 );
    uart_ll_set_hw_flow_ctrl( &UART0, UART_HW_FLOWCTRL_DISABLE, 0 );
    uart_ll_set_tx_idle_num( &UART0, 0 );
    connectPins();
    uart_ll_rxfifo_rst( &UART0 );
    uart_ll_txfifo_rst( &UART0 );
    uart_ll_set_rxfifo_full_thr( &UART0, static_cast<std::uint16_t>( kRxFullThreshold ) );
    uart_ll_set_rx_tout( &UART0, static_cast<std::uint16_t>( kRxTimeoutBits ) );
    uart_ll_disable_intr_mask( &UART0, UART_LL_INTR_MASK );
    uart_ll_clr_intsts_mask( &UART0, UART_LL_INTR_MASK );

    own.rxReady = xSemaphoreCreateBinaryStatic( &own.rxReadyBuffer );
    if ( own.rxReady == nullptr ) {
        return ESP_ERR_NO_MEM;
    }
    interrupt::Route route;
    const esp_err_t  routed = interrupt::routeHere(
        static_cast<std::uint32_t>( ETS_UART0_INTR_SOURCE ), &onInterrupt, nullptr, route );
    if ( routed != ESP_OK ) {
        return routed;
    }
    uart_ll_ena_intr_mask( &UART0, kRxInterrupts );
    return ESP_OK;
}

std::size_t read( char* out, std::size_t capacity, TickType_t wait ) noexcept {
    State& own = state();
    if ( out == nullptr || capacity == 0U ) {
        return 0;
    }
    // A give left for bytes an earlier call already took is forgotten first,
    // so that the wait below is for bytes that come after this look.
    (void)xSemaphoreTake( own.rxReady, 0 );
    std::size_t count = take( own, out, capacity );
    if ( count == 0U && wait > 0U ) {
        (void)xSemaphoreTake( own.rxReady, wait );
        count = take( own, out, capacity );
    }
    return count;
}

std::size_t write( const char* data, std::size_t length ) noexcept {
    if ( data == nullptr ) {
        return 0;
    }
    std::size_t  placed      = 0;
    std::int64_t fullSinceUs = -1;  // when the FIFO was first seen full, -1 while it has room
    while ( placed < length ) {
        const std::uint32_t room = uart_ll_get_txfifo_len( &UART0 );
        if ( room > 0U ) {
            const std::size_t chunk = std::min( static_cast<std::size_t>( room ), length - placed );
            for ( std::size_t i = 0; i < chunk; ++i ) {
                const std::uint8_t byte = chars::byteOf( data[placed + i] );
                uart_ll_write_txfifo( &UART0, &byte, 1 );
            }
            placed += chunk;
            fullSinceUs = -1;
        } else {
            const std::int64_t now = esp_timer_get_time();
            if ( fullSinceUs < 0 ) {
                fullSinceUs = now;
            } else if ( now - fullSinceUs >= kTxStallMicroseconds ) {
                break;  // a line that takes nothing: the caller counts the rest
            } else {
                // Still full, within the stall bound: keep polling.
            }
        }
    }
    return placed;
}

bool takeOverrun() noexcept {
    State&            own    = state();
    const UBaseType_t masked = portSET_INTERRUPT_MASK_FROM_ISR();
    const bool        lost   = own.overrun;
    own.overrun              = false;
    portCLEAR_INTERRUPT_MASK_FROM_ISR( masked );
    return lost;
}

}  // namespace kleidos::meter::uart0
