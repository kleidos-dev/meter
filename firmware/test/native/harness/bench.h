// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file bench.h
 * @brief The whole instrument on the host: a sim::World, the firmware's own
 *        objects brought up the way app_main() brings them up, and the line a
 *        host speaks SCPI over.
 *
 *     Bench bench;                      // boots, announces, ready for SCPI
 *     CHECK_EQ( bench.ask( "*IDN?" ), "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0" );
 *
 * Faults are set on `bench.world()` before boot through the Setup callback,
 * or afterwards directly.
 */
#pragma once

#include "console.h"
#include "heap_monitor.h"
#include "idf_ledger.h"
#include "pin_boot.h"
#include "sim.h"
#include "status_led.h"
#include "unit.h"
#include "watchdog.h"

#include <esp_ipc.h>

#include <cstring>
#include <functional>
#include <string>
#include <utility>
#include <vector>

namespace bench {

using namespace kleidos::meter;

/// Every unit of one response line, split at `;` outside quotes.
std::vector<std::string> units( const std::string& line );

/// The value of `key=` in a report, or "" when absent.
std::string field( const std::string& report, const std::string& key );

class Bench {
   public:
    using Setup = std::function<void( sim::World& )>;

    /// Builds the world, lets @p setup arrange it, and boots the firmware;
    /// @p inGap runs once the console's UART is up and before the
    /// announcement, which follows @p announceGapUs later (what the port
    /// receives meanwhile is discarded by the announcement).
    explicit Bench( const Setup& setup = {}, bool boot = true, sim::Micros announceGapUs = 0,
                    Setup inGap = {} )
        : world_( new sim::World() ),
          announceGapUs_( announceGapUs ),
          inGap_( std::move( inGap ) ) {
        // A new board: its RTC memory holds nothing a previous one left. A
        // test that stands for a warm reset writes the record in @p setup.
        relay::detail::resetRecord() = relay::ResetRecord{};
        if ( setup ) {
            setup( *world_ );
        }
        if ( boot ) {
            this->boot();
        }
    }

    Bench( const Bench& )            = delete;
    Bench& operator=( const Bench& ) = delete;

    /// Every case that boots a Bench also shows that everything it asked of
    /// ESP-IDF since its `app_main()` (the boot, then every command) is no
    /// allocating entry point of idf_ledger.h but the calls the image's
    /// configuration makes safe: any other, a new allocation on the device,
    /// fails the case and names the entry point.
    ///
    /// And that no level interrupt was left pending by its handler (a storm,
    /// sim::World::gpioStorms) and the interrupt mask is back where it was;
    /// that the peripheral clock lock was let go and the RMT reached only
    /// inside it and only while clocked (FW-10); and that no pad the RMT
    /// drives was left at no level. And that Timer Group 0's watchdog was
    /// reached only unlocked and clocked, its timer group's clock only under
    /// the lock, its key left locked, and that it never ran out (the device
    /// would have reset) unless the case provoked it (Mwdt::resetExpected,
    /// FW-11). And that UART0 was reached from Core 0 alone, its clock and
    /// reset only under the lock, no byte written into a full transmit FIFO
    /// or read from an empty receive one, and its level interrupt never left
    /// pending by its handler (FW-12). And that I2C0's registers were reached
    /// from the core that clocked them alone (0.16.5's rule, `docs/
    /// architecture.md` SS 2.2: the bus is on Core 1 and the console on
    /// Core 0, so a command that reached it from its own core fails the case
    /// it ran in), clocked, its clock and reset only under the lock, no
    /// transaction started unconfigured, over another or from a command list
    /// the hardware would not run as meant, and no FIFO overrun or underrun
    /// (FW-13, FW-14). And that the record logs' flash was written only
    /// where it reads erased, never outside its window, erased only a whole
    /// sector at a time (FW-15), and never asked for a write or an erase the
    /// image's region check refuses (FW-16).
    ~Bench() {
        CHECK( world_->gpioStorms == 0 );
        CHECK( world_->interruptMaskDepth == 0 );
        CHECK( world_->rccDepth == 0 );
        CHECK( world_->rmt.rccViolations == 0 );
        CHECK( world_->rmt.writesWhileGated == 0 );
        CHECK( world_->rmt.undefinedLevels == 0 );
        CHECK( world_->mwdt.lockedWrites == 0 );
        CHECK( world_->mwdt.writesWhileGated == 0 );
        CHECK( world_->mwdt.locked );
        CHECK( world_->timerGroups[0].rccViolations == 0 );
        world_->noteMwdt();
        CHECK( world_->mwdt.resetExpected || world_->mwdt.resetAtUs < 0 );
        CHECK( world_->uart.rccViolations == 0 );
        CHECK( world_->uart.offCoreCalls == 0 );
        CHECK( world_->uart.txOverruns == 0 );
        CHECK( world_->uart.emptyReads == 0 );
        CHECK( world_->uart.storms == 0 );
        CHECK( world_->i2c.rccViolations == 0 );
        CHECK( world_->i2c.offCoreCalls == 0 );
        CHECK( world_->i2c.writesWhileGated == 0 );
        CHECK( world_->i2c.misconfiguredStarts == 0 );
        CHECK( world_->i2c.startsWhileActive == 0 );
        CHECK( world_->i2c.protocolErrors == 0 );
        CHECK( world_->i2c.txOverruns == 0 );
        CHECK( world_->i2c.emptyReads == 0 );
        CHECK( world_->flash.zeroToOne == 0 );
        CHECK( world_->flash.outOfWindow == 0 );
        CHECK( world_->flash.unalignedErase == 0 );
        CHECK( world_->flash.refusedByRegion == 0 );
        CHECK( world_->flash.bounceBufferReads == 0 );
        CHECK_EQ( unlistedIdfCalls(), "" );
        world_->leave();
    }

    /// The calls since this bench's `app_main()` of an allocating ESP-IDF
    /// entry point that idf_ledger.h does not list as made safe, joined (""
    /// when none): what the destructor requires empty.
    std::string unlistedIdfCalls() const {
        return sim::unlistedIdfAllocationText( world_->idfLedger );
    }

    /// app_main()'s order, on objects that live as long as the process: the
    /// heap monitor armed first, as its first line, the pin map read before
    /// anything is driven, and the console's loop
    /// run last, on ESP-IDF's main task (sim::World::startMainTask(), FW-17),
    /// which the boot itself stands for on the test's thread.
    void boot() {
        heap::arm();
        pins_        = new pin_boot::Live( pin_boot::load() );
        relays_      = new relay::Bank();
        calibration_ = new CalibrationStore();
        device_      = new Ina228Driver();
        sampler_     = new Sampler();
        console_     = new Console();
        (void)relays_->begin( pins_->map );
        led::blankAll();
        (void)watchdog::begin();
        (void)calibration_->load();
        // The bus and the sampler come up on the sampler's core, as
        // app_main() brings them up (main.cpp), so the bus is Core 1's and
        // the console's transactions cross cores; in place, on the console's,
        // when the IPC call is refused.
        BringUpResult result        = BringUpResult::BusError;
        auto          onSamplerCore = [&] {
            result                    = pin_boot::bringUp( *device_, *pins_ );
            const pinmap::PinMap& map = pins_->map;
            samplerReady_             = sampler_->begin(
                *device_, *calibration_,
                static_cast<gpio_num_t>( map.pins[pinmap::roleIndex( pin_table::Role::Alert )] ),
                map.pullAlert == pinmap::Pull::Up, world_->ring() );
        };
        if ( esp_ipc_call_blocking(
                 1, []( void* run ) { ( *static_cast<decltype( onSamplerCore )*>( run ) )(); },
                 &onSamplerCore )
             != ESP_OK ) {
            onSamplerCore();
        }
        consoleReady_ = console_->begin( *device_, *sampler_, *calibration_, *relays_, *pins_ );
        if ( consoleReady_ ) {
            if ( inGap_ ) {
                inGap_( *world_ );
            }
            if ( announceGapUs_ > 0 ) {
                world_->run( announceGapUs_ );  // the port receives, nothing reads
            }
            console_->announce( result, samplerReady_ );
            world_->startMainTask( [console = console_] { console->run(); } );
        }
        bringUp_ = result;
        world_->run( 10'000 );
        bootOutput_ = world_->takeOutput();
    }

    /// Everything the instrument wrote during boot(): the announcement and
    /// the errors' text it printed, which the golden transcript keeps
    /// (test_golden.cpp) and every other case discards.
    const std::string& bootOutput() const { return bootOutput_; }

    sim::World& world() { return *world_; }

    /// Sends one program message and returns the response line without its
    /// newline ("" when the message produced none within @p timeoutUs).
    std::string ask( const std::string& message, sim::Micros timeoutUs = 200'000 ) {
        (void)world_->takeOutput();
        world_->send( message );
        std::string collected;
        (void)world_->runUntil(
            [&] {
                collected += world_->takeOutput();
                return response( collected ) != nullptr;
            },
            timeoutUs );
        collected += world_->takeOutput();
        const char* line = response( collected );
        return line != nullptr ? std::string( line, std::strchr( line, '\n' ) ) : std::string();
    }

    /// Sends a command that answers nothing, then lets it run.
    void tell( const std::string& message, sim::Micros runUs = 50'000 ) {
        world_->send( message );
        world_->run( runUs );
    }

    /// Everything the instrument wrote since the last call (stream records).
    std::string drain() { return world_->takeOutput(); }

    /// The error queue, as `SYSTem:ERRor:ALL?` returns it.
    std::string errors() { return ask( "SYST:ERR:ALL?" ); }

    /// True when the queue holds nothing.
    bool clean() { return errors() == "0,\"No error\""; }

    const pin_boot::Live& pins() const { return *pins_; }
    relay::Bank&          relays() { return *relays_; }
    CalibrationStore&     calibration() { return *calibration_; }
    Ina228Driver&         device() { return *device_; }
    Sampler&              sampler() { return *sampler_; }
    BringUpResult         bringUp() const { return bringUp_; }
    bool                  samplerReady() const { return samplerReady_; }
    bool                  consoleReady() const { return consoleReady_; }

   private:
    /// The first complete line of @p text that is not a stream record.
    static const char* response( const std::string& text ) {
        const char* cursor = text.c_str();
        while ( *cursor != '\0' ) {
            const char* end = std::strchr( cursor, '\n' );
            if ( end == nullptr ) {
                return nullptr;
            }
            const bool record = std::strncmp( cursor, "METER ", 6 ) == 0
                                || std::strncmp( cursor, "S,", 2 ) == 0
                                || std::strncmp( cursor, "K,", 2 ) == 0
                                || std::strncmp( cursor, "METER_MARK ", 11 ) == 0;
            if ( !record ) {
                return cursor;
            }
            cursor = end + 1;
        }
        return nullptr;
    }

    sim::World*       world_         = nullptr;
    sim::Micros       announceGapUs_ = 0;
    Setup             inGap_;
    std::string       bootOutput_;
    pin_boot::Live*   pins_         = nullptr;
    relay::Bank*      relays_       = nullptr;
    CalibrationStore* calibration_  = nullptr;
    Ina228Driver*     device_       = nullptr;
    Sampler*          sampler_      = nullptr;
    Console*          console_      = nullptr;
    BringUpResult     bringUp_      = BringUpResult::NoDevice;
    bool              samplerReady_ = false;
    bool              consoleReady_ = false;
};

inline std::vector<std::string> units( const std::string& line ) {
    std::vector<std::string> out;
    std::string              current;
    bool                     quoted = false;
    for ( char c : line ) {
        if ( c == '"' ) {
            quoted = !quoted;
        }
        if ( c == ';' && !quoted ) {
            out.push_back( current );
            current.clear();
            continue;
        }
        current += c;
    }
    out.push_back( current );
    return out;
}

inline std::string field( const std::string& report, const std::string& key ) {
    const std::string needle = key + "=";
    std::size_t       at     = 0;
    while ( ( at = report.find( needle, at ) ) != std::string::npos ) {
        if ( at == 0 || report[at - 1] == ' ' || report[at - 1] == ';' ) {
            const std::size_t start = at + needle.size();
            const std::size_t end   = report.find_first_of( " ;", start );
            return report.substr( start,
                                  end == std::string::npos ? std::string::npos : end - start );
        }
        at += needle.size();
    }
    return "";
}

}  // namespace bench
