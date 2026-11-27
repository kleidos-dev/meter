// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_i2c_bus.cpp
 * @brief i2c_bus.cpp on the simulated I2C0's registers and the INA228 model:
 *        the set-up and its refusals, the clock over its whole range, each
 *        kind of transaction and its command list on the wire, the argument
 *        checks, NACKs, the controller's time-out, a bus held busy, a
 *        controller that never answers, the recovery, and the one-core and
 *        one-at-a-time rules.
 */
#include "i2c_bus.h"
#include "ina228_regs.h"
#include "scope.h"
#include "unit.h"

#include <freertos/FreeRTOS.h>
#include <hal/i2c_ll.h>
#include <soc/gpio_sig_map.h>
#include <soc/io_mux_reg.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <initializer_list>
#include <string>

using namespace kleidos::meter;
using bench::Scope;

namespace {

/// The bus's pins and clock as app_main() opens it (main.cpp).
constexpr std::uint32_t kSda   = 4;
constexpr std::uint32_t kScl   = 5;
constexpr std::uint32_t kBoot  = 400000;
constexpr std::uint8_t  kIna   = 0x40;
constexpr std::uint8_t  kEmpty = 0x41;

/// The statuses begin() enables: NACK, time-out, completion, arbitration.
constexpr std::uint32_t kEvents =
    I2C_LL_INTR_NACK | I2C_LL_INTR_TIMEOUT | I2C_LL_INTR_MST_COMPLETE | I2C_LL_INTR_ARBITRATION;

/// The last transaction on the wire, or "" when none ran.
std::string lastWire( const Scope& scope ) {
    return scope.world->i2c.wire.empty() ? std::string() : scope.world->i2c.wire.back();
}

/// The clock begin() and setClock() leave latched is within 1 % of @p hz.
bool clockNear( const sim::I2c& bus, std::uint32_t hz ) {
    const std::uint32_t actual = bus.sclHz();
    const std::uint32_t error  = actual > hz ? actual - hz : hz - actual;
    return error * 100U <= hz;
}

/// A fresh bus on the boot's pins and clock, on the calling test's board.
void open() {
    REQUIRE( i2c_bus::begin( kSda, kScl, kBoot, {} ) == ESP_OK );
}

}  // namespace

TEST( "i2c_bus", "begin() sets I2C0 as the master on the crystal and gives it both pads" ) {
    Scope scope;
    open();
    const sim::I2c& bus = scope->i2c;
    CHECK( bus.busClock && bus.resets == 1 && bus.core == 0 );
    CHECK( bus.sourceClock == I2C_CLK_SRC_XTAL && bus.controllerClock );
    CHECK( bus.master && bus.openDrain && !bus.arbitration && bus.rxFullAckLevel == 0 );
    CHECK( bus.txOrder == I2C_DATA_MODE_MSB_FIRST && bus.rxOrder == I2C_DATA_MODE_MSB_FIRST );
    CHECK( bus.fifoMode );
    CHECK( bus.enabled == kEvents && bus.raw == 0 );
    // The timing latched: 400 kHz from the crystal, the SCL time-out at 2 ms
    // (2^17 cycles), no fractional divider, the glitch filter.
    CHECK( bus.updates >= 1 && clockNear( bus, kBoot ) );
    CHECK( bus.live.tout == 17 && bus.live.fracA == 0 && bus.live.fracB == 0 );
    CHECK( bus.live.filter == i2c_bus::kGlitchFilterCycles );
    CHECK( i2c_bus::clockHz() == kBoot );
    // Both pads open drain with their pull-ups, high, the matrix's signals.
    for ( const std::uint32_t pin : { kSda, kScl } ) {
        const sim::Pad& pad = scope->pads[pin];
        CHECK( pad.function == PIN_FUNC_GPIO && pad.configured );
        CHECK( pad.latch == 1 && pad.input && pad.openDrain && pad.pullUp && !pad.pullDown );
        CHECK( pad.oneByPeripheral );
    }
    CHECK( scope->pads[kSda].outSignal == I2CEXT0_SDA_OUT_IDX );
    CHECK( scope->pads[kScl].outSignal == I2CEXT0_SCL_OUT_IDX );
    CHECK( scope->inputSignalPad.at( I2CEXT0_SDA_IN_IDX ) == kSda );
    CHECK( scope->inputSignalPad.at( I2CEXT0_SCL_IN_IDX ) == kScl );
    CHECK( bus.starts == 0 );           // no transaction
    CHECK( scope->idfLedger.empty() );  // nothing ESP-IDF allocates
    CHECK( scope->cpus[0].routes.empty() && scope->cpus[1].routes.empty() );  // no interrupt
}

TEST( "i2c_bus", "begin() gives each line the pull it is told, and moves to other pads" ) {
    Scope scope;
    REQUIRE( i2c_bus::begin( kSda, kScl, kBoot, { false, true } ) == ESP_OK );
    CHECK( !scope->pads[kSda].pullUp && scope->pads[kScl].pullUp );
    CHECK( i2c_bus::probe( kIna ) == ESP_OK );
    // A pull an earlier begin() enabled is turned off by the next.
    REQUIRE( i2c_bus::begin( kSda, kScl, kBoot, { true, false } ) == ESP_OK );
    CHECK( scope->pads[kSda].pullUp && !scope->pads[kScl].pullUp );
    CHECK( i2c_bus::probe( kIna ) == ESP_OK );
    // Other pads: the INA228 is not on them.
    REQUIRE( i2c_bus::begin( 18, 21, kBoot, {} ) == ESP_OK );
    CHECK( scope->inputSignalPad.at( I2CEXT0_SDA_IN_IDX ) == 18U );
    CHECK( i2c_bus::probe( kIna ) == ESP_ERR_NOT_FOUND );
    REQUIRE( i2c_bus::begin( kSda, kScl, kBoot, {} ) == ESP_OK );
    CHECK( i2c_bus::probe( kIna ) == ESP_OK );
}

TEST( "i2c_bus", "begin() refuses a pin the chip lacks, one pin twice, or a clock out of range" ) {
    const struct {
        std::uint32_t sda;
        std::uint32_t scl;
        std::uint32_t hz;
    } refused[] = {
        { i2c_bus::kGpioCount, kScl, kBoot },       // no such data pin
        { kSda, i2c_bus::kGpioCount, kBoot },       // no such clock pin
        { kSda, kSda, kBoot },                      // the same pin twice
        { kSda, kScl, i2c_bus::kMinClockHz - 1U },  // below the range
        { kSda, kScl, i2c_bus::kMaxClockHz + 1U },  // above it
    };
    for ( const auto& args : refused ) {
        Scope scope;
        CHECK( i2c_bus::begin( args.sda, args.scl, args.hz, {} ) == ESP_ERR_INVALID_ARG );
        CHECK( !scope->i2c.busClock && scope->i2c.resets == 0 );  // no register touched
        // A refused begin() leaves the bus unset: nothing else runs.
        CHECK( i2c_bus::clockHz() == 0 );
        CHECK( i2c_bus::probe( kIna ) == ESP_ERR_INVALID_STATE );
        CHECK( i2c_bus::setClock( kBoot ) == ESP_ERR_INVALID_STATE );
        CHECK( i2c_bus::clearBus() == ESP_ERR_INVALID_STATE );
        CHECK( scope->i2c.starts == 0 );
    }
    // Both ends of the range are accepted.
    for ( const std::uint32_t hz : { i2c_bus::kMinClockHz, i2c_bus::kMaxClockHz } ) {
        Scope scope;
        CHECK( i2c_bus::begin( kSda, kScl, hz, {} ) == ESP_OK );
        CHECK( clockNear( scope->i2c, hz ) );
    }
}

TEST( "i2c_bus", "setClock() rewrites the timing for every clock from 50 kHz to 1 MHz" ) {
    Scope scope;
    open();
    const int resets = scope->i2c.resets;
    for ( const std::uint32_t hz :
          { 50000U, 79999U, 80000U, 100000U, 333333U, 400000U, 1000000U } ) {
        CHECK( i2c_bus::setClock( hz ) == ESP_OK );
        CHECK( i2c_bus::clockHz() == hz );
        CHECK( clockNear( scope->i2c, hz ) );
        CHECK( scope->i2c.live.tout == 17 && scope->i2c.live.filter == 7 );
        CHECK( i2c_bus::probe( kIna ) == ESP_OK );  // and the bus still works
    }
    // No reset, no object: the registers alone.
    CHECK( scope->i2c.resets == resets );
    CHECK( scope->idfLedger.empty() );
    // A clock out of range is refused and changes nothing.
    CHECK( i2c_bus::setClock( 49999 ) == ESP_ERR_INVALID_ARG );
    CHECK( i2c_bus::setClock( 1000001 ) == ESP_ERR_INVALID_ARG );
    CHECK( i2c_bus::clockHz() == 1000000U && clockNear( scope->i2c, 1000000U ) );
}

TEST( "i2c_bus", "a transaction takes the bit times of the clock it was set to" ) {
    Scope scope;
    open();
    // A probe is a START, one byte and its acknowledge, and a STOP: 11 bit
    // times, 28 us at 400 kHz, 110 us at 100 kHz.
    sim::Micros before = scope->now();
    CHECK( i2c_bus::probe( kIna ) == ESP_OK );
    const sim::Micros fast = scope->now() - before;
    REQUIRE( i2c_bus::setClock( 100000 ) == ESP_OK );
    before = scope->now();
    CHECK( i2c_bus::probe( kIna ) == ESP_OK );
    const sim::Micros slow = scope->now() - before;
    CHECK( fast >= 28 && fast < 110 );
    CHECK( slow >= 110 );
}

TEST( "i2c_bus", "a probe is a START, the address and a STOP, and a silent address is not found" ) {
    Scope scope;
    open();
    CHECK( i2c_bus::probe( kIna ) == ESP_OK );
    CHECK_EQ( lastWire( scope ), "S 80 P" );
    CHECK( i2c_bus::probe( kEmpty ) == ESP_ERR_NOT_FOUND );
    CHECK_EQ( lastWire( scope ), "S 82! P" );
    // The STOP's completion was waited for and cleared: nothing is left for
    // the next transaction to take, and it reads the device.
    CHECK( !scope->i2c.active && scope->i2c.raw == 0 );
    CHECK( i2c_bus::probe( kIna ) == ESP_OK );
    CHECK( i2c_bus::probe( i2c_bus::kMaxAddress + 1U ) == ESP_ERR_INVALID_ARG );
    CHECK( scope->i2c.starts == 3 );
}

TEST( "i2c_bus", "a scan of the INA228's strap addresses finds the device alone" ) {
    Scope scope;
    open();
    int found = 0;
    for ( std::uint8_t address = ina228::kAddressFirst; address <= ina228::kAddressLast;
          ++address ) {
        const esp_err_t result = i2c_bus::probe( address );
        CHECK( result == ( address == kIna ? ESP_OK : ESP_ERR_NOT_FOUND ) );
        found += result == ESP_OK ? 1 : 0;
    }
    CHECK( found == 1 );
    CHECK( scope->i2c.starts == static_cast<int>( ina228::kAddressCount ) );
}

TEST( "i2c_bus", "a write sends the address and every byte, and the device takes the value" ) {
    Scope scope;
    open();
    const std::array<std::uint8_t, 3> frame = { ina228::kRegShuntCal, 0x12, 0x34 };
    CHECK( i2c_bus::write( kIna, frame.data(), frame.size() ) == ESP_OK );
    CHECK_EQ( lastWire( scope ), "S 80 02 12 34 P" );
    CHECK( scope->ina.regs[ina228::kRegShuntCal] == 0x1234 );
    // The longest write fits the FIFO.
    std::array<std::uint8_t, i2c_bus::kMaxWriteBytes> longest{};
    longest[0] = ina228::kRegShuntCal;
    CHECK( i2c_bus::write( kIna, longest.data(), longest.size() ) == ESP_OK );
    CHECK( scope->i2c.txOverruns == 0 );
}

TEST( "i2c_bus", "a read takes the bytes the pointer names, the last one not acknowledged" ) {
    Scope scope;
    open();
    const std::uint8_t pointer = ina228::kRegManufacturerId;
    REQUIRE( i2c_bus::write( kIna, &pointer, 1 ) == ESP_OK );
    std::array<std::uint8_t, 2> id{};
    CHECK( i2c_bus::read( kIna, id.data(), id.size() ) == ESP_OK );
    CHECK_EQ( lastWire( scope ), "S 81 r1 n1 P" );
    CHECK( id[0] == 0x54 && id[1] == 0x49 );
    // One byte: a single unacknowledged read.
    std::uint8_t first = 0;
    CHECK( i2c_bus::read( kIna, &first, 1 ) == ESP_OK );
    CHECK_EQ( lastWire( scope ), "S 81 n1 P" );
    CHECK( first == 0x54 );
}

TEST( "i2c_bus", "a register read writes the pointer and reads after a repeated START" ) {
    Scope scope;
    open();
    const std::uint8_t          pointer = ina228::kRegDeviceId;
    std::array<std::uint8_t, 2> id{};
    CHECK( i2c_bus::writeRead( kIna, &pointer, 1, id.data(), id.size() ) == ESP_OK );
    CHECK_EQ( lastWire( scope ), "S 80 3F S 81 r1 n1 P" );
    CHECK( id[0] == 0x22 && id[1] == 0x81 );
    // The longest of each side together fit both FIFOs.
    std::array<std::uint8_t, i2c_bus::kMaxWriteBytes> out{};
    out[0] = ina228::kRegShuntCal;
    std::array<std::uint8_t, i2c_bus::kMaxReadBytes> in{};
    CHECK( i2c_bus::writeRead( kIna, out.data(), out.size(), in.data(), in.size() ) == ESP_OK );
    CHECK( scope->i2c.txOverruns == 0 && scope->i2c.emptyReads == 0 );
}

TEST( "i2c_bus",
      "arguments outside a transaction's limits are refused before the bus is touched" ) {
    Scope scope;
    open();
    std::array<std::uint8_t, i2c_bus::kMaxReadBytes + 1U> room{};
    const std::uint8_t                                    byte = 0;
    const std::uint8_t                                    far  = i2c_bus::kMaxAddress + 1U;
    // write(): the address, a null buffer, nothing to send, too much.
    CHECK( i2c_bus::write( far, &byte, 1 ) == ESP_ERR_INVALID_ARG );
    CHECK( i2c_bus::write( kIna, nullptr, 1 ) == ESP_ERR_INVALID_ARG );
    CHECK( i2c_bus::write( kIna, &byte, 0 ) == ESP_ERR_INVALID_ARG );
    CHECK( i2c_bus::write( kIna, room.data(), i2c_bus::kMaxWriteBytes + 1U )
           == ESP_ERR_INVALID_ARG );
    // read(): the same four.
    CHECK( i2c_bus::read( far, room.data(), 1 ) == ESP_ERR_INVALID_ARG );
    CHECK( i2c_bus::read( kIna, nullptr, 1 ) == ESP_ERR_INVALID_ARG );
    CHECK( i2c_bus::read( kIna, room.data(), 0 ) == ESP_ERR_INVALID_ARG );
    CHECK( i2c_bus::read( kIna, room.data(), room.size() ) == ESP_ERR_INVALID_ARG );
    // writeRead(): the address, the written side, the read side.
    CHECK( i2c_bus::writeRead( far, &byte, 1, room.data(), 1 ) == ESP_ERR_INVALID_ARG );
    CHECK( i2c_bus::writeRead( kIna, &byte, 0, room.data(), 1 ) == ESP_ERR_INVALID_ARG );
    CHECK( i2c_bus::writeRead( kIna, &byte, 1, room.data(), 0 ) == ESP_ERR_INVALID_ARG );
    CHECK( scope->i2c.starts == 0 );
}

TEST( "i2c_bus",
      "a NACK on a byte ends the transaction once the bus is idle, and changes nothing" ) {
    Scope scope;
    open();
    const std::array<std::uint8_t, 3> frame = { ina228::kRegShuntCal, 0xAB, 0xCD };
    scope->i2c.nackData                     = 1;
    CHECK( i2c_bus::write( kIna, frame.data(), frame.size() ) == ESP_ERR_INVALID_RESPONSE );
    CHECK_EQ( lastWire( scope ), "S 80 02! P" );
    CHECK( scope->ina.regs[ina228::kRegShuntCal] != 0xABCD );
    CHECK( !scope->i2c.active && scope->i2c.raw == 0 );
    // On the address: the same, for a read.
    scope->i2c.nackAddress = 1;
    std::array<std::uint8_t, 2> id{ 0xEE, 0xEE };
    CHECK( i2c_bus::read( kIna, id.data(), id.size() ) == ESP_ERR_INVALID_RESPONSE );
    CHECK_EQ( lastWire( scope ), "S 81! P" );
    CHECK( id[0] == 0xEE && id[1] == 0xEE );  // untouched
    // And the next transaction is honest.
    CHECK( i2c_bus::write( kIna, frame.data(), frame.size() ) == ESP_OK );
    CHECK( scope->ina.regs[ina228::kRegShuntCal] == 0xABCD );
    CHECK( scope->i2c.resets == 1 );  // a NACK recovers nothing
}

TEST( "i2c_bus", "a NACK whose STOP never completes times out and recovers the bus" ) {
    Scope scope;
    open();
    scope->i2c.nackHolds     = true;
    scope->i2c.nackAddress   = 1;
    const sim::Micros before = scope->now();
    CHECK( i2c_bus::probe( kIna ) == ESP_ERR_TIMEOUT );
    CHECK( scope->now() - before >= i2c_bus::kTransactionBudgetUs );
    CHECK( scope->i2c.resets == 3 && scope->i2c.clears == 1 );  // two resets around the clear
    CHECK( !scope->i2c.active );
    CHECK( i2c_bus::probe( kIna ) == ESP_OK );
}

TEST( "i2c_bus", "the controller's time-out recovers the bus, which then works again" ) {
    Scope scope;
    open();
    scope->i2c.timeouts                 = 1;
    const std::uint8_t          pointer = ina228::kRegDeviceId;
    std::array<std::uint8_t, 2> id{ 0xEE, 0xEE };
    CHECK( i2c_bus::writeRead( kIna, &pointer, 1, id.data(), id.size() ) == ESP_ERR_TIMEOUT );
    CHECK( id[0] == 0xEE && id[1] == 0xEE );
    // Reset, configured, nine pulses, reset, configured again: the clock and
    // the statuses are what begin() set.
    CHECK( scope->i2c.resets == 3 && scope->i2c.clears == 1 );
    CHECK( scope->i2c.clearPulses == i2c_bus::kClearPulses );
    CHECK( !scope->i2c.fsmStuck && scope->i2c.enabled == kEvents );
    CHECK( clockNear( scope->i2c, kBoot ) && scope->i2c.master && scope->i2c.openDrain );
    CHECK( i2c_bus::writeRead( kIna, &pointer, 1, id.data(), id.size() ) == ESP_OK );
    CHECK( id[0] == 0x22 && id[1] == 0x81 );
}

TEST( "i2c_bus", "a controller that never answers is abandoned at the budget and recovered" ) {
    Scope scope;
    open();
    scope->i2c.silent          = true;
    const sim::Micros  before  = scope->now();
    const std::uint8_t pointer = ina228::kRegDeviceId;
    CHECK( i2c_bus::write( kIna, &pointer, 1 ) == ESP_ERR_TIMEOUT );
    const sim::Micros waited = scope->now() - before;
    CHECK( waited >= i2c_bus::kTransactionBudgetUs && waited < 2 * i2c_bus::kTransactionBudgetUs );
    CHECK( scope->i2c.resets == 3 && !scope->i2c.active );
    scope->i2c.silent = false;
    CHECK( i2c_bus::write( kIna, &pointer, 1 ) == ESP_OK );
}

TEST( "i2c_bus", "a bus held busy is cleared before the transaction, or refused if it stays" ) {
    {
        // A device holding SDA lets go after the nine pulses.
        Scope scope;
        open();
        scope->i2c.sdaHeldLow = true;
        CHECK( i2c_bus::probe( kIna ) == ESP_OK );
        CHECK( scope->i2c.clears == 1 && scope->i2c.resets == 3 );
        CHECK( !scope->i2c.sdaHeldLow );
        CHECK_EQ( lastWire( scope ), "S 80 P" );
    }
    {
        // One that does not: nothing is started.
        Scope scope;
        open();
        scope->i2c.sdaHeldLow      = true;
        scope->i2c.releasedByClear = false;
        CHECK( i2c_bus::probe( kIna ) == ESP_ERR_TIMEOUT );
        CHECK( scope->i2c.starts == 0 && scope->i2c.clears == 1 );
    }
}

TEST( "i2c_bus",
      "clearBus() clocks the pulses between two resets and says whether the bus is idle" ) {
    Scope scope;
    open();
    CHECK( i2c_bus::clearBus() == ESP_OK );
    CHECK( scope->i2c.clears == 1 && scope->i2c.resets == 3 );
    CHECK( clockNear( scope->i2c, kBoot ) && scope->i2c.enabled == kEvents );
    scope->i2c.sdaHeldLow      = true;
    scope->i2c.releasedByClear = false;
    CHECK( i2c_bus::clearBus() == ESP_ERR_TIMEOUT );
    scope->i2c.releasedByClear = true;
    CHECK( i2c_bus::clearBus() == ESP_OK );
    CHECK( i2c_bus::probe( kIna ) == ESP_OK );
    CHECK( scope->i2c.starts == 1 );  // the clears made no transaction
}

TEST( "i2c_bus", "every call from the other core is refused before a register is touched" ) {
    Scope scope;
    open();
    const int starts = scope->i2c.starts;
    sim::setCurrentCore( 1 );
    const std::uint8_t byte = ina228::kRegDeviceId;
    std::uint8_t       in   = 0;
    CHECK( i2c_bus::probe( kIna ) == ESP_ERR_INVALID_STATE );
    CHECK( i2c_bus::write( kIna, &byte, 1 ) == ESP_ERR_INVALID_STATE );
    CHECK( i2c_bus::read( kIna, &in, 1 ) == ESP_ERR_INVALID_STATE );
    CHECK( i2c_bus::writeRead( kIna, &byte, 1, &in, 1 ) == ESP_ERR_INVALID_STATE );
    CHECK( i2c_bus::setClock( 100000 ) == ESP_ERR_INVALID_STATE );
    CHECK( i2c_bus::clearBus() == ESP_ERR_INVALID_STATE );
    sim::setCurrentCore( 0 );
    CHECK( scope->i2c.offCoreCalls == 0 && scope->i2c.starts == starts );
    CHECK( i2c_bus::clockHz() == kBoot );
}

TEST( "i2c_bus",
      "a call that preempts a transaction in flight is refused and the first completes" ) {
    Scope scope;
    open();
    esp_err_t preemptedProbe = ESP_OK;
    esp_err_t preemptedClock = ESP_OK;
    esp_err_t preemptedClear = ESP_OK;
    scope->i2c.onStart       = [&] {
        preemptedProbe = i2c_bus::probe( kIna );
        preemptedClock = i2c_bus::setClock( 100000 );
        preemptedClear = i2c_bus::clearBus();
    };
    const std::uint8_t          pointer = ina228::kRegDeviceId;
    std::array<std::uint8_t, 2> id{};
    CHECK( i2c_bus::writeRead( kIna, &pointer, 1, id.data(), id.size() ) == ESP_OK );
    scope->i2c.onStart = {};
    CHECK( preemptedProbe == ESP_ERR_INVALID_STATE );
    CHECK( preemptedClock == ESP_ERR_INVALID_STATE );
    CHECK( preemptedClear == ESP_ERR_INVALID_STATE );
    CHECK( id[0] == 0x22 && id[1] == 0x81 );
    CHECK( scope->i2c.starts == 1 && i2c_bus::clockHz() == kBoot );
    // And the bus is free again afterwards.
    CHECK( i2c_bus::probe( kIna ) == ESP_OK );
}

TEST( "i2c_bus", "the harness's own checks see a transaction started unconfigured" ) {
    // The register fake is what the module is tested on, so its fault
    // counters are shown to count: a start with the controller still in reset
    // never runs, and a Scope would fail the case that made it.
    Scope            scope;
    i2c_dev_t* const controller = &I2C0;
    i2c_ll_start_trans( controller );
    CHECK( scope->i2c.misconfiguredStarts == 1 && scope->i2c.wire.empty() );
    CHECK( scope->i2c.writesWhileGated == 1 );
    scope->i2c.misconfiguredStarts = 0;
    scope->i2c.writesWhileGated    = 0;
}
