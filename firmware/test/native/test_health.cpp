// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_health.cpp
 * @brief 0.12.0 over the line: the real `*TST?` with each simulated fault on
 *        its own bit, `SYSTem:HEALth?`, the QUEStionable TIME and data-lost
 *        bits, the transition filters, the hold's asynchronous error, and
 *        `CONFigure`/`MEASure` of a bus function selecting STATE.
 */
#include "bench.h"
#include "flash_log.h"
#include "flash_port.h"
#include "unit.h"

#include <array>
#include <cstdint>
#include <cstdlib>
#include <string>
#include <vector>

using bench::Bench;
using bench::field;
namespace reg = kleidos::meter::ina228;

TEST( "health", "*TST? passes a healthy instrument and names each fault by its bit" ) {
    Bench b;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "0" );
    // Each fault on its own: the identity read back wrong, SHUNT_CAL lost,
    // the trim memory, the ALERT wire, a relay pad, the stored calibration,
    // a task's stack.
    b.world().ina.regs[reg::kRegManufacturerId] = 0x1234;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "8" );
    b.world().ina.regs[reg::kRegManufacturerId] = 0x5449;
    b.world().ina.regs[reg::kRegShuntCal]       = 0x0001;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "8" );
    b.world().ina.regs[reg::kRegShuntCal] = b.device().shuntCal();
    b.world().ina.failRegister            = reg::kRegDeviceId;
    b.world().ina.failCount               = 1;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "8" );
    b.world().ina.failRegister            = -1;
    b.world().ina.regs[reg::kRegDeviceId] = 0x1231;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "8" );
    b.world().ina.regs[reg::kRegDeviceId] = b.world().ina.deviceId;
    b.world().ina.memStat                 = false;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "16" );
    b.world().ina.memStat      = true;
    b.world().ina.failRegister = reg::kRegDiagAlrt;
    b.world().ina.failCount    = 1;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "16" );
    b.world().ina.failRegister = -1;
    b.world().ina.alertPin     = sim::Ina228Model::AlertPin::Absent;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "32" );
    b.world().ina.alertPin   = sim::Ina228Model::AlertPin::HeldUntilRead;
    b.world().pads[7].forced = 0;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "64" );
    b.world().pads[7].forced          = -1;
    b.world().flash.readsUntilFailure = 0;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "128" );
    b.world().stackFreeBytes = 100;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "256" );
    b.world().stackFreeBytes = 2048;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "0" );
    CHECK( b.clean() );
    // It reads the device, so it waits for no window.
    b.tell( "SENS:PROF STAT;:INIT" );
    b.tell( "*TST?" );
    CHECK_CONTAINS( b.errors(), "-221," );
    b.tell( "ABOR" );
}

TEST( "health", "*TST? passes when a conversion ends inside its ADC_CONFIG write" ) {
    // 0.16.4: the pin goes low, the write clears CNVRF behind it, and no
    // DIAG_ALRT read releases it until a later conversion sets CNVRF again.
    // The probe's own read came 0.3 ms after the write, before any, so the
    // pin stayed low through its window: no edge, bit 32, in 15 of 69 calls
    // on the bench (2026-11-21).
    Bench b;
    for ( int i = 0; i < 4; ++i ) {
        b.world().ina.conversionEndsInAdcConfigWrites = 1;
        CHECK_EQ( b.ask( "*TST?", 500'000 ), "0" );
        CHECK( b.world().ina.conversionEndsInAdcConfigWrites == 0 );
    }
    CHECK( b.clean() );
}

TEST( "health", "*TST? on an instrument whose INA228 never came up" ) {
    Bench b( []( sim::World& w ) { w.ina.present = false; } );
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "1" );
}

TEST( "health", "SYSTem:HEALth? names the reset, the image, the stacks and the counts" ) {
    Bench             b( []( sim::World& w ) { w.resetReason = 6; } );
    const std::string health = b.ask( "SYST:HEAL?" );
    CHECK_EQ( field( health, "reset" ), "task_wdt" );
    CHECK_EQ( field( health, "fw" ), "0.18.0" );
    CHECK_EQ( field( health, "elf" ), field( b.ask( "SYST:INF?" ), "elf" ) );
    CHECK_EQ( field( health, "stack_console" ), "2048" );
    CHECK_EQ( field( health, "stack_sampler" ), "2048" );
    CHECK_EQ( field( health, "allocs_after_init" ), "0" );
    CHECK_EQ( field( health, "hold_released" ), "0" );
    CHECK_EQ( field( health, "errors" ), "0" );
    CHECK( std::stoull( field( health, "uptime_us" ) ) > 0U );
    Bench strange( []( sim::World& w ) { w.resetReason = 99; } );
    CHECK_EQ( field( strange.ask( "SYST:HEAL?" ), "reset" ), "unknown" );
    Bench dead( []( sim::World& w ) {
        w.faults.failTask     = true;
        w.faults.failTaskName = "meter-sample";
    } );
    CHECK_EQ( field( dead.ask( "SYST:HEAL?" ), "stack_sampler" ), "0" );
}

TEST( "health", "QUEStionable TIME and data-lost bits follow the open or last capture" ) {
    Bench b;
    b.tell( "STAT:QUES?" );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "256" );  // uncalibrated, as booted
    // A failed read in the capture: TIME, kept after ABORt, cleared by the
    // next capture.
    b.tell( "SENS:PROF STAT;:INIT", 20'000 );
    b.world().ina.failRegister = reg::kRegVShunt;
    b.world().ina.failCount    = 1;
    b.world().run( 20'000 );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), std::to_string( 256 + 4 ) );
    b.tell( "ABOR" );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), std::to_string( 256 + 4 ) );
    CHECK_EQ( b.ask( "STAT:QUES?" ), std::to_string( 4 ) );
    b.tell( "INIT", 20'000 );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "256" );
    // A sample the line could not carry: data lost.
    b.sampler().noteDropped( 1 );
    b.world().run( 20'000 );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), std::to_string( 256 + 512 ) );
    b.tell( "ABOR" );
    // A pace that fell back to the timer: TIME again, from the closed
    // capture's statistics once the sampler's counters restart.
    b.world().ina.alertPin = sim::Ina228Model::AlertPin::HeldUntilRead;
    b.tell( "INIT", 30'000 );
    b.world().ina.alertPin = sim::Ina228Model::AlertPin::Absent;
    b.world().run( 100'000 );
    b.tell( "ABOR" );
    CHECK_EQ( field( b.ask( "FETC:STAT?" ), "pace_fallback" ), "1" );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), std::to_string( 256 + 4 ) );
}

TEST( "health", "the transition filters choose which edges reach the event registers" ) {
    Bench b;
    CHECK_EQ( b.ask( "STAT:OPER:PTR?;:STAT:OPER:NTR?;:STAT:QUES:PTR?;:STAT:QUES:NTR?" ),
              "32767;0;32767;0" );
    // Falls only: a capture's end sets MEASURING's event, its start does not.
    b.tell( "STAT:OPER?;:STAT:OPER:PTR 0;:STAT:OPER:NTR 16" );
    b.tell( "SENS:PROF STAT;:INIT", 20'000 );
    CHECK_EQ( b.ask( "STAT:OPER?" ), "0" );
    b.tell( "ABOR", 20'000 );
    CHECK_EQ( b.ask( "STAT:OPER?" ), "16" );
    b.tell( "STAT:QUES:PTR 0;:STAT:QUES:NTR 256" );
    CHECK_EQ( b.ask( "STAT:QUES:PTR?;:STAT:QUES:NTR?" ), "0;256" );
    b.tell( "STAT:QUES:PTR 40000" );
    CHECK_CONTAINS( b.errors(), "-222," );
    b.tell( "STAT:OPER:NTR x" );
    CHECK_CONTAINS( b.errors(), "-1" );
    b.tell( "STAT:QUES:NTR 70000;:STAT:OPER:PTR 70000" );
    CHECK_EQ( b.ask( "SYST:ERR:COUN?" ), "2" );
    b.tell( "*CLS;:STAT:PRES" );
    CHECK_EQ( b.ask( "STAT:OPER:PTR?;:STAT:OPER:NTR?;:STAT:QUES:PTR?;:STAT:QUES:NTR?" ),
              "32767;0;32767;0" );
}

TEST( "health", "the hold releasing a null path by itself queues error 113" ) {
    Bench b( []( sim::World& w ) { w.circuit.dutNa = 10'000'000; } );
    b.ask( "ROUT:NULL ON;:ROUT:RES?", 400'000 );
    CHECK( b.clean() );
    b.world().run( 125'000'000 );
    const std::string errors = b.errors();
    CHECK_CONTAINS( errors,
                    "113,\"Null path released;the hold released the null path after 120 s" );
    CHECK_EQ( field( b.ask( "SYST:HEAL?" ), "hold_released" ), "1" );
    CHECK_EQ( field( b.ask( "ROUT:STAT?" ), "path" ), "shunt" );
}

TEST( "health", "CONFigure and MEASure of a bus function select STATE; READ and FETCh do not" ) {
    Bench b;
    CHECK_EQ( b.ask( "SENS:PROF?" ), "TRACE" );
    b.tell( "SAMP:COUN 20" );
    // MEASure configures: after a boot on TRACE it answers (it queued -230).
    const std::string volts = b.ask( "MEAS:VOLT?", 1'000'000 );
    CHECK_EQ( volts.substr( 0, 4 ), "3.89" );
    CHECK_EQ( b.ask( "SENS:PROF?" ), "STATE" );
    b.tell( "SENS:PROF SLE;:CONF:POW" );
    CHECK_EQ( b.ask( "SENS:PROF?" ), "SLEEP" );  // SLEEP converts the bus already
    b.tell( "SENS:PROF TRAC;:CONF:ENER" );
    CHECK_EQ( b.ask( "SENS:PROF?" ), "STATE" );
    b.tell( "SENS:PROF BURS;:CONF:CURR;:CONF:TEMP" );
    CHECK_EQ( b.ask( "SENS:PROF?" ), "BURST" );  // nothing of the bus asked for
    b.tell( "SYST:COMM:I2C:FREQ 1000000;:SENS:PROF TRAC" );
    b.tell( "READ:VOLT?", 1'000'000 );
    CHECK_CONTAINS( b.errors(), "converted no bus voltage" );
    CHECK_EQ( b.ask( "SENS:PROF?" ), "TRACE" );
}

TEST( "health", "*TST? names each failed read, each task's stack and a stored blob" ) {
    Bench b;
    for ( const int failing : { int{ reg::kRegManufacturerId }, int{ reg::kRegShuntCal } } ) {
        b.world().ina.failRegister = failing;
        b.world().ina.failCount    = 1;
        CHECK_EQ( b.ask( "*TST?", 500'000 ), "8" );
    }
    // The probe's conversion word does not write.
    b.world().ina.failRegister = reg::kRegAdcConfig;
    b.world().ina.failCount    = 1;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "32" );
    b.world().ina.failRegister = -1;
    for ( const char* task : { "main", "meter-sample" } ) {
        b.world().stackFreeByName[task] = 100;
        CHECK_EQ( b.ask( "*TST?", 500'000 ), "256" );
        b.world().stackFreeByName.erase( task );
    }
    // A whole record the store refuses (seven bytes: no layout of its
    // own), newer than anything it takes; a save after it is newer still.
    const std::array<std::uint8_t, 7> refused = { 0xAB, 0xAB, 0xAB, 0xAB, 0xAB, 0xAB, 0xAB };
    namespace log                             = kleidos::meter::flash_log;
    REQUIRE(
        log::mount( log::Log::Calibration, kleidos::meter::flash_port::kLogBaseAddress, nullptr )
        == log::Status::Ok );
    REQUIRE( log::save( log::Log::Calibration, refused.data(), refused.size() )
             == log::Status::Ok );
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "128" );
    b.tell( "SENS:CURR:SHUN 0.1" );
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "0" );
}

TEST( "health", "a period missed past its limit sets TIME once the capture closes" ) {
    // A pin that pulses three times a conversion paces SLEEP three times too
    // fast: the achieved period misses the nominal one by far.
    Bench b( []( sim::World& w ) { w.ina.alertPin = sim::Ina228Model::AlertPin::Pulses; } );
    b.tell( "SENS:PROF SLE;:INIT", 700'000 );
    b.world().ina.alertDivider = 3;
    b.world().retimeAlert();
    b.world().run( 1'000'000 );
    b.tell( "ABOR" );
    const std::string stats = b.ask( "FETC:STAT?" );
    CHECK_EQ( field( stats, "pace_unmet" ), "1" );
    CHECK_EQ( field( stats, "read_err" ), "0" );
    CHECK_EQ( field( stats, "pace_fallback" ), "0" );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), std::to_string( 256 + 4 ) );
    b.world().ina.alertDivider = 1;
    b.world().retimeAlert();
    b.tell( "INIT", 700'000 );
    CHECK_EQ( b.ask( "STAT:QUES:COND?" ), "256" );
    b.tell( "ABOR" );
}

TEST( "health", "*TST?: the probe's alert enable not written is a dead wire" ) {
    Bench b;
    // The first DIAG_ALRT transaction is MEMSTAT's read (16), the second the
    // probe's conversion-ready enable (32).
    b.world().ina.failRegister = reg::kRegDiagAlrt;
    b.world().ina.failCount    = 2;
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "48" );
}
