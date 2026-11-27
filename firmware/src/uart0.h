// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file uart0.h
 * @ingroup console_uart
 * @brief The console's line: UART0 on its registers, a static receive ring
 *        filled by an interrupt routed without the allocator, and a polled
 *        transmit.
 *
 * ESP-IDF's UART driver kept its object, its receive ring buffer, its event
 * queue, its mutexes and its interrupt's records on the heap (docs/misra.md
 * D1). This module does what the console needed of it with nothing allocated
 * (FW-12):
 *
 *  - **The port** is UART0 on GPIO43 (TX) and GPIO44 (RX), the pins the
 *    carrier's CH343P bridge is wired to, reached through the IO MUX as
 *    ESP-IDF's driver reaches a port's default pins. begin() sets it through
 *    `hal/uart_ll.h`: its bus clock and reset under ESP-IDF's peripheral
 *    clock lock, the 80 MHz APB clock divided to the baud rate, 8 data bits,
 *    no parity, one stop bit, no flow control, and both FIFOs emptied.
 *  - **The receive side** is an interrupt (FIFO above kRxFullThreshold, a
 *    receive time-out of kRxTimeoutBits, a FIFO overflow) routed by
 *    interrupt::routeHere() to a level-1 line of the calling core, Core 0's.
 *    Its handler moves what the FIFO holds into a static ring of
 *    kRxRingBytes and gives a static binary semaphore, on which read() waits.
 *    A full ring leaves the rest in the hardware FIFO and turns the data
 *    interrupts off until read() has made room, as ESP-IDF's driver did; only
 *    a FIFO overflow loses bytes, and it is latched for takeOverrun(), which
 *    the console turns into `-363`.
 *  - **The transmit side** is polled: write() puts each byte into the
 *    hardware FIFO as it has room, and gives up on a line that takes nothing
 *    for kTxStallMicroseconds, returning what it placed, so a caller counts a
 *    short write instead of blocking for ever.
 *
 * **One core.** The ring and the two flags are plain variables: the handler
 * runs on the core that called begin(), and read() and takeOverrun() touch
 * them only with that core's interrupts masked. Every caller is the console
 * task, pinned to Core 0, where `app_main()` calls begin(). The handler and
 * what it calls are in IRAM: a line no allocator registered stays enabled
 * while the flash is written (ESP-IDF masks only the lines its allocator
 * gave a non-IRAM handler), so it must run with the cache off.
 */
#ifndef KLEIDOS_METER_UART0_H
#define KLEIDOS_METER_UART0_H

#include <esp_err.h>
#include <freertos/FreeRTOS.h>

#include <cstddef>
#include <cstdint>

namespace kleidos::meter::uart0 {

/// @addtogroup console_uart
/// @{

/// @brief UART0's TX pad, to the CH343P's RXD (`docs/carrier-board.md` SS 7):
///        the port's IO MUX pin on the ESP32-S3 (`U0TXD_GPIO_NUM`).
constexpr std::uint32_t kTxGpio = 43;

/// @brief UART0's RX pad, from the CH343P's TXD: the port's IO MUX pin
///        (`U0RXD_GPIO_NUM`).
constexpr std::uint32_t kRxGpio = 44;

/// @brief The clock the baud-rate divider divides: the APB's 80 MHz, which
///        the ESP32-S3 keeps at every CPU frequency this image runs (no power
///        management in `sdkconfig.defaults`), and which ESP-IDF's driver
///        chose for the port by default (`UART_SCLK_DEFAULT`).
constexpr std::uint32_t kSourceClockHz = 80000000;

/// @brief Each hardware FIFO's depth (`SOC_UART_FIFO_LEN`).
constexpr std::uint32_t kFifoBytes = 128;

/// @brief The receive ring: what the interrupt has taken from the FIFO and
///        read() not yet. 1 KiB, the receive ring ESP-IDF's driver was given;
///        a power of two, so that the free-running indices wrap with it.
constexpr std::size_t kRxRingBytes = 1024;

static_assert( ( kRxRingBytes & ( kRxRingBytes - 1U ) ) == 0U, "the ring is a power of two" );
static_assert( kRxRingBytes > kFifoBytes, "the ring holds a whole FIFO and more" );

/// @brief The receive FIFO's fill above which the interrupt is raised: half
///        the FIFO, so the handler has 64 frames, 107 us at 6 Mbps, before
///        the FIFO overflows (ESP-IDF's driver raised it at 120, 13 us).
constexpr std::uint32_t kRxFullThreshold = 64;

static_assert( kRxFullThreshold < kFifoBytes, "the threshold is inside the FIFO" );

/// @brief How long the line idles after a byte before the interrupt is raised
///        for what the FIFO holds below the threshold, in bit times: ten
///        frames of ten bits, ESP-IDF's driver's default (17 us at 6 Mbps).
constexpr std::uint32_t kRxTimeoutBits = 100;

/// @brief How long write() waits for room in a full transmit FIFO before it
///        gives up on the line: 10 ms, the time 6,000 bytes take at 6 Mbps,
///        where a FIFO with no flow control empties in 213 us.
constexpr std::int64_t kTxStallMicroseconds = 10000;

/// @brief Sets UART0 up for the console and routes its interrupt to the
///        calling core.
///
/// Called once, from `app_main()` through Console::begin(), on Core 0 and
/// before the console's loop runs (Console::run(), on the same task). Forgets anything an earlier begin() left
/// in the ring and the flags, resets the port, sets its clock, frame, pins
/// and thresholds, creates the semaphore over static storage, routes the
/// interrupt and enables it. Allocates nothing.
/// @param[in] baud The line's rate (`kConsoleBaud`, sampler.h).
/// @retval ESP_OK              The port is set and receiving.
/// @retval ESP_ERR_INVALID_ARG The divider cannot make @p baud from
///                             kSourceClockHz; the interrupt is not routed.
/// @retval ESP_ERR_NO_MEM      The semaphore was not created; not routed.
/// @retval ESP_ERR_NOT_FOUND   No level-1 line of this core is free.
esp_err_t begin( std::uint32_t baud ) noexcept;

/// @brief Takes up to @p capacity received bytes, in order, waiting at most
///        @p wait for the first one when none has come.
///
/// Returns as soon as there is anything, never waiting for more. A ring that
/// had filled is refilled from the FIFO here, and the interrupt turned on
/// again once the FIFO is empty. Only after begin() has answered ESP_OK (the
/// console, which runs only then).
/// @param[out] out      Where the bytes go.
/// @param[in]  capacity The room at @p out.
/// @param[in]  wait     The longest wait for a first byte, in ticks; 0 takes
///                      only what is there.
/// @return The bytes taken; 0 when none came, when @p out is null or when
///         @p capacity is 0.
std::size_t read( char* out, std::size_t capacity, TickType_t wait ) noexcept;

/// @brief Puts @p length bytes into the transmit FIFO, in order, as it makes
///        room.
///
/// Spins while the FIFO is full (no task is woken: the console is the only
/// writer and has nothing else to do meanwhile), and gives up when it has
/// stayed full for kTxStallMicroseconds.
/// @param[in] data   The bytes.
/// @param[in] length How many.
/// @return The bytes placed: @p length, or fewer on a stalled line; 0 when
///         @p data is null.
std::size_t write( const char* data, std::size_t length ) noexcept;

/// @brief Whether the receive FIFO has overflowed since the last call, and
///        forgets it.
/// @retval true  Bytes were lost (the FIFO was full when they came).
/// @retval false Nothing was lost, or begin() was never called.
bool takeOverrun() noexcept;

/// @}

}  // namespace kleidos::meter::uart0

#endif  // KLEIDOS_METER_UART0_H
