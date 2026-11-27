// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file i2c_bus.h
 * @ingroup i2c_bus
 * @brief The INA228's bus: I2C0 driven on its registers as a master for one
 *        device, every transaction completed by polling the controller's
 *        status, with nothing allocated.
 *
 * ESP-IDF's I2C master, which the firmware used until FW-14, keeps its bus
 * object, five semaphores, a queue, the port's handle slot and each device's
 * object on the heap, and claims its interrupt through the allocator; a
 * change of the bus clock removes the device's object and allocates it again.
 * This module does what the instrument asks of the bus with static state and
 * no interrupt, semaphore or queue (FW-13), and `Ina228Driver` makes every
 * transaction through it (FW-14):
 *
 *  - **The controller** is I2C0, set through `hal/i2c_ll.h`: its bus clock
 *    and reset under ESP-IDF's peripheral clock lock, the 40 MHz crystal as
 *    its source (`I2C_CLK_SRC_DEFAULT` on the ESP32-S3), master mode with
 *    open-drain pads, no arbitration (the bus has one master), both FIFOs in
 *    FIFO mode, most significant bit first, a glitch filter of
 *    kGlitchFilterCycles and an SCL time-out of kSclTimeoutUs.
 *  - **The pads** are any two GPIOs, given to the controller through the GPIO
 *    matrix as ESP-IDF's driver gives them: driven high, open drain, the
 *    internal pull-ups on as a safety net beside the breakout's own,
 *    the output enable taken from the peripheral, and both input signals read
 *    back from the pads.
 *  - **The clock** is any rate from kMinClockHz to kMaxClockHz, set by
 *    rewriting the timing registers with the HAL's own arithmetic
 *    (`i2c_ll_master_cal_bus_clk()`), at begin() and at setClock(): no object
 *    is freed or made, so `SYSTem:COMMunicate:I2C:FREQuency` keeps its range
 *    and allocates nothing.
 *  - **A transaction** (probe(), write(), read(), writeRead()) is one command
 *    list in the controller's eight command registers and its bytes in the
 *    32-byte transmit FIFO: a START, the address byte, the data written, a
 *    repeated START and the read address for a read, the bytes read with the
 *    last one not acknowledged, and a STOP. The caller then polls the
 *    interrupt status (whose interrupt is routed to no CPU line) until the
 *    STOP's completion, a NACK, the controller's time-out or a lost
 *    arbitration, within kTransactionBudgetUs of the clock. A NACK ends the
 *    transaction once the bus is idle again, so no completion is left behind
 *    for the next one to take (the fault 0.16.5 found in ESP-IDF's driver,
 *    `docs/architecture.md` SS 2.2). Every status is cleared before the next
 *    transaction starts.
 *  - **Recovery**: a time-out, a lost arbitration, a bus that stays busy
 *    after a NACK or that is busy before a transaction starts resets the
 *    controller and restores its configuration, clocks nine SCL pulses so a
 *    device that holds SDA low finishes its byte and lets go, and resets the
 *    controller again, which also clears the pulse generator's enable whether
 *    or not the ESP32-S3 clears it itself (ESP-IDF's
 *    `i2c_ll_master_is_bus_clear_done()` says it cannot be read on this chip).
 *    clearBus() asks for the same recovery.
 *
 * **One core.** begin() records the core it runs on; every later call is
 * refused from the other one with `ESP_ERR_INVALID_STATE` and touches no
 * register, so the registers are only ever written by one core. On that core
 * a transaction is never interleaved with another: one that starts while
 * another is in flight (a higher-priority task, or an IPC call, that preempted
 * it) is refused the same way, and the one it preempted completes untouched.
 * The instrument's protocol keeps the two apart in the first place: a command
 * that makes a transaction is refused while a capture runs (the `R` flag of
 * `docs/scpi.md`).
 *
 * **IRAM.** The transactions and everything they call on a sample's path are
 * `IRAM_ATTR`, because both cores share the flash cache (`docs/architecture.md`
 * SS 5.2); the set-up, the clock change and the recovery are not, and
 * `scripts/check_iram_hot_path.py` lists the recovery as a cold exit.
 */
#ifndef KLEIDOS_METER_I2C_BUS_H
#define KLEIDOS_METER_I2C_BUS_H

#include <esp_err.h>

#include <cstddef>
#include <cstdint>

namespace kleidos::meter::i2c_bus {

/// @addtogroup i2c_bus
/// @{

/// @brief The controller's source clock: the 40 MHz crystal, the ESP32-S3's
///        `I2C_CLK_SRC_DEFAULT` and the clock ESP-IDF's driver gave the bus.
constexpr std::uint32_t kSourceClockHz = 40000000;

/// @brief The slowest bus clock setClock() accepts: 50 kHz, the floor of
///        `SYSTem:COMMunicate:I2C:FREQuency` (`docs/scpi.md`).
constexpr std::uint32_t kMinClockHz = 50000;

/// @brief The fastest: 1 MHz, Fast-mode Plus, the ceiling of the same command
///        and the clock the BURST profile needs.
constexpr std::uint32_t kMaxClockHz = 1000000;

/// @brief Each of the controller's FIFOs, in bytes (`I2C_LL_FIFO_LEN`).
constexpr std::size_t kFifoBytes = 32;

/// @brief The most bytes one transaction writes after its address: the
///        transmit FIFO less the two address bytes a write-then-read puts in
///        it.
constexpr std::size_t kMaxWriteBytes = kFifoBytes - 2U;

/// @brief The most bytes one transaction reads: the receive FIFO less two,
///        the largest read ESP-IDF's driver makes in one command list on this
///        chip (`s_i2c_read_command()`, SOC_I2C_STOP_INDEPENDENT unset).
constexpr std::size_t kMaxReadBytes = kFifoBytes - 2U;

/// @brief The highest 7-bit address.
constexpr std::uint32_t kMaxAddress = 0x7FU;

/// @brief How long a transaction may take before it is abandoned and the bus
///        recovered, in microseconds: 20 ms, the `xfer_timeout_ms` the
///        INA228's driver gave every ESP-IDF call, where a five-byte
///        register read is 75 bit times (190 us at 400 kHz) and the longest
///        transaction this module makes, 30 bytes written and 30 read, 561
///        (11.3 ms at 50 kHz).
constexpr std::int64_t kTransactionBudgetUs = 20000;

/// @brief How long SCL may stay in one level before the controller raises
///        its time-out, in microseconds: ESP-IDF's driver's default for a
///        device (`I2C_LL_SCL_WAIT_US_VAL_DEFAULT`). The INA228 never
///        stretches the clock ([DS] Figure 7-8 notes).
constexpr std::uint32_t kSclTimeoutUs = 2000;

/// @brief The glitch filter's width in source-clock cycles: 7, the value the
///        INA228's driver gave ESP-IDF's (`glitch_ignore_cnt`).
constexpr std::uint8_t kGlitchFilterCycles = 7;

/// @brief The SCL pulses a bus clear clocks: nine, enough for a device that
///        holds SDA low inside a byte to finish it and its acknowledge
///        (`I2C_LL_RESET_SLV_SCL_PULSE_NUM_DEFAULT`).
constexpr std::uint32_t kClearPulses = 9;

/// @brief The highest GPIO number the ESP32-S3 has, plus one.
constexpr std::uint32_t kGpioCount = 49;

static_assert( kMaxWriteBytes + 2U <= kFifoBytes, "a write-then-read fits the transmit FIFO" );
static_assert( kMaxReadBytes < kFifoBytes - 1U, "a read fits one command list" );
static_assert( kMinClockHz < kMaxClockHz, "the clock range is not empty" );

/// @brief Which of the bus's two lines have the chip's internal pull-up
///        beside the breakout's own (the pin map's `pull_sda` and `pull_scl`).
struct Pulls {
    bool sda = true;  ///< The data line's.
    bool scl = true;  ///< The clock line's.
};

/// @brief Sets I2C0 up as the bus's master on @p sdaGpio and @p sclGpio at
///        @p hz, and makes the calling core the bus's.
///
/// Forgets anything an earlier begin() left, resets the controller, gives it
/// the two pads, configures it and sets the clock. Makes no transaction.
/// Allocates nothing. A begin() that refuses leaves the bus unset: every
/// later call but begin() is refused until one succeeds. The pads an earlier
/// begin() used stay routed to the controller's outputs until their owner
/// parks them (relay::Bank::park()).
/// @param[in] sdaGpio The data line's GPIO.
/// @param[in] sclGpio The clock line's GPIO.
/// @param[in] hz      The bus clock, kMinClockHz to kMaxClockHz.
/// @param[in] pulls   Which lines have the internal pull-up.
/// @retval ESP_OK              The controller is set and the bus idle.
/// @retval ESP_ERR_INVALID_ARG A GPIO the chip does not have, the same GPIO
///                             twice, or a clock outside the range; no
///                             register is touched.
esp_err_t begin( std::uint32_t sdaGpio, std::uint32_t sclGpio, std::uint32_t hz,
                 Pulls pulls ) noexcept;

/// @brief Moves the bus to @p hz by rewriting the controller's timing
///        registers, between two transactions.
/// @param[in] hz The new bus clock, kMinClockHz to kMaxClockHz.
/// @retval ESP_OK                The next transaction runs at @p hz.
/// @retval ESP_ERR_INVALID_ARG   @p hz is outside the range; the clock is
///                               unchanged.
/// @retval ESP_ERR_INVALID_STATE begin() has not run, the call came from the
///                               other core, or a transaction is in flight;
///                               nothing is touched.
esp_err_t setClock( std::uint32_t hz ) noexcept;

/// @brief The bus clock the controller is set to.
/// @return The last clock begin() or setClock() set, in Hz; 0 before begin().
std::uint32_t clockHz() noexcept;

/// @brief Whether a device acknowledges @p address: a START, the address
///        byte for a write, and a STOP.
/// @param[in] address The 7-bit address.
/// @retval ESP_OK                It acknowledged.
/// @retval ESP_ERR_NOT_FOUND     Nothing acknowledged it.
/// @retval ESP_ERR_TIMEOUT       The bus did not complete the probe; it was
///                               recovered.
/// @retval ESP_ERR_INVALID_ARG   @p address is above kMaxAddress.
/// @retval ESP_ERR_INVALID_STATE As setClock().
esp_err_t probe( std::uint8_t address ) noexcept;

/// @brief Writes @p length bytes to the device at @p address.
/// @param[in] address The 7-bit address.
/// @param[in] data    The bytes, sent in order (the INA228 takes the register
///                    pointer first, then the value most significant byte
///                    first).
/// @param[in] length  How many, 1 to kMaxWriteBytes.
/// @retval ESP_OK                 Every byte was acknowledged.
/// @retval ESP_ERR_INVALID_RESPONSE The address or a byte was not
///                                acknowledged; the bus is idle again.
/// @retval ESP_ERR_TIMEOUT        The bus did not complete the transaction;
///                                it was recovered.
/// @retval ESP_ERR_INVALID_ARG    @p address is above kMaxAddress, @p data is
///                                null, or @p length is 0 or too long.
/// @retval ESP_ERR_INVALID_STATE  As setClock().
esp_err_t write( std::uint8_t address, const std::uint8_t* data, std::size_t length ) noexcept;

/// @brief Reads @p length bytes from the device at @p address, from wherever
///        its register pointer was left.
/// @param[in]  address The 7-bit address.
/// @param[out] data    Receives the bytes, in the order they came; untouched
///                     unless the read completed.
/// @param[in]  length  How many, 1 to kMaxReadBytes.
/// @return As write().
esp_err_t read( std::uint8_t address, std::uint8_t* data, std::size_t length ) noexcept;

/// @brief Writes @p outLength bytes to the device at @p address and, after a
///        repeated START, reads @p inLength bytes back: a register read.
/// @param[in]  address   The 7-bit address.
/// @param[in]  out       The bytes written (the register pointer).
/// @param[in]  outLength How many, 1 to kMaxWriteBytes.
/// @param[out] in        Receives the bytes read; untouched unless the
///                       transaction completed.
/// @param[in]  inLength  How many, 1 to kMaxReadBytes.
/// @return As write().
esp_err_t writeRead( std::uint8_t address, const std::uint8_t* out, std::size_t outLength,
                     std::uint8_t* in, std::size_t inLength ) noexcept;

/// @brief Recovers the bus: resets the controller, clocks kClearPulses SCL
///        pulses, and resets it again with its configuration restored.
/// @retval ESP_OK                The bus is idle.
/// @retval ESP_ERR_TIMEOUT       It is still busy: a device holds a line
///                               through the pulses.
/// @retval ESP_ERR_INVALID_STATE As setClock().
esp_err_t clearBus() noexcept;

/// @}

}  // namespace kleidos::meter::i2c_bus

#endif  // KLEIDOS_METER_I2C_BUS_H
