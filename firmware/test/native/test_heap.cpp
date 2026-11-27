// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file test_heap.cpp
 * @brief No heap from `app_main()`'s first line: the heap monitor's counting
 *        by phase, `DIAGnostic:HEAP?`, the census and its two queries, a
 *        boot and a session of every kind of work that allocate and free
 *        nothing, and the every-command session (FW-19): every header of the
 *        command table, after each of which both since-main counts read 0.
 *
 * The harness's heap_caps_malloc and heap_caps_free call ESP-IDF's two hooks
 * as the device's do, so the monitor counts what the firmware's own code
 * allocates. The fakes of the ESP-IDF drivers do not allocate as the real ones
 * do; instead each fake of an entry point that allocates on the device keeps a
 * ledger (harness/idf_ledger.h), which every Bench and Scope holds to the
 * calls the image's configuration makes allocate nothing (the flash log's
 * write and erase), with no allow-list (FW-17): the cases below
 * show what the boot, a clock change and a save ask of ESP-IDF, and that a planted
 * call outside the list fails its case. The device's own counts are the bench
 * suite's (tests/bench) and the census's.
 */
#include "bench.h"
#include "heap_monitor.h"
#include "idf_ledger.h"
#include "scope.h"
#include "unit.h"

#include <esp_heap_caps.h>
#include <esp_intr_alloc.h>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <set>
#include <span>
#include <string>
#include <string_view>
#include <vector>

using bench::Bench;
using bench::field;
using namespace kleidos::meter;

namespace {

/// A census ring of the tests' own, attached for the life of the object and
/// taken away after it, so that no test leaves the monitor pointing at a
/// ring that is gone.
template <std::size_t N>
struct AttachedCensus {
    std::array<heap::CensusSlot, N> slots{};
    AttachedCensus() { heap::attachCensus( slots ); }
    ~AttachedCensus() { heap::attachCensus( {} ); }
    AttachedCensus( const AttachedCensus& )            = delete;
    AttachedCensus& operator=( const AttachedCensus& ) = delete;
};

/// The distinct entry points of @p ledger from @p from on, sorted, joined
/// with spaces: what a case compares against the sites it expects.
std::string entriesOf( const std::vector<std::string_view>& ledger, std::size_t from = 0 ) {
    std::set<std::string_view> distinct;
    for ( std::size_t i = from; i < ledger.size(); ++i ) {
        distinct.insert( ledger[i] );
    }
    std::string text;
    for ( const std::string_view entry : distinct ) {
        text += ( text.empty() ? "" : " " ) + std::string( entry );
    }
    return text;
}

}  // namespace

TEST( "heap", "the 0.7.0 counts are the since-main ones, and none is maintenance" ) {
    Bench b( {}, false );
    heap::disarm();
    void* before = heap_caps_malloc( 16, MALLOC_CAP_8BIT );
    heap_caps_free( before );
    CHECK( !heap::counts().armed );
    CHECK( heap::counts().allocsAfterInit == 0 );

    // app_main()'s first line (FW-17): the steady state starts with it, and
    // what came before is pre-main alone.
    heap::arm();
    void* steady = heap_caps_malloc( 16, MALLOC_CAP_8BIT );
    void* second = heap_caps_malloc( 16, MALLOC_CAP_8BIT );
    heap_caps_free( second );
    heap_caps_free( steady );
    // A null pointer is no allocation: the hooks ignore it.
    esp_heap_trace_alloc_hook( nullptr, 16, 0 );
    esp_heap_trace_free_hook( nullptr );
    const heap::Counts counts = heap::counts();
    CHECK( counts.armed );
    CHECK( counts.allocsAfterInit == 2 && counts.allocsSinceMain == 2 );
    CHECK( counts.freesAfterInit == 2 && counts.freesSinceMain == 2 );
    CHECK( counts.preMainAllocs == 1 && counts.preMainFrees == 1 );
    // No operation is maintenance since FW-16: the two keys stay and read 0.
    CHECK( counts.maintenanceAllocs == 0 );
    CHECK( counts.maintenanceFrees == 0 );
    heap::disarm();
    CHECK( heap::counts().freesAfterInit == 0 );
}

TEST( "heap", "DIAGnostic:HEAP? reports the counts, armed from app_main()'s first line" ) {
    heap::disarm();
    Bench b;
    // The boot armed the monitor before anything else, and nothing it did
    // allocated or freed (FW-17: armed=0 for a quarter of a second until then).
    const std::string report = b.ask( "DIAG:HEAP?" );
    CHECK_EQ( field( report, "armed" ), "1" );
    CHECK_EQ( field( report, "allocs_since_main" ), "0" );
    CHECK_EQ( field( report, "frees_since_main" ), "0" );
    CHECK_EQ( field( report, "allocs_after_init" ), "0" );
    CHECK_EQ( field( report, "frees_after_init" ), "0" );
    CHECK_EQ( field( report, "maint_allocs" ), "0" );
    CHECK_EQ( field( report, "maint_frees" ), "0" );
    CHECK_EQ( field( report, "free_internal" ), std::to_string( 300 * 1024 ) );
    CHECK_EQ( field( report, "min_free_internal" ), std::to_string( 280 * 1024 ) );
    CHECK_EQ( field( report, "largest_internal" ), std::to_string( 200 * 1024 ) );
    CHECK_EQ( field( report, "free_spiram" ), std::to_string( 4 * 1024 * 1024 ) );
    b.tell( "DIAG:HEAP? 1" );  // it takes no parameter
    CHECK_CONTAINS( b.errors(), "-108" );
    // The key still says when the monitor is not armed, a state the device
    // never reports (app_main() arms it first) but the report can carry.
    heap::disarm();
    CHECK_EQ( field( b.ask( "DIAG:HEAP?" ), "armed" ), "0" );
    heap::arm();
}

TEST( "heap", "a boot and a session of every kind of work allocate nothing from the first line" ) {
    heap::disarm();
    Bench             b( []( sim::World& w ) {
        w.ringSamples   = 4096;
        w.circuit.dutNa = 5'000'000;
    } );
    const std::size_t bootAllocs = b.world().heapAllocs;
    const std::size_t bootLedger = b.world().idfLedger.size();

    // Every profile, captured and closed.
    b.tell( "SYST:COMM:I2C:FREQ 1000000" );
    for ( const char* profile : { "STATE", "SLEEP", "BURST", "TRACE" } ) {
        b.tell( std::string( "SENS:PROF " ) + profile );
        b.tell( "INIT", 1'500'000 );
        b.tell( "ABOR" );
        const std::string stats = b.ask( "FETC:STAT?" );
        CHECK_EQ( profile + std::string( ":" ) + ( stats.empty() ? b.errors() : "ok" ),
                  profile + std::string( ":ok" ) );
    }
    // A full ring and its dump.
    b.tell( "SENS:PROF TRACE;:INIT", 2'000'000 );
    const std::string dump = b.ask( "TRAC:DATA? RAW,0,64" );
    CHECK( dump.rfind( "#3469METER_DUMP count=64 ", 0 ) == 0 );
    CHECK_CONTAINS( dump, " period_us=280 fmt=raw20" );
    b.tell( "ABOR" );
    // A measurement, a null, a range switch, the relays.
    b.tell( "SENS:PROF STATE;:SAMP:COUN 20" );
    CHECK_EQ( b.ask( "MEAS:CURR?", 2'000'000 ), "0.00500625" );  // 5 mA and the offset
    b.tell( "SENS:CURR:RANG:IND 1;:SENS:CURR:RANG:IND 0" );
    b.tell( "ROUT:NULL ON", 500'000 );
    b.tell( "CAL:ZERO 1", 1'500'000 );
    b.tell( "ABOR" );
    b.tell( "ROUT:NULL OFF", 500'000 );
    // Five hundred queries, the self-test and a reset.
    for ( int i = 0; i < 500; ++i ) {
        (void)b.ask( "SYST:STAT?" );
    }
    CHECK_EQ( b.ask( "*TST?", 500'000 ), "0" );
    b.tell( "*RST", 100'000 );
    const std::string report = b.ask( "DIAG:HEAP?" );
    CHECK_EQ( field( report, "armed" ), "1" );
    CHECK_EQ( field( report, "allocs_after_init" ), "0" );
    CHECK_EQ( field( report, "frees_after_init" ), "0" );
    // The whole of it counted from app_main()'s first line, with no exemption.
    CHECK_EQ( field( report, "allocs_since_main" ), "0" );
    CHECK_EQ( field( report, "frees_since_main" ), "0" );
    // The firmware's own code never calls the allocator, boot included.
    CHECK( bootAllocs == 0 );
    CHECK( b.world().heapAllocs == 0 );
    CHECK( b.world().heapFrees == 0 );
    // What the session asked of ESP-IDF after the boot is the null's save,
    // a record of the flash log: a write and its read-back, on the calls the
    // image's configuration and the port make allocate nothing (FW-16, a
    // read into internal RAM only), and nothing else. The clock changes
    // rewrite the bus's timing registers and ask for nothing (FW-14).
    CHECK_EQ( entriesOf( b.world().idfLedger, bootLedger ), "esp_flash_read esp_flash_write" );
}

/// One message of the every-command session and how long it is let run.
struct SessionStep {
    const char* message;  ///< The program message, every unit from the root.
    sim::Micros runUs;    ///< How long the world runs after it.
};

/// The every-command session (FW-19): each header of the console's command
/// table at least once, in the order a session would give them, the saves
/// (the tag, the date, the VBUS side, the shunt on each range, a null), the
/// lead cycle, the charge reset and the STATus subsystem included. The
/// watchdog's forced reset is the last word, kLastWord, given apart.
/// tests/firmware/test_heap_session_covers_the_table.py fails when a header
/// of kCommands has no message in either.
constexpr SessionStep kEveryCommand[] = {
    // IEEE 488.2's common commands, the self-test among them.
    { "*IDN?", 50'000 },
    { "*CLS", 50'000 },
    { "*ESE 0", 50'000 },
    { "*ESE?", 50'000 },
    { "*ESR?", 50'000 },
    { "*SRE 0", 50'000 },
    { "*SRE?", 50'000 },
    { "*STB?", 50'000 },
    { "*OPC", 50'000 },
    { "*OPC?", 50'000 },
    { "*WAI", 50'000 },
    { "*TST?", 500'000 },
    // SYSTem, two clock changes among them.
    { "SYST:ERR?", 50'000 },
    { "SYST:ERR:COUN?", 50'000 },
    { "SYST:ERR:ALL?", 50'000 },
    { "SYST:VERS?", 50'000 },
    { "SYST:INF?", 50'000 },
    { "SYST:STAT?", 50'000 },
    { "SYST:HEAL?", 50'000 },
    { "SYST:LED?", 50'000 },
    // The pin map: the live one, a staging of every kind, cleared, a map the
    // INA228 is not on tried and put back, the live one kept, and the defaults.
    { "SYST:PINS?", 50'000 },
    { "SYST:PINS:STAG SDA,45", 50'000 },
    { "SYST:PINS:STAG REL1,8,HIGH", 50'000 },
    { "SYST:PINS:ADDR #H41", 50'000 },
    { "SYST:PINS:CLOC 100000", 50'000 },
    { "SYST:PINS:PEND?", 50'000 },
    { "SYST:PINS:CLE", 50'000 },
    { "SYST:PINS:STAG SDA,18", 50'000 },
    { "SYST:PINS:APPL", 2'000'000 },
    { "SYST:PINS:CLE", 50'000 },
    { "SYST:PINS:APPL", 2'000'000 },
    { "SYST:PINS:DEF", 2'000'000 },
    { "SYST:PINS:RES?", 50'000 },
    { "SYST:COMM:I2C:FREQ?", 50'000 },
    { "SYST:COMM:I2C:FREQ 100000", 50'000 },
    { "SYST:COMM:I2C:FREQ 400000", 50'000 },
    // The STATus subsystem, every register and filter.
    { "STAT:OPER?", 50'000 },
    { "STAT:OPER:COND?", 50'000 },
    { "STAT:OPER:ENAB 0", 50'000 },
    { "STAT:OPER:ENAB?", 50'000 },
    { "STAT:QUES?", 50'000 },
    { "STAT:QUES:COND?", 50'000 },
    { "STAT:QUES:ENAB 0", 50'000 },
    { "STAT:QUES:ENAB?", 50'000 },
    { "STAT:QUES:PTR 32767", 50'000 },
    { "STAT:QUES:PTR?", 50'000 },
    { "STAT:QUES:NTR 0", 50'000 },
    { "STAT:QUES:NTR?", 50'000 },
    { "STAT:OPER:PTR 32767", 50'000 },
    { "STAT:OPER:PTR?", 50'000 },
    { "STAT:OPER:NTR 0", 50'000 },
    { "STAT:OPER:NTR?", 50'000 },
    { "STAT:PRES", 50'000 },
    // SENSe: the profile, the ranges, the shunt on each range (two saves),
    // the functions, the charge and its reset, the capture settings.
    { "SENS:PROF STATE", 50'000 },
    { "SENS:PROF?", 50'000 },
    { "SENS:PROF:REP?", 50'000 },
    { "SENS:CURR:RANG 1.6", 50'000 },
    { "SENS:CURR:RANG?", 50'000 },
    { "SENS:CURR:RANG:IND 1", 50'000 },
    { "SENS:CURR:RANG:IND?", 50'000 },
    { "SENS:CURR:RANG:IND 0", 50'000 },
    { "SENS:CURR:RANG:REP?", 50'000 },
    { "SENS:CURR:SHUN 0.100116", 50'000 },
    { "SENS:CURR:SHUN?", 50'000 },
    { "SENS:CURR:SHUN:RANG1 0.100144", 50'000 },
    { "SENS:CURR:SHUN:RANG1?", 50'000 },
    { "SENS:CURR:APER?", 50'000 },
    { "SENS:VOLT:APER?", 50'000 },
    { "SENS:TEMP:APER?", 50'000 },
    { "SENS:CURR:APER 0.00028", 50'000 },
    { "SENS:AVER:COUN?", 50'000 },
    { "SENS:AVER:COUN 16", 50'000 },
    { "SENS:AVER ON", 50'000 },
    { "SENS:AVER OFF", 50'000 },
    { "SENS:AVER?", 50'000 },
    { R"(SENS:FUNC:ON "CURR","VOLT")", 50'000 },
    { "SENS:FUNC:ON?", 50'000 },
    { "SENS:FUNC:ON:DEF", 50'000 },
    { "SENS:CHAR?", 1'200'000 },
    { "SENS:CHAR:RES", 50'000 },
    { "SENS:CAPT DATA", 50'000 },
    { "SENS:CAPT?", 50'000 },
    { "SENS:CAPT:LIM OFF", 50'000 },
    { "SENS:CAPT:LIM?", 50'000 },
    { "SENS:CURR:THR 0.002", 50'000 },
    { "SENS:CURR:THR?", 50'000 },
    { "SENS:CURR:THR:HYST 0", 50'000 },
    { "SENS:CURR:THR:HYST?", 50'000 },
    { "SENS:CURR:THR:MARK ON", 50'000 },
    { "SENS:CURR:THR:MARK?", 50'000 },
    { "SENS:CURR:THR:MARK OFF;:SENS:CURR:THR OFF", 50'000 },
    // SAMPle, FORMat and TRIGger.
    { "SAMP:TIM?", 50'000 },
    { "SAMP:COUN 20", 50'000 },
    { "SAMP:COUN?", 50'000 },
    { "FORM CSV", 50'000 },
    { "FORM?", 50'000 },
    { "FORM TEXT", 50'000 },
    { "TRIG:SOUR IMM", 50'000 },
    { "TRIG:SOUR?", 50'000 },
    { "TRIG:LEV 0", 50'000 },
    { "TRIG:LEV?", 50'000 },
    { "TRIG:SLOP POS", 50'000 },
    { "TRIG:SLOP?", 50'000 },
    { "TRIG:PRET 0", 50'000 },
    { "TRIG:PRET?", 50'000 },
    // CONFigure, MEASure, READ and FETCh, every function.
    { "CONF:CURR", 50'000 },
    { "CONF?", 50'000 },
    { "CONF:VOLT", 50'000 },
    { "CONF:POW", 50'000 },
    { "CONF:ENER", 50'000 },
    { "CONF:TEMP", 50'000 },
    { "CONF:CURR", 50'000 },
    { "MEAS:CURR?", 2'000'000 },
    { "MEAS:VOLT?", 2'000'000 },
    { "MEAS:POW?", 2'000'000 },
    { "MEAS:ENER?", 2'000'000 },
    { "MEAS:TEMP?", 2'000'000 },
    { "CONF:CURR", 50'000 },
    { "READ?", 2'000'000 },
    { "READ:CURR?", 2'000'000 },
    { "READ:VOLT?", 2'000'000 },
    { "READ:POW?", 2'000'000 },
    { "READ:ENER?", 2'000'000 },
    { "READ:TEMP?", 2'000'000 },
    { "FETC?", 50'000 },
    { "FETC:CURR?", 50'000 },
    { "FETC:VOLT?", 50'000 },
    { "FETC:POW?", 50'000 },
    { "FETC:ENER?", 50'000 },
    { "FETC:CHAR?", 50'000 },
    { "FETC:TEMP?", 50'000 },
    // The captures: a stream with a mark, a forced one, and a ring with its
    // dump.
    { "SENS:PROF STATE;:INIT", 1'000'000 },
    { "FETC:HEAD?", 50'000 },
    { "TRAC:MARK sweep", 500'000 },
    { "ABOR", 100'000 },
    { "FETC:STAT?", 50'000 },
    { "FETC:STAT:BATC?", 50'000 },
    { "INIT:FORC", 500'000 },
    { "ABOR", 100'000 },
    { "SENS:PROF TRAC;:INIT", 2'000'000 },
    { "TRAC:POIN?", 50'000 },
    { "TRAC:POIN:MAX?", 50'000 },
    { "TRAC:DATA? RAW,0,64", 500'000 },
    { "ABOR;:SENS:PROF STATE", 100'000 },
    // CALibration: the tag, the date and the VBUS side (three saves), the
    // reference, a null (a save) and the shunt against the reference.
    { "CAL:DATA?", 50'000 },
    { "CAL:TAG?", 50'000 },
    { R"(CAL:TAG "KM-SESSION")", 50'000 },
    { "CAL:DATE 2026,11,22", 50'000 },
    { "CAL:DATE?", 50'000 },
    { "CAL:VBUS SOUR", 50'000 },
    { "CAL:VBUS?", 50'000 },
    { "CAL:SHUN:REF 100", 50'000 },
    { "CAL:SHUN:REF?", 50'000 },
    { "ROUT:NULL ON", 500'000 },
    { "CAL:ZERO 1", 1'500'000 },
    { "ABOR", 100'000 },
    { "CAL:ZERO:RES?", 50'000 },
    { "ROUT:NULL OFF", 500'000 },
    { "SAMP:COUN 200;:CAL:SHUN", 1'000'000 },
    { "CAL:SHUN:RES?", 50'000 },
    // DIAGnostic: the ALERT pin, a scan, register reads, the heap and the
    // watchdog's query.
    { "DIAG:ALER?", 50'000 },
    { "DIAG:I2C:SCAN?", 200'000 },
    { "DIAG:REG? #H3E", 50'000 },
    { "DIAG:REG? #H3F", 50'000 },
    { "DIAG:HEAP?", 50'000 },
    { "DIAG:WATC?", 50'000 },
    // ROUTe: every relay order, a lead cycle among them.
    { "ROUT:STAT?", 50'000 },
    { "ROUT:RES?", 50'000 },
    { "ROUT:BYP ON", 500'000 },
    { "ROUT:BYP OFF", 500'000 },
    { "ROUT:LEAD OPEN,FORC", 500'000 },
    { "ROUT:LEAD CLOS", 500'000 },
    { "ROUT:LEAD:CYCL 100,FORC", 600'000 },
    { "ROUT:REL3 ON", 50'000 },
    { "ROUT:REL4 OFF", 50'000 },
    // And a reset.
    { "*RST", 100'000 },
};

/// The session's last word, given apart because nothing answers after it:
/// the watchdog's forced reset (`DIAGnostic:WATChdog:TEST`), after which the
/// console stops checking in and the timer resets the chip.
constexpr SessionStep kLastWord[] = {
    { "DIAG:WATC:TEST", 7'000'000 },
};

TEST( "heap", "every command of the table allocates and frees nothing from the first line" ) {
    heap::disarm();
    Bench             b( []( sim::World& w ) {
        w.ringSamples   = 4096;
        w.circuit.dutNa = 5'000'000;
    } );
    const std::size_t bootLedger = b.world().idfLedger.size();
    for ( const SessionStep& step : kEveryCommand ) {
        b.tell( step.message, step.runUs );
        (void)b.drain();
        const std::string report = b.ask( "DIAG:HEAP?" );
        // The step that allocated, if one did, names itself.
        CHECK_EQ( std::string( step.message ) + ": " + field( report, "allocs_since_main" ) + ","
                      + field( report, "frees_since_main" ),
                  std::string( step.message ) + ": 0,0" );
    }
    CHECK( b.world().heapAllocs == 0 );
    CHECK( b.world().heapFrees == 0 );
    // What the session asked of ESP-IDF since the boot: the saves' writes to
    // the calibration log and their read-backs, made safe by the image's
    // configuration and the port (FW-16), and nothing else; the clock changes
    // rewrite the bus's timing registers.
    CHECK_EQ( entriesOf( b.world().idfLedger, bootLedger ), "esp_flash_read esp_flash_write" );

    // The watchdog's forced reset, the last word: the console stops checking
    // in and the timer resets the chip, which on the device begins a new boot
    // and its counts from zero; until then nothing allocated.
    b.world().mwdt.resetExpected = true;
    for ( const SessionStep& step : kLastWord ) {
        b.tell( step.message, step.runUs );
    }
    b.world().noteMwdt();
    CHECK( b.world().mwdt.resetAtUs >= 0 );
    CHECK( b.world().heapAllocs == 0 );
    CHECK( b.world().heapFrees == 0 );
    CHECK_EQ( entriesOf( b.world().idfLedger, bootLedger ), "esp_flash_read esp_flash_write" );
}

TEST( "heap", "the boot asks ESP-IDF for no allocating entry point" ) {
    Bench b;
    CHECK_EQ( b.unlistedIdfCalls(), "" );
    // The calibration is read from the flash log (FW-16): no nvs_flash_init()
    // or nvs_open(), and the reads only, into the log's buffer in internal
    // RAM, which the port makes allocate nothing (a made-safe call).
    CHECK_EQ( entriesOf( b.world().idfLedger ), "esp_flash_read" );
    // The tasks subscribe to the firmware's own watchdog (FW-11), which asks
    // ESP-IDF for nothing that allocates: no esp_task_wdt_add() above; the
    // console's UART0 is set on its registers (FW-12): no
    // uart_param_config() or uart_driver_install(); and so is the INA228's
    // bus (FW-14): no i2c_new_master_bus() or i2c_master_bus_add_device().
}

TEST( "heap", "a clock change asks ESP-IDF for nothing, a save for the log's write alone" ) {
    Bench       b;
    std::size_t from = b.world().idfLedger.size();
    // Three clock changes, as the bench's: the timing registers, nothing else
    // (FW-14; until then each removed and added the device handle, D8).
    for ( const char* hz : { "100000", "1000000", "400000" } ) {
        b.tell( std::string( "SYST:COMM:I2C:FREQ " ) + hz );
    }
    CHECK_EQ( entriesOf( b.world().idfLedger, from ), "" );
    CHECK_EQ( field( b.ask( "DIAG:HEAP?" ), "maint_allocs" ), "0" );
    from = b.world().idfLedger.size();
    b.tell( "CAL:TAG \"KM-LEDGER\"" );
    CHECK_EQ( entriesOf( b.world().idfLedger, from ), "esp_flash_read esp_flash_write" );
    CHECK_EQ( b.unlistedIdfCalls(), "" );
    CHECK_EQ( field( b.ask( "DIAG:HEAP?" ), "maint_allocs" ), "0" );
    // Enough saves to fill the first sector: the switch erases the other.
    from = b.world().idfLedger.size();
    for ( int i = 0; i < 45; ++i ) {
        b.tell( std::string( "CAL:TAG \"T" ) + std::to_string( i ) + "\"" );
    }
    CHECK_EQ( entriesOf( b.world().idfLedger, from ),
              "esp_flash_erase_region esp_flash_read esp_flash_write" );
    CHECK( b.world().flash.erases[1] == 1U );
    from = b.world().idfLedger.size();
    for ( const char* query : { "*IDN?", "SYST:STAT?", "CAL:DATA?", "DIAG:HEAP?" } ) {
        (void)b.ask( query );
    }
    CHECK_EQ( entriesOf( b.world().idfLedger, from ), "" );
}

TEST( "heap", "a planted allocating ESP-IDF call that is not made safe fails the case" ) {
    std::string              unlisted;
    esp_err_t                planted = ESP_OK;
    std::vector<std::string> failures;
    {
        // Every check made while `expected` lives is kept instead of failing
        // this case, so only the destructors' are made in here.
        unit::ExpectedFailures expected;
        {
            Bench         b;
            intr_handle_t handle = nullptr;
            planted              = esp_intr_alloc( 0, 0, nullptr, nullptr, &handle );
            (void)esp_intr_free( nullptr );
            unlisted = b.unlistedIdfCalls();
        }  // ~Bench() checks the ledger: the first failure
        {
            bench::Scope scope;
            (void)esp_intr_free( nullptr );
        }  // ~Scope() as well: the second
        failures = expected.failures();
    }
    CHECK( planted == ESP_ERR_NOT_SUPPORTED );
    CHECK_EQ( unlisted, "esp_intr_alloc, esp_intr_free" );
    CHECK( failures.size() == 2 );
    CHECK( failures.size() == 2
           && failures[0].find( "actual:   esp_intr_alloc, esp_intr_free" ) != std::string::npos
           && failures[1].find( "actual:   esp_intr_free" ) != std::string::npos );
}

TEST( "heap", "the ledger's lists: no allow-list, the made-safe calls are denied" ) {
    // FW-16 took the last allowed call off (NVS) and FW-17 the empty list
    // itself; the made-safe calls stay on the deny list, which only grows,
    // each once.
    for ( const std::string_view entry : sim::kMadeSafeIdf ) {
        CHECK( std::count( sim::kAllocatingIdf.begin(), sim::kAllocatingIdf.end(), entry ) == 1 );
        CHECK( std::count( sim::kMadeSafeIdf.begin(), sim::kMadeSafeIdf.end(), entry ) == 1 );
    }
    for ( const std::string_view entry : sim::kAllocatingIdf ) {
        CHECK( std::count( sim::kAllocatingIdf.begin(), sim::kAllocatingIdf.end(), entry ) == 1 );
    }
    CHECK( sim::unlistedIdfAllocations(
               { "esp_flash_write", "esp_intr_alloc", "nvs_open", "esp_flash_erase_region" } )
           == ( std::vector<std::string>{ "esp_intr_alloc", "nvs_open" } ) );
    CHECK_EQ( sim::unlistedIdfAllocationText( {} ), "" );
}

TEST( "heap", "the phase counts start at the first hook call and split at arm()" ) {
    Bench b( {}, false );
    heap::disarm();
    void* startup = heap_caps_malloc( 8, MALLOC_CAP_8BIT );
    heap_caps_free( startup );
    void* kept = heap_caps_malloc( 8, MALLOC_CAP_8BIT );
    heap::arm();
    void* boot = heap_caps_malloc( 8, MALLOC_CAP_8BIT );
    heap_caps_free( kept );
    void* block = heap_caps_malloc( 8, MALLOC_CAP_8BIT );
    heap_caps_free( block );
    heap::arm();  // a second call changes nothing
    heap_caps_free( boot );
    const heap::Counts counts = heap::counts();
    CHECK( counts.preMainAllocs == 2 );
    CHECK( counts.preMainFrees == 1 );
    CHECK( counts.allocsSinceMain == 2 );
    CHECK( counts.freesSinceMain == 3 );
    // The steady state is the same window (FW-17).
    CHECK( counts.allocsAfterInit == 2 );
    CHECK( counts.freesAfterInit == 3 );
    CHECK( counts.maintenanceAllocs == 0 );
    heap::disarm();
    CHECK( heap::counts().preMainAllocs == 0 );
    CHECK( heap::counts().freesSinceMain == 0 );
}

TEST( "heap", "DIAGnostic:HEAP? appends the counts by phase" ) {
    Bench b( {}, false );
    heap::disarm();
    void* startup = heap_caps_malloc( 8, MALLOC_CAP_8BIT );
    b.boot();  // app_main(), whose first line arms the monitor
    heap_caps_free( startup );
    const std::string report = b.ask( "DIAG:HEAP?" );
    CHECK_EQ( field( report, "pre_main_allocs" ), "1" );
    CHECK_EQ( field( report, "pre_main_frees" ), "0" );
    CHECK_EQ( field( report, "allocs_since_main" ), "0" );
    CHECK_EQ( field( report, "frees_since_main" ), "1" );
    // The keys of 0.7.0 keep their places, the new ones follow them.
    CHECK( report.find( "free_spiram=" ) < report.find( " pre_main_allocs=" ) );
    CHECK( report.find( " pre_main_allocs=" ) < report.find( " pre_main_frees=" ) );
    CHECK( report.find( " pre_main_frees=" ) < report.find( " allocs_since_main=" ) );
    CHECK( report.find( " allocs_since_main=" ) < report.find( " frees_since_main=" ) );
    heap::disarm();
}

TEST( "heap", "the plain image has no census: its queries are undefined headers" ) {
    heap::disarm();
    Bench b;
    CHECK( !heap::censusBuilt() );
    CHECK( heap::censusSummary().capacity == heap::kCensusEvents );
    heap::CensusEvent event;
    CHECK( !heap::censusEvent( 0, event ) );
    b.tell( "DIAG:HEAP:CENS?" );
    b.tell( "DIAG:HEAP:CENS:EVEN? 0" );
    CHECK_EQ( b.errors(),
              "-113,\"Undefined header;DIAG:HEAP:CENS?\","
              "-113,\"Undefined header;DIAG:HEAP:CENS:EVEN?\"" );
}

TEST( "heap", "the census keeps every event in order, by phase, and forgets the oldest" ) {
    Bench b( {}, false );
    heap::disarm();
    AttachedCensus<4> census;
    CHECK( heap::censusBuilt() );
    void* startup = heap_caps_malloc( 24, MALLOC_CAP_INTERNAL );
    heap::arm();
    heap_caps_free( startup );
    heap::CensusEvent event;
    CHECK( heap::censusEvent( 0, event ) );
    CHECK( event.index == 0 );
    CHECK( event.allocation );
    CHECK( event.beforeMain );
    CHECK( event.size == 24 );
    CHECK( event.caps == MALLOC_CAP_INTERNAL );
    CHECK( event.block != 0 );
    // No frames scripted (sim::World::backtrace): the walk ends at once.
    CHECK( event.callers[0] == 0 );
    const std::uintptr_t block = event.block;
    CHECK( heap::censusEvent( 1, event ) );
    CHECK( !event.allocation );
    CHECK( !event.beforeMain );
    CHECK( event.size == 0 );
    CHECK( event.block == block );
    // Not yet recorded.
    CHECK( !heap::censusEvent( 2, event ) );
    CHECK( heap::censusSummary().events == 2 );
    CHECK( heap::censusSummary().first == 0 );
    // Four more: the ring holds the last four of six.
    for ( int i = 0; i < 2; ++i ) {
        void* block2 = heap_caps_malloc( 4, MALLOC_CAP_8BIT );
        heap_caps_free( block2 );
    }
    const heap::CensusSummary summary = heap::censusSummary();
    CHECK( summary.capacity == 4 );
    CHECK( summary.events == 6 );
    CHECK( summary.first == 2 );
    CHECK( !heap::censusEvent( 1, event ) );
    CHECK( heap::censusEvent( 2, event ) );
    CHECK( event.index == 2 );
    CHECK( heap::censusEvent( 5, event ) );
    CHECK( !heap::censusEvent( 6, event ) );
    // A slot a hook is writing (its stamp cleared) is not read.
    census.slots[1].stamp.store( 0 );
    CHECK( !heap::censusEvent( 5, event ) );
    heap::disarm();
    CHECK( heap::censusSummary().events == 0 );
    CHECK( !heap::censusEvent( 2, event ) );
}

TEST( "heap", "the census keeps the call sites the walk climbs, and stops where it must" ) {
    Bench b( {}, false );
    heap::disarm();
    heap::arm();
    AttachedCensus<4> census;
    sim::World&       world = b.world();
    heap::CensusEvent event;
    // Three frames: return addresses with a window increment in their top
    // bits or none, each kept as its call instruction; the walk ends after
    // the last, and the rest of the slot stays 0.
    world.backtrace = { 0x80001006U, 0x40002009U, 0xC0003003U };
    void* block     = heap_caps_malloc( 8, MALLOC_CAP_8BIT );
    CHECK( heap::censusEvent( 0, event ) );
    CHECK( event.callers[0] == 0x40001003U );
    CHECK( event.callers[1] == 0x40002006U );
    CHECK( event.callers[2] == 0x40003000U );
    CHECK( event.callers[3] == 0 );
    // A frame the walk finds not sane ends it there: what came before stays.
    world.backtraceInvalidAt = 1;
    heap_caps_free( block );
    CHECK( heap::censusEvent( 1, event ) );
    CHECK( event.callers[0] == 0x40001003U );
    CHECK( event.callers[1] == 0 );
    // A deeper stack than the slot holds: the innermost kCensusFrames kept.
    world.backtraceInvalidAt = std::numeric_limits<std::size_t>::max();
    world.backtrace.clear();
    for ( std::uint32_t i = 0; i < heap::kCensusFrames + 4U; ++i ) {
        world.backtrace.push_back( 0x40010000U + 0x10U * i );
    }
    block = heap_caps_malloc( 8, MALLOC_CAP_8BIT );
    CHECK( heap::censusEvent( 2, event ) );
    CHECK( event.callers[0] == 0x40010000U - 3U );
    CHECK( event.callers[heap::kCensusFrames - 1U]
           == 0x40010000U + 0x10U * ( heap::kCensusFrames - 1U ) - 3U );
    world.backtrace.clear();
    heap_caps_free( block );
    heap::disarm();
}

TEST( "heap", "the census queries report the ring and each event, callers included" ) {
    Bench b( {}, false );
    heap::disarm();
    AttachedCensus<8> census;
    void*             startup = heap_caps_malloc( 40, MALLOC_CAP_8BIT | MALLOC_CAP_INTERNAL );
    b.boot();  // app_main(), whose first line arms the monitor
    heap_caps_free( startup );
    void* kept = heap_caps_malloc( 16, MALLOC_CAP_SPIRAM );
    heap_caps_free( kept );

    const std::string summary = b.ask( "DIAG:HEAP:CENS?" );
    CHECK_EQ( summary, "capacity=8 frames=16 events=4 first=0" );

    const std::string first = b.ask( "DIAGnostic:HEAP:CENSus:EVENt? 0" );
    CHECK_EQ( field( first, "n" ), "0" );
    CHECK_EQ( field( first, "op" ), "alloc" );
    CHECK_EQ( field( first, "phase" ), "pre" );
    CHECK_EQ( field( first, "size" ), "40" );
    CHECK_EQ( field( first, "caps" ), "0x00000804" );
    CHECK( field( first, "block" ).rfind( "0x", 0 ) == 0 );
    CHECK_EQ( field( first, "callers" ), "-" );
    const std::string freed = b.ask( "DIAG:HEAP:CENS:EVEN? 1" );
    CHECK_EQ( field( freed, "op" ), "free" );
    CHECK_EQ( field( freed, "phase" ), "main" );
    CHECK_EQ( field( freed, "size" ), "0" );

    // The device's return addresses, as a census build's hooks keep them:
    // printed innermost first up to the first empty frame, or all sixteen.
    census.slots[2].callers[0].store( 0x42001234U );
    census.slots[2].callers[1].store( 0x4037abcdU );
    CHECK_EQ( field( b.ask( "DIAG:HEAP:CENS:EVEN? 2" ), "callers" ), "0x42001234,0x4037abcd" );
    for ( std::size_t i = 0; i < heap::kCensusFrames; ++i ) {
        census.slots[3].callers[i].store( 0x42000000U + i );
    }
    CHECK_EQ( field( b.ask( "DIAG:HEAP:CENS:EVEN? 3" ), "callers" ),
              "0x42000000,0x42000001,0x42000002,0x42000003,0x42000004,0x42000005,"
              "0x42000006,0x42000007,0x42000008,0x42000009,0x4200000a,0x4200000b,"
              "0x4200000c,0x4200000d,0x4200000e,0x4200000f" );
    CHECK( b.clean() );

    // An event not held, a missing index, a parameter the summary refuses.
    b.tell( "DIAG:HEAP:CENS:EVEN? 4" );
    CHECK_CONTAINS( b.errors(), "-222,\"Data out of range;census event 4 not held\"" );
    b.tell( "DIAG:HEAP:CENS:EVEN?" );
    CHECK_CONTAINS( b.errors(), "DIAGnostic:HEAP:CENSus:EVENt? <n>" );
    b.tell( "DIAG:HEAP:CENS? 1" );
    CHECK_CONTAINS( b.errors(), "-108" );
    // The census is tried after the command table: its commands still answer.
    CHECK_EQ( b.ask( "SYST:VERS?" ), "1999.0" );
    b.tell( "DIAG:HEAP:CENS:NOPE?" );
    CHECK_CONTAINS( b.errors(), "-113" );
    heap::disarm();
}
