// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_uart0.cpp
 * @brief uart0.cpp on the simulated UART0's registers: the port's set-up and
 *        its refusals, the receive interrupt and ring, a ring that fills and
 *        is refilled, the overflow, the waits, and the polled transmit on a
 *        line that carries, stalls, and comes back.
 */
#include "scope.h"
#include "uart0.h"
#include "unit.h"

#include <hal/uart_ll.h>
#include <soc/gpio_sig_map.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <utility>

using namespace kleidos::meter;
using bench::Scope;

namespace {

/// The line's rate the console asks for (`kConsoleBaud`).
constexpr std::uint32_t kBaud = 6000000;

/// The three receive interrupts begin() enables.
constexpr std::uint32_t kRxInterrupts =
    UART_INTR_RXFIFO_FULL | UART_INTR_RXFIFO_TOUT | UART_INTR_RXFIFO_OVF;

/// @p count bytes of a pattern that does not repeat within a ring.
std::string pattern( std::size_t count ) {
    std::string out;
    for ( std::size_t i = 0; i < count; ++i ) {
        out += static_cast<char>( 'A' + static_cast<int>( i % 251U ) % 58 );
    }
    return out;
}

/// Reads what read() has without waiting, until it has nothing.
std::string readAll() {
    std::string                           out;
    std::array<char, uart0::kRxRingBytes> chunk{};
    for ( ;; ) {
        const std::size_t count = uart0::read( chunk.data(), chunk.size(), 0 );
        if ( count == 0 ) {
            return out;
        }
        out.append( chunk.data(), count );
    }
}

/// Runs @p body once at @p atUs of the world's virtual time.
void at( sim::World& world, sim::Micros atUs, std::function<void()> body ) {
    const std::size_t index = world.addInterrupt( {} );
    sim::Interrupt&   once  = world.interrupt( index );
    once.handler            = [&world, index, body = std::move( body )] {
        world.interrupt( index ).enabled = false;
        body();
    };
    once.enabled = true;
    once.period  = sim::kForever / 4;
    once.next    = atUs;
}

}  // namespace

TEST( "uart0", "begin() sets 6 Mbps 8N1 on the IO MUX pins and routes the interrupt here" ) {
    Scope scope;
    REQUIRE( uart0::begin( kBaud ) == ESP_OK );
    const sim::Uart& uart = scope->uart;
    CHECK( uart.busClock && uart.resets == 1 );
    CHECK( uart.sourceClock == SOC_MOD_CLK_APB );
    CHECK( uart.baud == kBaud && uart.sclkHz == uart0::kSourceClockHz );
    CHECK( uart.dataBits == UART_DATA_8_BITS && uart.parity == UART_PARITY_DISABLE );
    CHECK( uart.stopBits == UART_STOP_BITS_1 && uart.flowCtrl == UART_HW_FLOWCTRL_DISABLE );
    CHECK( uart.normalMode && uart.txIdle == 0 );
    CHECK( uart.rxFullThreshold == uart0::kRxFullThreshold );
    CHECK( uart.rxTimeoutBits == uart0::kRxTimeoutBits );
    CHECK( uart.rxFifoResets == 1 && uart.txFifoResets == 1 );
    CHECK( uart.enabled == kRxInterrupts );
    // Both pins on UART0's IO MUX function, RX's input off the matrix.
    CHECK( scope->pads[uart0::kTxGpio].function == 0 );
    CHECK( scope->pads[uart0::kRxGpio].function == 0 && scope->pads[uart0::kRxGpio].input );
    REQUIRE( scope->inputFromMatrix.count( U0RXD_IN_IDX ) == 1 );
    CHECK( !scope->inputFromMatrix.at( U0RXD_IN_IDX ) );
    // A level-1 line of this core (the test's thread stands for Core 0).
    const sim::Cpu& core0 = scope->cpus[0];
    REQUIRE( core0.routes.count( sim::kUart0Source ) == 1 );
    const int line = core0.routes.at( sim::kUart0Source );
    CHECK( core0.lines.at( static_cast<std::size_t>( line ) ).handler != nullptr );
    CHECK( ( core0.enabled & ( 1U << static_cast<unsigned>( line ) ) ) != 0 );
    CHECK( scope->idfLedger.empty() );  // nothing ESP-IDF allocates
}

TEST( "uart0",
      "begin() refuses a rate, a semaphore or a line it cannot have, and routes nothing" ) {
    {
        Scope scope;
        scope->uart.refuseBaud = true;
        CHECK( uart0::begin( kBaud ) == ESP_ERR_INVALID_ARG );
        CHECK( scope->cpus[0].routes.empty() && scope->uart.enabled == 0 );
    }
    {
        Scope scope;
        scope->faults.failSemaphore = true;
        CHECK( uart0::begin( kBaud ) == ESP_ERR_NO_MEM );
        CHECK( scope->cpus[0].routes.empty() && scope->uart.enabled == 0 );
    }
    {
        Scope scope;
        for ( sim::CpuLine& line : scope->cpus[0].lines ) {
            line.handler = +[]( void* ) {};
        }
        CHECK( uart0::begin( kBaud ) == ESP_ERR_NOT_FOUND );
        CHECK( scope->cpus[0].routes.empty() && scope->uart.enabled == 0 );
    }
}

TEST( "uart0", "what comes is read in order, as much as there is room for, and never waited for" ) {
    Scope scope;
    REQUIRE( uart0::begin( kBaud ) == ESP_OK );
    std::array<char, 16> out{};
    CHECK( uart0::read( out.data(), out.size(), 0 ) == 0 );  // nothing yet
    // Below the FIFO's threshold: the time-out moves it into the ring.
    scope->sendRaw( "abcdef" );
    CHECK( scope->uart.rxFifo.empty() );
    CHECK( uart0::read( out.data(), 4, 0 ) == 4 );
    CHECK_EQ( std::string( out.data(), 4 ), "abcd" );
    CHECK( uart0::read( out.data(), out.size(), 0 ) == 2 );
    CHECK_EQ( std::string( out.data(), 2 ), "ef" );
    // No room, or nowhere to put it: nothing is taken.
    scope->sendRaw( "g" );
    CHECK( uart0::read( nullptr, out.size(), 0 ) == 0 );
    CHECK( uart0::read( out.data(), 0, 0 ) == 0 );
    CHECK( uart0::read( out.data(), out.size(), 0 ) == 1 && out[0] == 'g' );
    // Above the threshold the FIFO is emptied as it fills, so a burst wider
    // than the FIFO loses nothing.
    const std::string burst = pattern( 900 );
    scope->sendRaw( burst );
    CHECK( scope->uart.lostBytes == 0 );
    CHECK_EQ( readAll(), burst );
}

TEST( "uart0", "a read waits for a first byte, and returns when one comes" ) {
    Scope scope;
    REQUIRE( uart0::begin( kBaud ) == ESP_OK );
    std::array<char, 16> out{};
    // Nothing comes: the whole wait, then nothing.
    const sim::Micros start = scope->now();
    CHECK( uart0::read( out.data(), out.size(), 5 ) == 0 );
    CHECK( scope->now() - start >= 5'000 );
    // A byte 1 ms into a 10 ms wait ends it then.
    const sim::Micros begun = scope->now();
    at( *scope.world, begun + 1'000, [&scope] { scope->sendRaw( "z" ); } );
    CHECK( uart0::read( out.data(), out.size(), 10 ) == 1 && out[0] == 'z' );
    CHECK( scope->now() - begun < 2'000 );
    // A give left by bytes already taken does not end the next wait early.
    scope->sendRaw( "y" );
    CHECK( uart0::read( out.data(), out.size(), 0 ) == 1 );
    const sim::Micros again = scope->now();
    CHECK( uart0::read( out.data(), out.size(), 3 ) == 0 );
    CHECK( scope->now() - again >= 3'000 );
}

TEST( "uart0", "a full ring leaves the rest in the FIFO, and a read refills it in order" ) {
    Scope scope;
    REQUIRE( uart0::begin( kBaud ) == ESP_OK );
    const std::string burst = pattern( uart0::kRxRingBytes + 100 );
    scope->sendRaw( burst );
    // The ring took 1,024; the FIFO holds the rest, its data interrupts off
    // (the overflow's still on), and nothing is lost.
    CHECK( scope->uart.rxFifo.size() == 100 );
    CHECK( scope->uart.enabled == UART_INTR_RXFIFO_OVF );
    CHECK( scope->uart.lostBytes == 0 );
    // Ten bytes of room take ten from the FIFO: the ring stays full, the
    // interrupts off.
    std::array<char, uart0::kRxRingBytes> out{};
    CHECK( uart0::read( out.data(), 10, 0 ) == 10 );
    std::string got( out.data(), 10 );
    CHECK( scope->uart.rxFifo.size() == 90 );
    CHECK( scope->uart.enabled == UART_INTR_RXFIFO_OVF );
    // A read that makes room for the rest empties the FIFO and turns them on.
    got += readAll();
    CHECK( scope->uart.rxFifo.empty() );
    CHECK( scope->uart.enabled == kRxInterrupts );
    CHECK_EQ( got, burst );
    // And the next byte comes in through the interrupt again.
    scope->sendRaw( "!" );
    CHECK_EQ( readAll(), "!" );
}

TEST( "uart0", "an overflow is latched once and forgotten once taken; the FIFO starts again" ) {
    Scope scope;
    REQUIRE( uart0::begin( kBaud ) == ESP_OK );
    CHECK( !uart0::takeOverrun() );
    // The ring's 1,024 and the FIFO's 128 hold; the next byte is lost.
    const std::string burst = pattern( uart0::kRxRingBytes + uart0::kFifoBytes + 1 );
    scope->sendRaw( burst );
    CHECK( scope->uart.lostBytes == 1 );
    CHECK( scope->uart.rxFifoResets == 2 );  // begin()'s and the overflow's
    CHECK( scope->uart.rxFifo.empty() );
    CHECK( uart0::takeOverrun() );
    CHECK( !uart0::takeOverrun() );
    // What the ring held before it is intact; the FIFO's 128 went with it.
    CHECK_EQ( readAll(), burst.substr( 0, uart0::kRxRingBytes ) );
    CHECK( scope->uart.enabled == kRxInterrupts );
}

TEST( "uart0", "write puts every byte on the line in order, a FIFO at a time" ) {
    Scope scope;
    REQUIRE( uart0::begin( kBaud ) == ESP_OK );
    const std::string text = pattern( 1000 );
    CHECK( uart0::write( text.data(), text.size() ) == text.size() );
    CHECK_EQ( scope->takeOutput(), text );
    CHECK( uart0::write( nullptr, 5 ) == 0 );
    CHECK( uart0::write( text.data(), 0 ) == 0 );
    CHECK_EQ( scope->takeOutput(), "" );
}

TEST( "uart0", "a line that takes nothing: write gives up after its budget with what it placed" ) {
    Scope scope;
    REQUIRE( uart0::begin( kBaud ) == ESP_OK );
    const std::string text = pattern( 300 );
    scope->stallTx();
    const sim::Micros start = scope->now();
    CHECK( uart0::write( text.data(), text.size() ) == uart0::kFifoBytes );
    CHECK( scope->now() - start >= uart0::kTxStallMicroseconds );
    CHECK_EQ( scope->takeOutput(), "" );
    scope->resumeTx();  // the FIFO's 128 go out
    CHECK_EQ( scope->takeOutput(), text.substr( 0, uart0::kFifoBytes ) );
    // A line that carries 50 bytes, then stalls: 50 and a full FIFO placed.
    scope->uart.txBudget = 50;
    CHECK( uart0::write( text.data(), text.size() ) == 50 + uart0::kFifoBytes );
    CHECK_EQ( scope->takeOutput(), text.substr( 0, 50 ) );
    scope->resumeTx();
    (void)scope->takeOutput();
}

TEST( "uart0", "a line that stalls and comes back inside the budget loses nothing" ) {
    Scope scope;
    REQUIRE( uart0::begin( kBaud ) == ESP_OK );
    const std::string text = pattern( 400 );
    scope->stallTx();
    scope->resumeTxAt( scope->now() + 2'000 );
    CHECK( uart0::write( text.data(), text.size() ) == text.size() );
    CHECK_EQ( scope->takeOutput(), text );
}

TEST( "uart0", "two stalls, each inside the budget and longer than it together, lose nothing" ) {
    Scope scope;
    REQUIRE( uart0::begin( kBaud ) == ESP_OK );
    const std::string     text   = pattern( 600 );
    const sim::Micros     start  = scope->now();
    constexpr sim::Micros kStall = 6'000;  // each stall: less than kTxStallMicroseconds
    static_assert( kStall < uart0::kTxStallMicroseconds
                   && 2 * kStall > uart0::kTxStallMicroseconds );
    // The line takes nothing until 6 ms, then 100 bytes, then nothing until
    // 12 ms: the FIFO is full for 12 ms in all, but never 10 ms at a stretch,
    // so every byte goes out. The clock of a stall restarts once the FIFO
    // takes a byte (uart0.h, write()).
    scope->stallTx();
    const std::size_t index   = scope->addInterrupt( {} );
    sim::Interrupt&   briefly = scope->interrupt( index );
    sim::World&       world   = *scope.world;
    briefly.handler           = [&world, index] {
        world.interrupt( index ).enabled = false;
        world.resumeTx();
        world.uart.txBudget = 100;  // then stalled again
    };
    briefly.enabled = true;
    briefly.period  = sim::kForever / 4;  // one firing, then disabled
    briefly.next    = start + kStall;
    scope->resumeTxAt( start + 2 * kStall );
    CHECK( uart0::write( text.data(), text.size() ) == text.size() );
    CHECK( scope->now() - start >= 2 * kStall );
    CHECK_EQ( scope->takeOutput(), text );
}
