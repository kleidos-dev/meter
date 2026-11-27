// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file i2c_bus.cpp
 * @ingroup i2c_bus
 * @brief I2C0 set through its low-level layer, its command lists, the polled
 *        completion and the recovery of a stuck bus.
 *
 * No driver object, semaphore, queue or interrupt (FW-13): the controller is
 * set through `hal/i2c_ll.h` and the pads through `hal/gpio_ll.h`, and the
 * bus's clock, core and in-flight flag are one static object in internal
 * RAM.
 */
#include "i2c_bus.h"

#include <esp_attr.h>
#include <esp_private/periph_ctrl.h>
#include <esp_rom_sys.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <hal/gpio_ll.h>
#include <hal/i2c_ll.h>
#include <soc/gpio_sig_map.h>
#include <soc/io_mux_reg.h>

namespace kleidos::meter::i2c_bus {

namespace {

/// The controller's port: I2C0, the one ESP-IDF's driver was given.
constexpr std::int32_t kPort = 0;

/// Every interrupt status bit the controller has (`I2C_LL_INTR_MASK`).
constexpr std::uint32_t kAllStatus = static_cast<std::uint32_t>( I2C_LL_INTR_MASK );

/// The address or a byte was not acknowledged.
constexpr std::uint32_t kNack = static_cast<std::uint32_t>( I2C_LL_INTR_NACK );

/// The STOP has been sent: the transaction is over.
constexpr std::uint32_t kComplete = static_cast<std::uint32_t>( I2C_LL_INTR_MST_COMPLETE );

/// SCL stayed in one level past the time-out, or the controller lost the
/// bus: either leaves its state machine where only a reset moves it.
constexpr std::uint32_t kFault = static_cast<std::uint32_t>( I2C_LL_INTR_TIMEOUT )
                                 | static_cast<std::uint32_t>( I2C_LL_INTR_ARBITRATION );

/// The statuses a transaction waits for, enabled so that the status register
/// shows them; the controller's interrupt source is routed to no CPU line
/// (ESP-IDF's startup routes every source to none, and nothing here routes
/// this one), so enabling them raises no interrupt.
constexpr std::uint32_t kEvents = kNack | kComplete | kFault;

/// The bit of an address byte that asks the device to send.
constexpr std::uint32_t kReadBit = 0x01U;

/// The microseconds in a second.
constexpr std::uint32_t kMicrosPerSecond = 1000000;

/// The bus's clock, core and in-flight flag, in one object reached
/// through a function so that no variable has namespace scope (docs/misra.md,
/// rule 6.7.2). Plain fields: every call but begin() is refused from a core
/// other than @c core, and @c inFlight is set and read with that core's
/// interrupts masked.
struct State {
    std::uint32_t hz       = 0;      ///< The bus clock; 0 until begin() has run.
    std::int32_t  core     = -1;     ///< The core begin() ran on.
    bool          inFlight = false;  ///< A transaction, a clock change or a clear is running.
};

/// @brief The one State.
/// @return The bus's clock, core and flag.
IRAM_ATTR State& state() noexcept {
    static constinit State instance;
    return instance;
}

/// @brief Whether @p hz is a clock the bus may run at.
/// @param[in] hz A bus clock, Hz.
/// @return true from kMinClockHz to kMaxClockHz.
constexpr bool clockInRange( std::uint32_t hz ) noexcept {
    return hz >= kMinClockHz && hz <= kMaxClockHz;
}

/// @brief Takes the bus for one operation, with this core's interrupts masked
///        while it looks.
/// @param[in,out] own The state.
/// @retval true  The bus is set up, this is its core, and nothing else runs
///               on it: the operation may touch the registers.
/// @retval false It may not; nothing was taken.
IRAM_ATTR bool claim( State& own ) noexcept {
    const UBaseType_t masked = portSET_INTERRUPT_MASK_FROM_ISR();
    const bool        free =
        own.hz != 0U && static_cast<std::int32_t>( xPortGetCoreID() ) == own.core && !own.inFlight;
    if ( free ) {
        own.inFlight = true;
    }
    portCLEAR_INTERRUPT_MASK_FROM_ISR( masked );
    return free;
}

/// @brief Gives the bus back after an operation claim() let run.
/// @param[in,out] own The state.
IRAM_ATTR void release( State& own ) noexcept {
    own.inFlight = false;
}

/// @brief Writes the timing registers for @p hz from the HAL's own arithmetic,
///        with the SCL time-out and no fractional divider, and latches them
///        into the controller's clock domain.
/// @param[in] hz The bus clock, in range.
void applyClock( std::uint32_t hz ) noexcept {
    i2c_hal_clk_config_t timing{};
    i2c_ll_master_cal_bus_clk( kSourceClockHz, hz, &timing );
    i2c_ll_master_set_bus_timing( &I2C0, &timing );
    i2c_ll_set_tout( &I2C0, static_cast<std::int32_t>( i2c_ll_calculate_timeout_us_to_reg_val(
                                kSourceClockHz, kSclTimeoutUs ) ) );
    i2c_ll_master_set_fractional_divider( &I2C0, 0, 0 );
    i2c_ll_update( &I2C0 );
}

/// @brief Gates I2C0's bus clock on and pulses its reset, under ESP-IDF's
///        peripheral clock lock: every register back at its reset value.
///
/// The parentheses around each LL function's name call the function itself
/// rather than the macro of the same name, whose only addition is to require
/// `PERIPH_RCC_ATOMIC()`'s scope (uart0.cpp's resetPort() says why);
/// `periph_rcc_enter()` and `periph_rcc_exit()` are that scope's lock.
void resetController() noexcept {
    periph_rcc_enter();
    ( i2c_ll_enable_bus_clock )( kPort, true );
    ( i2c_ll_reset_register )( kPort );
    periph_rcc_exit();
}

/// @brief Sets a controller just out of reset as the bus's master, as
///        ESP-IDF's `i2c_hal_master_init()` and its driver's set-up do, and
///        sets its clock.
/// @param[in] hz The bus clock, in range.
void configure( std::uint32_t hz ) noexcept {
    i2c_ll_set_source_clk( &I2C0, I2C_CLK_SRC_DEFAULT );
    i2c_ll_enable_controller_clock( &I2C0, true );
    i2c_ll_set_mode( &I2C0, I2C_BUS_MODE_MASTER );
    i2c_ll_enable_pins_open_drain( &I2C0, true );
    i2c_ll_enable_arbitration( &I2C0, false );
    i2c_ll_master_rx_full_ack_level( &I2C0, 0 );
    i2c_ll_set_data_mode( &I2C0, I2C_DATA_MODE_MSB_FIRST, I2C_DATA_MODE_MSB_FIRST );
    i2c_ll_enable_fifo_mode( &I2C0, true );
    i2c_ll_txfifo_rst( &I2C0 );
    i2c_ll_rxfifo_rst( &I2C0 );
    i2c_ll_disable_intr_mask( &I2C0, kAllStatus );
    i2c_ll_clear_intr_mask( &I2C0, kAllStatus );
    i2c_ll_enable_intr_mask( &I2C0, kEvents );
    i2c_ll_master_set_filter( &I2C0, kGlitchFilterCycles );
    applyClock( hz );
}

/// @brief Gives @p gpio to the controller's @p outSignal and @p inSignal
///        through the GPIO matrix, open drain with or without its pull-up, as
///        ESP-IDF's driver does (`s_hp_i2c_pins_config()`,
///        `gpio_hal_matrix_out()` without the ROM).
/// @param[in] gpio      The pad.
/// @param[in] outSignal The controller's output signal for the line.
/// @param[in] inSignal  Its input signal.
/// @param[in] pullUp    Whether the pad has the internal pull-up.
void connectPad( std::uint32_t gpio, std::uint32_t outSignal, std::uint32_t inSignal,
                 bool pullUp ) noexcept {
    gpio_ll_set_level( &GPIO, gpio, 1 );
    gpio_ll_input_enable( &GPIO, gpio );
    gpio_ll_od_enable( &GPIO, gpio );
    if ( pullUp ) {
        gpio_ll_pullup_en( &GPIO, gpio );
    } else {
        gpio_ll_pullup_dis( &GPIO, gpio );
    }
    gpio_ll_pulldown_dis( &GPIO, gpio );
    gpio_ll_func_sel( &GPIO, static_cast<std::uint8_t>( gpio ), PIN_FUNC_GPIO );
    gpio_ll_set_output_signal_matrix_source( &GPIO, gpio, outSignal, false );
    gpio_ll_set_output_enable_ctrl( &GPIO, static_cast<std::uint8_t>( gpio ), true, false );
    gpio_ll_set_input_signal_matrix_source( &GPIO, inSignal, gpio, false );
}

/// @brief Recovers a bus whose controller or device is stuck: resets and
///        configures the controller, clocks kClearPulses SCL pulses and waits
///        them out, then resets and configures it again (i2c_bus.h). Out of
///        IRAM: a sample never runs it.
/// @param[in] own The state.
/// @retval true  The bus is idle.
/// @retval false It is still busy.
[[gnu::noinline]] bool recover( const State& own ) noexcept {
    resetController();
    configure( own.hz );
    i2c_ll_master_clr_bus( &I2C0, kClearPulses, true );
    esp_rom_delay_us( ( ( kClearPulses + 1U ) * kMicrosPerSecond ) / own.hz + 1U );
    resetController();
    configure( own.hz );
    return !i2c_ll_is_bus_busy( &I2C0 );
}

/// @brief A command that sends a START, or a repeated START.
/// @return The command.
IRAM_ATTR i2c_ll_hw_cmd_t startCommand() noexcept {
    i2c_ll_hw_cmd_t command{};
    command.op_code = I2C_LL_CMD_RESTART;
    return command;
}

/// @brief A command that sends @p bytes from the transmit FIFO, each to be
///        acknowledged (a NACK ends the transaction).
/// @param[in] bytes How many, 1 to kFifoBytes.
/// @return The command.
IRAM_ATTR i2c_ll_hw_cmd_t writeCommand( std::uint8_t bytes ) noexcept {
    i2c_ll_hw_cmd_t command{};
    command.op_code  = I2C_LL_CMD_WRITE;
    command.ack_en   = 1;
    command.ack_exp  = 0;
    command.byte_num = bytes;
    return command;
}

/// @brief A command that reads @p bytes into the receive FIFO, acknowledging
///        each, so the device sends the next.
/// @param[in] bytes How many, 1 to kMaxReadBytes - 1.
/// @return The command.
IRAM_ATTR i2c_ll_hw_cmd_t readAckedCommand( std::uint8_t bytes ) noexcept {
    i2c_ll_hw_cmd_t command{};
    command.op_code  = I2C_LL_CMD_READ;
    command.ack_val  = 0;
    command.byte_num = bytes;
    return command;
}

/// @brief A command that reads the last byte and does not acknowledge it, so
///        the device lets SDA go for the STOP.
/// @return The command.
IRAM_ATTR i2c_ll_hw_cmd_t readLastCommand() noexcept {
    i2c_ll_hw_cmd_t command{};
    command.op_code  = I2C_LL_CMD_READ;
    command.ack_val  = 1;
    command.byte_num = 1;
    return command;
}

/// @brief A command that sends the STOP and ends the list.
/// @return The command.
IRAM_ATTR i2c_ll_hw_cmd_t stopCommand() noexcept {
    i2c_ll_hw_cmd_t command{};
    command.op_code = I2C_LL_CMD_STOP;
    return command;
}

/// @brief Writes @p command into command register @p index.
/// @param[in] command The command.
/// @param[in] index   Its register, 0 to 7.
/// @return The next register's index.
IRAM_ATTR std::int32_t push( const i2c_ll_hw_cmd_t& command, std::int32_t index ) noexcept {
    i2c_ll_master_write_cmd_reg( &I2C0, command, index );
    return index + 1;
}

/// @brief Puts one transaction's bytes into the transmit FIFO and its
///        commands into the command registers (i2c_bus.h): the address for a
///        write and the data when there is data or nothing to read, then the
///        read address and the reads when there is something to read.
/// @param[in] address   The 7-bit address.
/// @param[in] out       The bytes to write; null when @p outLength is 0.
/// @param[in] outLength How many, 0 to kMaxWriteBytes.
/// @param[in] inLength  How many bytes to read, 0 to kMaxReadBytes.
IRAM_ATTR void load( std::uint8_t address, const std::uint8_t* out, std::size_t outLength,
                     std::size_t inLength ) noexcept {
    const auto writeAddress =
        static_cast<std::uint8_t>( static_cast<std::uint32_t>( address ) << 1U );
    std::int32_t index = push( startCommand(), 0 );
    if ( outLength > 0U || inLength == 0U ) {
        i2c_ll_write_txfifo( &I2C0, &writeAddress, 1 );
        i2c_ll_write_txfifo( &I2C0, out, static_cast<std::uint8_t>( outLength ) );
        index = push( writeCommand( static_cast<std::uint8_t>( outLength + 1U ) ), index );
    }
    if ( inLength > 0U ) {
        if ( outLength > 0U ) {
            index = push( startCommand(), index );
        }
        const auto readAddress =
            static_cast<std::uint8_t>( static_cast<std::uint32_t>( writeAddress ) | kReadBit );
        i2c_ll_write_txfifo( &I2C0, &readAddress, 1 );
        index = push( writeCommand( 1 ), index );
        if ( inLength > 1U ) {
            index = push( readAckedCommand( static_cast<std::uint8_t>( inLength - 1U ) ), index );
        }
        index = push( readLastCommand(), index );
    }
    (void)push( stopCommand(), index );
}

/// @brief Waits for the bus to go idle after a NACK, whose STOP is still on
///        the wire when the NACK is seen, so that its completion is not left
///        for the next transaction.
/// @param[in] own        The state.
/// @param[in] deadlineUs When the transaction's budget runs out.
/// @retval ESP_ERR_INVALID_RESPONSE The bus went idle: an honest NACK.
/// @retval ESP_ERR_TIMEOUT          It did not; the bus was recovered.
IRAM_ATTR esp_err_t afterNack( const State& own, std::int64_t deadlineUs ) noexcept {
    while ( i2c_ll_is_bus_busy( &I2C0 ) ) {
        if ( esp_timer_get_time() >= deadlineUs ) {
            (void)recover( own );
            return ESP_ERR_TIMEOUT;
        }
    }
    i2c_ll_clear_intr_mask( &I2C0, kAllStatus );
    return ESP_ERR_INVALID_RESPONSE;
}

/// @brief Starts the loaded transaction and polls its status until it ends.
/// @param[in]  own      The state.
/// @param[out] in       Receives the bytes read; untouched unless it completed.
/// @param[in]  inLength How many bytes the transaction reads.
/// @retval ESP_OK                   The STOP was sent; @p in holds the bytes.
/// @retval ESP_ERR_INVALID_RESPONSE A NACK (afterNack()).
/// @retval ESP_ERR_TIMEOUT          The controller's time-out, a lost
///                                  arbitration or the budget; the bus was
///                                  recovered.
IRAM_ATTR esp_err_t run( const State& own, std::uint8_t* in, std::size_t inLength ) noexcept {
    i2c_ll_clear_intr_mask( &I2C0, kAllStatus );
    i2c_ll_update( &I2C0 );
    i2c_ll_start_trans( &I2C0 );
    const std::int64_t deadlineUs = esp_timer_get_time() + kTransactionBudgetUs;
    while ( true ) {
        std::uint32_t status = 0;
        i2c_ll_get_intr_mask( &I2C0, &status );
        if ( ( status & kNack ) != 0U ) {
            return afterNack( own, deadlineUs );
        }
        if ( ( status & kFault ) != 0U ) {
            (void)recover( own );
            return ESP_ERR_TIMEOUT;
        }
        if ( ( status & kComplete ) != 0U ) {
            i2c_ll_read_rxfifo( &I2C0, in, static_cast<std::uint8_t>( inLength ) );
            i2c_ll_clear_intr_mask( &I2C0, kAllStatus );
            return ESP_OK;
        }
        if ( esp_timer_get_time() >= deadlineUs ) {
            (void)recover( own );
            return ESP_ERR_TIMEOUT;
        }
    }
}

/// @brief One transaction, once the caller's arguments have passed: claims
///        the bus, recovers it first when it is busy, loads, runs and gives
///        the bus back.
/// @param[in]  address   The 7-bit address.
/// @param[in]  out       The bytes to write, or null.
/// @param[in]  outLength How many.
/// @param[out] in        Receives the bytes read, or null.
/// @param[in]  inLength  How many.
/// @return As run(); `ESP_ERR_TIMEOUT` for a bus still busy after a
///         recovery; `ESP_ERR_INVALID_STATE` when claim() refuses.
IRAM_ATTR esp_err_t transact( std::uint8_t address, const std::uint8_t* out, std::size_t outLength,
                              std::uint8_t* in, std::size_t inLength ) noexcept {
    State& own = state();
    if ( !claim( own ) ) {
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t result = ESP_ERR_TIMEOUT;
    // A bus held busy is recovered first; one that stays busy fails the
    // transaction unmade.
    bool idle = !i2c_ll_is_bus_busy( &I2C0 );
    if ( !idle ) {
        idle = recover( own );
    }
    if ( idle ) {
        i2c_ll_txfifo_rst( &I2C0 );
        i2c_ll_rxfifo_rst( &I2C0 );
        load( address, out, outLength, inLength );
        result = run( own, in, inLength );
    }
    release( own );
    return result;
}

/// @brief Whether @p address is a 7-bit address.
/// @param[in] address The address.
/// @return true up to kMaxAddress.
IRAM_ATTR constexpr bool addressValid( std::uint8_t address ) noexcept {
    return address <= kMaxAddress;
}

/// @brief Whether @p data and @p length describe bytes to write.
/// @param[in] data   The bytes.
/// @param[in] length How many.
/// @return true for a non-null @p data and 1 to kMaxWriteBytes bytes.
IRAM_ATTR constexpr bool writeValid( const std::uint8_t* data, std::size_t length ) noexcept {
    return data != nullptr && length > 0U && length <= kMaxWriteBytes;
}

/// @brief Whether @p data and @p length describe room for bytes to read.
/// @param[in] data   The room.
/// @param[in] length How many.
/// @return true for a non-null @p data and 1 to kMaxReadBytes bytes.
IRAM_ATTR constexpr bool readValid( const std::uint8_t* data, std::size_t length ) noexcept {
    return data != nullptr && length > 0U && length <= kMaxReadBytes;
}

static_assert( kMaxWriteBytes + 1U <= 0xFFU && kMaxReadBytes <= 0xFFU,
               "a command's byte count fits its eight bits" );
static_assert( static_cast<std::size_t>( I2C_LL_FIFO_LEN ) == kFifoBytes, "the FIFO is the LL's" );
static_assert( static_cast<std::uint32_t>( I2C_LL_RESET_SLV_SCL_PULSE_NUM_DEFAULT ) == kClearPulses,
               "the clear's pulses are ESP-IDF's" );

}  // namespace

esp_err_t begin( std::uint32_t sdaGpio, std::uint32_t sclGpio, std::uint32_t hz,
                 Pulls pulls ) noexcept {
    State& own   = state();
    own.hz       = 0;  // a refused begin() leaves the bus unset
    own.inFlight = false;
    if ( sdaGpio >= kGpioCount || sclGpio >= kGpioCount || sdaGpio == sclGpio
         || !clockInRange( hz ) ) {
        return ESP_ERR_INVALID_ARG;
    }
    own.hz   = hz;
    own.core = static_cast<std::int32_t>( xPortGetCoreID() );
    resetController();
    configure( hz );
    connectPad( sdaGpio, I2CEXT0_SDA_OUT_IDX, I2CEXT0_SDA_IN_IDX, pulls.sda );
    connectPad( sclGpio, I2CEXT0_SCL_OUT_IDX, I2CEXT0_SCL_IN_IDX, pulls.scl );
    return ESP_OK;
}

esp_err_t setClock( std::uint32_t hz ) noexcept {
    if ( !clockInRange( hz ) ) {
        return ESP_ERR_INVALID_ARG;
    }
    State& own = state();
    if ( !claim( own ) ) {
        return ESP_ERR_INVALID_STATE;
    }
    applyClock( hz );
    own.hz = hz;
    release( own );
    return ESP_OK;
}

std::uint32_t clockHz() noexcept {
    return state().hz;
}

esp_err_t probe( std::uint8_t address ) noexcept {
    if ( !addressValid( address ) ) {
        return ESP_ERR_INVALID_ARG;
    }
    const esp_err_t result = transact( address, nullptr, 0, nullptr, 0 );
    return ( result == ESP_ERR_INVALID_RESPONSE ) ? ESP_ERR_NOT_FOUND : result;
}

IRAM_ATTR esp_err_t write( std::uint8_t address, const std::uint8_t* data,
                           std::size_t length ) noexcept {
    if ( !addressValid( address ) || !writeValid( data, length ) ) {
        return ESP_ERR_INVALID_ARG;
    }
    return transact( address, data, length, nullptr, 0 );
}

IRAM_ATTR esp_err_t read( std::uint8_t address, std::uint8_t* data, std::size_t length ) noexcept {
    if ( !addressValid( address ) || !readValid( data, length ) ) {
        return ESP_ERR_INVALID_ARG;
    }
    return transact( address, nullptr, 0, data, length );
}

IRAM_ATTR esp_err_t writeRead( std::uint8_t address, const std::uint8_t* out, std::size_t outLength,
                               std::uint8_t* in, std::size_t inLength ) noexcept {
    if ( !addressValid( address ) || !writeValid( out, outLength ) || !readValid( in, inLength ) ) {
        return ESP_ERR_INVALID_ARG;
    }
    return transact( address, out, outLength, in, inLength );
}

esp_err_t clearBus() noexcept {
    State& own = state();
    if ( !claim( own ) ) {
        return ESP_ERR_INVALID_STATE;
    }
    const bool idle = recover( own );
    release( own );
    return idle ? ESP_OK : ESP_ERR_TIMEOUT;
}

}  // namespace kleidos::meter::i2c_bus
