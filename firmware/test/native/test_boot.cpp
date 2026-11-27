// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_boot.cpp
 * @brief app_main() as the device runs it, on ESP-IDF's main task: the pin
 *        map read, the relays released where it puts them, then the LED, the calibration from the flash log, the sensor,
 *        the sampler, and then the console's loop on that task for good.
 */
#include "calibration.h"
#include "heap_monitor.h"
#include "pinmap.h"
#include "pinmap_store.h"
#include "relay.h"
#include "scope.h"
#include "unit.h"

#include <esp_err.h>

#include <cstddef>
#include <memory>
#include <string>

extern "C" void app_main( void );

namespace {

/// @brief Starts ESP-IDF's main task running the real app_main(), as its
///        startup does, and lets the world run the boot.
/// @param[in,out] scope The board.
/// @return Set if app_main() ever returned, which it never may (FW-17): a
///         returning app_main() deletes the main task, and the idle task
///         frees its stack after app_main()'s first line.
std::shared_ptr<bool> bootOnMainTask( bench::Scope& scope ) {
    auto returned = std::make_shared<bool>( false );
    scope->startMainTask( [returned] {
        app_main();
        *returned = true;
    } );
    scope->run( 20'000 );
    return returned;
}

}  // namespace

TEST( "boot", "app_main brings the instrument up and the console answers" ) {
    bench::Scope scope;
    // A calibration stored by an earlier boot is the one this boot loads.
    {
        kleidos::meter::CalibrationStore earlier;
        earlier.setShuntMicroOhm( 100'144 );
        REQUIRE( earlier.save() );
    }
    kleidos::meter::heap::disarm();
    const std::shared_ptr<bool> returned = bootOnMainTask( scope );
    // Every relay released: K1 (HW-316) high, the Click's bypass input low.
    CHECK( scope->pads[7].level() == 1 && scope->pads[15].level() == 0 );
    // The brownout detector armed once, as ESP-IDF armed it without an
    // interrupt: 2.44 V, a hardware reset, flash suspended, RF down (FW-18).
    CHECK( scope->brownout.configs == 1 );
    CHECK( scope->brownout.threshold == 7 && scope->brownout.enabled );
    CHECK( scope->brownout.resetEnabled );
    CHECK( scope->brownout.flashPowerDown && scope->brownout.rfPowerDown );
    (void)scope->takeOutput();
    scope->send( "*IDN?;*ESR?;SYST:ERR:COUN?" );
    scope->run( 50'000 );
    CHECK_EQ( scope->takeOutput(), "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0;128;0\n" );
    scope->send( "SENS:CURR:SHUN?" );
    scope->run( 50'000 );
    CHECK_EQ( scope->takeOutput(), "0.100144\n" );
    // The bring-up and the sampler's begin ran on the sampler's core.
    CHECK( scope->faults.ipcCpu == 1 );
    // The own I2C master was begun there, so Core 1 is the bus's (FW-14),
    // and it takes no interrupt; the ALERT pin's interrupt is routed there
    // too, and enabled; UART0's is routed on Core 0 with the console (FW-09,
    // FW-12).
    CHECK( scope->i2c.core == 1 && scope->i2c.busClock && scope->i2c.master );
    const sim::Cpu& core1 = scope->cpus[1];
    REQUIRE( core1.routes.size() == 1 && core1.routes.count( sim::kGpioSource ) == 1 );
    const int alertLine = core1.routes.at( sim::kGpioSource );
    CHECK( core1.lines.at( static_cast<std::size_t>( alertLine ) ).handler != nullptr );
    CHECK( ( core1.enabled & ( 1U << static_cast<unsigned>( alertLine ) ) ) != 0 );
    const sim::Cpu& core0 = scope->cpus[0];
    REQUIRE( core0.routes.size() == 1 && core0.routes.count( sim::kUart0Source ) == 1 );
    const int uartLine = core0.routes.at( sim::kUart0Source );
    CHECK( core0.lines.at( static_cast<std::size_t>( uartLine ) ).handler != nullptr );
    CHECK( ( core0.enabled & ( 1U << static_cast<unsigned>( uartLine ) ) ) != 0 );
    CHECK( core1.routes.count( sim::kUart0Source ) == 0 );
    // app_main() armed the heap monitor first and became the console: the
    // main task raised to the console's priority, never returning, and no
    // task of the console's own (FW-17).
    CHECK( kleidos::meter::heap::counts().armed );
    CHECK( !*returned );
    CHECK( scope->taskPriorities.at( "main" ) == 5U );
    CHECK( scope->taskPriorities.count( "meter-console" ) == 0 );
    CHECK( scope->taskPriorities.at( "meter-sample" ) == 10U );
}

TEST( "boot", "app_main brings the instrument up on its own core when the IPC call is refused" ) {
    bench::Scope scope;
    scope->faults.failIpc = true;
    CHECK( !*bootOnMainTask( scope ) );
    (void)scope->takeOutput();
    // The instrument's objects are app_main()'s statics and keep the first
    // case's state, so this case asks only that the boot ran and answers.
    scope->send( "*IDN?" );
    scope->run( 50'000 );
    CHECK_EQ( scope->takeOutput(), "KLEIDOS,KLEIDOS-METER,KM-EE529C1B03,0.18.0\n" );
    CHECK( scope->faults.ipcCpu == 1 );  // asked, refused, and run in place
    // Everything on Core 0 then: the ALERT pin's line, and UART0's on the
    // next free one, which the route skips because the first's handler is
    // set (FW-09, FW-12). (The bus stays the first case's, open, so no new
    // one.)
    const sim::Cpu& core0 = scope->cpus[0];
    REQUIRE( core0.routes.count( sim::kGpioSource ) == 1 );
    REQUIRE( core0.routes.count( sim::kUart0Source ) == 1 );
    const int alertLine = core0.routes.at( sim::kGpioSource );
    CHECK( alertLine != core0.routes.at( sim::kUart0Source ) );
    CHECK( scope->cpus[1].routes.empty() );
}

TEST( "boot", "app_main survives a bus, a flash read and a console that all fail" ) {
    bench::Scope scope;
    scope->flash.readsUntilFailure = 0;
    // A device that holds SDA low for good: every probe of the scan finds
    // the bus stuck after its recovery (FW-14).
    scope->i2c.sdaHeldLow                = true;
    scope->i2c.releasedByClear           = false;
    scope->uart.refuseBaud               = true;
    const std::shared_ptr<bool> returned = bootOnMainTask( scope );
    CHECK( scope->takeOutput().find( "console uart install failed" ) != std::string::npos );
    CHECK( scope->pads[7].level() == 1 );
    // With no console, the main task waits for good rather than return: a
    // return would free its stack (FW-17). It stays at the main task's
    // priority, and the sampler runs on.
    scope->run( 1'000'000 );
    CHECK( !*returned );
    CHECK( scope->taskPriorities.at( "main" ) == 1U );
}

TEST( "boot", "app_main on an erased calib partition reads it and writes nothing" ) {
    bench::Scope scope;
    CHECK( !*bootOnMainTask( scope ) );
    // A boot reads both sectors of each log, the pin map's and the
    // calibration's (a header each, erased), and changes nothing in the
    // flash. (What it then holds is the first case's:
    // app_main()'s statics outlive it, and a load that finds no record keeps
    // the store as it was; test_calibration.cpp shows the defaults.)
    CHECK( scope->flash.reads == 4U );
    CHECK( scope->flash.programs == 0U && scope->flash.changes == 0U );
}

TEST( "boot", "app_main drives the relays where the stored map puts them, after reading it" ) {
    bench::Scope scope;
    // Relay 1 on GPIO10 active high, Relay 2 on GPIO11 active low, ALERT with
    // no pull, the bus the rig's, and the board wired so.
    kleidos::meter::pinmap::PinMap map = kleidos::meter::pinmap::kDefaultMap;
    map.pins                           = { 4, 5, 6, 10, 11 };
    map.relay1Drive                    = kleidos::meter::relay::Drive::ActiveHigh;
    map.relay2Drive                    = kleidos::meter::relay::Drive::ActiveLow;
    map.pullAlert                      = kleidos::meter::pinmap::Pull::Off;
    REQUIRE( kleidos::meter::pinmap_store::save( map ) == kleidos::meter::pinmap_store::Saved::Ok );
    scope->wires.lead           = 10;
    scope->wires.bypass         = 11;
    scope->wires.leadActiveHigh = true;
    scope->circuit.bypassPart   = sim::BypassPart::Hw316;
    CHECK( !*bootOnMainTask( scope ) );
    CHECK( scope->pads[10].firstDriven == 0 && scope->pads[11].firstDriven == 1 );
    CHECK( scope->pads[10].driving() && scope->pads[11].driving() );
    CHECK( !scope->leadOpen() && !scope->bypassClosed() && scope->circuit.disconnects == 0 );
    // The rig's own relay pins are parked: an input with the pull-down.
    for ( const std::size_t gpio : { 7U, 15U } ) {
        CHECK( !scope->pads[gpio].output && scope->pads[gpio].pullDown );
    }
    CHECK( scope->pads[6].input && !scope->pads[6].pullUp );
    (void)scope->takeOutput();
    scope->send( "SYST:PINS?" );
    scope->run( 50'000 );
    CHECK_EQ( scope->takeOutput(),
              "sda=4 scl=5 alert=6 addr=auto scl_hz=400000 pull_sda=up pull_scl=up "
              "pull_alert=off relay1=10 relay1_active=high relay2=11 relay2_active=low "
              "src=stored verified=0 table=0x90072FE9\n" );
}
