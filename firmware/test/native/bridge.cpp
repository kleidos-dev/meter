// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file bridge.cpp
 * @brief The firmware on this host's standard input and output: a booted
 *        bench::Bench whose UART0 is the process's two streams (FW-30).
 *
 *     make -C firmware/test/native bridge
 *     printf '*IDN?\n' | firmware/test/native/build/bridge
 *
 * What is written to it arrives at UART0 as a host's bytes do, at the line's
 * 6 Mbaud, and what the firmware transmits is written out: responses, stream
 * records and dumps, byte for byte. So a host tool can talk to the
 * firmware's own console where there is no meter, which is what the host
 * client's conformance suite does (`host/tests/conformance.py`,
 * `KLEIDOS_METER_NATIVE_BRIDGE`).
 *
 * The board is the harness's (sim.h) with the device's whole TRACE ring, a
 * fresh instrument: no calibration stored, the DUT drawing its 50 mA. Its
 * virtual time follows this machine's clock, so a capture streams at its
 * profile's rate and a null window of a second takes one; a process held up
 * for more than kMaxLagUs loses the time instead of running it at once.
 *
 * It ends when its input does, once it has run kDrainUs more of virtual time
 * so that the message in flight is answered; or when nobody reads its output.
 * The exit status is 0, 1 when one of the harness's own checks failed (the
 * ones every case of the host tests ends with, ~Bench(); each is printed on
 * the standard error), 2 for an argument: it takes none.
 */
#include "bench.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <string_view>

#include <poll.h>
#include <unistd.h>

namespace {

/// The device's TRACE ring, in samples (main.cpp).
constexpr std::size_t kDeviceRingSamples = 1'048'576;

/// Bytes sent to UART0 at once, half its receive FIFO, and how long the line
/// carries them at 6 Mbaud, 8N1.
constexpr std::size_t kChunkBytes = 64;
constexpr sim::Micros kChunkUs    = 107;

/// The longest the virtual time is run between two looks at the input, and
/// the longest those looks wait.
constexpr sim::Micros kSliceUs = 5'000;
constexpr int         kPollMs  = 2;

/// How far the virtual time may fall behind this machine's before the
/// difference is dropped.
constexpr sim::Micros kMaxLagUs = 1'000'000;

/// What is run after the input ends.
constexpr sim::Micros kDrainUs = 2'000'000;

/// The harness's checks that failed.
int failedChecks = 0;

/// This machine's clock, microseconds.
sim::Micros wallUs() {
    return std::chrono::duration_cast<std::chrono::microseconds>(
               std::chrono::steady_clock::now().time_since_epoch() )
        .count();
}

/// Writes all of @p bytes to the standard output; false when nobody reads it.
bool writeAll( std::string_view bytes ) {
    while ( !bytes.empty() ) {
        const ssize_t written = ::write( STDOUT_FILENO, bytes.data(), bytes.size() );
        if ( written > 0 ) {
            bytes.remove_prefix( static_cast<std::size_t>( written ) );
        } else if ( errno != EINTR ) {
            return false;
        }
    }
    return true;
}

/// Gives UART0 @p bytes at the line's pace.
void feed( sim::World& world, std::string_view bytes ) {
    while ( !bytes.empty() ) {
        const std::string_view chunk = bytes.substr( 0, kChunkBytes );
        world.sendRaw( chunk );
        world.run( kChunkUs );
        bytes.remove_prefix( chunk.size() );
    }
}

/// Serves the bench until its input ends or its output has no reader.
void serve( bench::Bench& bench ) {
    sim::World&            world  = bench.world();
    sim::Micros            offset = wallUs() - world.now();
    std::array<char, 4096> input  = {};
    while ( true ) {
        sim::Micros behind = wallUs() - offset - world.now();
        if ( behind > kMaxLagUs ) {
            offset += behind;
            behind = 0;
        }
        if ( behind > 0 ) {
            world.run( std::min( behind, kSliceUs ) );
        }
        if ( !writeAll( world.takeOutput() ) ) {
            return;
        }
        pollfd    waited = { .fd = STDIN_FILENO, .events = POLLIN, .revents = 0 };
        const int ready  = ::poll( &waited, 1, behind > kSliceUs ? 0 : kPollMs );
        if ( ready <= 0 ) {
            continue;  // nothing yet, or a signal
        }
        const ssize_t taken = ::read( STDIN_FILENO, input.data(), input.size() );
        if ( taken > 0 ) {
            feed( world, std::string_view( input.data(), static_cast<std::size_t>( taken ) ) );
        } else if ( taken == 0 || errno != EINTR ) {
            break;
        }
    }
    world.run( kDrainUs );
    (void)writeAll( world.takeOutput() );
}

}  // namespace

// The runner's checks (unit.h), which the bench's destructor makes: no case
// is running here, so a failed one is told on the standard error and counted.
namespace unit {

bool check( bool ok, const char* expression, const char* file, int line ) {
    if ( !ok ) {
        ++failedChecks;
        (void)std::fprintf( stderr, "bridge: %s:%d: %s\n", file, line, expression );
    }
    return ok;
}

bool checkEqual( const std::string& actual, const std::string& expected, const char* expression,
                 const char* file, int line ) {
    const bool ok = actual == expected;
    if ( !ok ) {
        (void)std::fprintf( stderr, "bridge: got \"%s\", not \"%s\"\n", actual.c_str(),
                            expected.c_str() );
    }
    return check( ok, expression, file, line );
}

}  // namespace unit

int main( int argc, char** ) {
    if ( argc != 1 ) {
        (void)std::fprintf( stderr, "usage: bridge < messages > answers (it takes no argument)\n" );
        return 2;
    }
    (void)std::signal( SIGPIPE, SIG_IGN );
    {
        bench::Bench bench( []( sim::World& w ) { w.ringSamples = kDeviceRingSamples; } );
        serve( bench );
    }
    // The firmware's tasks are threads that never end, so the process ends
    // without waiting for them or looking for what they still hold.
    (void)std::fflush( stderr );
    std::_Exit( failedChecks == 0 ? 0 : 1 );
}
