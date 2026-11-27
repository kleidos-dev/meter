// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file scope.h
 * @brief A bare simulated board for one test, with no firmware booted: for
 *        the modules tested on their own (calibration, relays, the driver).
 */
#pragma once

#include "idf_ledger.h"
#include "sim.h"
#include "unit.h"

namespace bench {

/// A bare board. Like bench::Bench, it fails the case whose code (the
/// module under test, or the boot suite's real `app_main()`) called an
/// allocating ESP-IDF entry point idf_ledger.h does not list as made safe,
/// left a level interrupt pending after its handler, left the interrupts masked, or
/// left the peripheral clock lock held, reached the RMT's clock outside it,
/// wrote to the RMT while it was gated, or left a pad it drives at no level,
/// or reached Timer Group 0's watchdog locked or unclocked, or its group's
/// clock outside the lock, or left its key unlocked, or let it run out, or
/// reached UART0 off Core 0 or its clock outside the lock, wrote into its
/// full transmit FIFO, read its empty receive one, or left its interrupt
/// pending, or reached I2C0 off the core that clocked it, unclocked or its
/// clock outside the lock, started a transaction unconfigured, over another
/// or from a malformed command list, or overran or underran one of its FIFOs,
/// or wrote a 1 over a 0 of the record logs' flash, reached it outside
/// its window, erased it other than a sector at a time, or asked for a write
/// or an erase the image's region check refuses.
struct Scope {
    sim::World* world                = new sim::World();
    Scope()                          = default;
    Scope( const Scope& )            = delete;
    Scope& operator=( const Scope& ) = delete;
    ~Scope() {
        CHECK_EQ( sim::unlistedIdfAllocationText( world->idfLedger ), "" );
        CHECK( world->gpioStorms == 0 );
        CHECK( world->interruptMaskDepth == 0 );
        CHECK( world->rccDepth == 0 );
        CHECK( world->rmt.rccViolations == 0 );
        CHECK( world->rmt.writesWhileGated == 0 );
        CHECK( world->rmt.undefinedLevels == 0 );
        CHECK( world->mwdt.lockedWrites == 0 );
        CHECK( world->mwdt.writesWhileGated == 0 );
        CHECK( world->mwdt.locked );
        CHECK( world->timerGroups[0].rccViolations == 0 );
        world->noteMwdt();
        CHECK( world->mwdt.resetExpected || world->mwdt.resetAtUs < 0 );
        CHECK( world->uart.rccViolations == 0 );
        CHECK( world->uart.offCoreCalls == 0 );
        CHECK( world->uart.txOverruns == 0 );
        CHECK( world->uart.emptyReads == 0 );
        CHECK( world->uart.storms == 0 );
        CHECK( world->i2c.rccViolations == 0 );
        CHECK( world->i2c.offCoreCalls == 0 );
        CHECK( world->i2c.writesWhileGated == 0 );
        CHECK( world->i2c.misconfiguredStarts == 0 );
        CHECK( world->i2c.startsWhileActive == 0 );
        CHECK( world->i2c.protocolErrors == 0 );
        CHECK( world->i2c.txOverruns == 0 );
        CHECK( world->i2c.emptyReads == 0 );
        CHECK( world->flash.zeroToOne == 0 );
        CHECK( world->flash.outOfWindow == 0 );
        CHECK( world->flash.unalignedErase == 0 );
        CHECK( world->flash.refusedByRegion == 0 );
        CHECK( world->flash.bounceBufferReads == 0 );
        world->leave();
    }
    sim::World* operator->() { return world; }
};

}  // namespace bench
