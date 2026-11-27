// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file sim.cpp
 * @brief The scheduler, the board model and every fake ESP-IDF function the
 *        firmware links against on the host (sim.h).
 */
#include "sim.h"

#include "driver/gpio.h"
#include "esp_app_desc.h"
#include "esp_cpu.h"
#include "esp_debug_helpers.h"
#include "esp_err.h"
#include "esp_flash.h"
#include "esp_freertos_hooks.h"
#include "esp_heap_caps.h"
#include "esp_intr_alloc.h"
#include "esp_mac.h"
#include "esp_memory_utils.h"
#include "esp_private/periph_ctrl.h"
#include "esp_rom_serial_output.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"
#include "flash_port.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "hal/brownout_hal.h"
#include "hal/gpio_ll.h"
#include "hal/i2c_ll.h"
#include "hal/mwdt_ll.h"
#include "hal/rmt_ll.h"
#include "hal/timg_ll.h"
#include "hal/uart_ll.h"
#include "idf_ledger.h"
#include "ina228_regs.h"
#include "soc/gpio_sig_map.h"
#include "soc/io_mux_reg.h"

#include <esp_ipc.h>
#include <esp_system.h>

#include <algorithm>
#include <bit>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <thread>
#include <utility>

/// Posts an event on the driver's event queue (defined with the fake FreeRTOS

namespace sim {

namespace {

thread_local World*                        tlWorld      = nullptr;
thread_local std::unique_lock<std::mutex>* tlLock       = nullptr;
thread_local Micros                        tlSliceStart = 0;
/// The core the calling thread stands for: a task's pinned core, the IPC
/// target inside esp_ipc_call_blocking(), and 0 for the test's own thread,
/// which stands for the main task's boot on Core 0.
thread_local int tlCore = 0;
/// The task the calling thread is (xTaskGetCurrentTaskHandle()): the handle
/// a static create returned for it, null for a dynamic create's and for the
/// test's own thread.
thread_local TaskHandle_t tlTask = nullptr;

/// SIM_TRACE=1 prints the clock and every jump; SIM_TRACE=2 every wait too.
int traceLevel() {
    static const int level = [] {
        const char* value = std::getenv( "SIM_TRACE" );
        return value == nullptr ? 0 : std::atoi( value );
    }();
    return level;
}

namespace reg = kleidos::meter::ina228;

/// Register widths in bytes, by address.
std::size_t widthOf( std::uint8_t address ) noexcept {
    switch ( address ) {
        case reg::kRegVShunt:
        case reg::kRegVBus:
        case reg::kRegCurrent:
        case reg::kRegPower:
            return 3;
        case reg::kRegEnergy:
        case reg::kRegCharge:
            return 5;
        default:
            return 2;
    }
}

std::uint64_t encode20( std::int64_t counts ) noexcept {
    counts = std::clamp<std::int64_t>( counts, -524288, 524287 );
    return ( static_cast<std::uint64_t>( counts ) & 0xFFFFFU ) << 4;
}

std::int64_t roundDiv( std::int64_t numerator, std::int64_t denominator ) noexcept {
    if ( denominator == 0 ) {
        return 0;
    }
    const std::int64_t half = denominator / 2;
    return numerator >= 0 ? ( numerator + half ) / denominator : ( numerator - half ) / denominator;
}

/// The conversion period an ADC_CONFIG word sets, microseconds.
Micros conversionPeriodUs( std::uint16_t word ) noexcept {
    const unsigned mode  = ( word >> 12 ) & 0xFU;
    const unsigned vbus  = ( word >> 9 ) & 0x7U;
    const unsigned vsh   = ( word >> 6 ) & 0x7U;
    const unsigned vt    = ( word >> 3 ) & 0x7U;
    const unsigned avg   = word & 0x7U;
    Micros         total = 0;
    if ( mode & 0x1U ) {
        total += reg::kConversionTimeUs[vbus];
    }
    if ( mode & 0x2U ) {
        total += reg::kConversionTimeUs[vsh];
    }
    if ( mode & 0x4U ) {
        total += reg::kConversionTimeUs[vt];
    }
    total *= reg::kAverageCount[avg];
    return total > 0 ? total : 50;
}

}  // namespace

// --- Pads -------------------------------------------------------------------

int Pad::level() const noexcept {
    if ( forced >= 0 ) {
        return forced;
    }
    if ( driving() ) {
        return outSignal == kSigGpioOut ? latch : peripheralLevel;
    }
    return pullUp ? 1 : 0;
}

// --- The INA228 -------------------------------------------------------------

void Ina228Model::powerOnReset() noexcept {
    regs.fill( 0 );
    regs[reg::kRegConfig]         = 0x0000;
    regs[reg::kRegAdcConfig]      = adcConfigReset;
    regs[reg::kRegShuntCal]       = shuntCalReset;
    regs[reg::kRegShuntOvl]       = 0x7FFF;
    regs[reg::kRegShuntUvl]       = 0x8000;
    regs[reg::kRegBusOvl]         = 0x7FFF;
    regs[reg::kRegTempLimit]      = 0x7FFF;
    regs[reg::kRegPowerLimit]     = 0xFFFF;
    regs[reg::kRegManufacturerId] = manufacturerId;
    regs[reg::kRegDeviceId]       = deviceId;
    pointer                       = 0;
}

// --- The world --------------------------------------------------------------

World::World() {
    // A world built while another is current (a test that needs two boards)
    // stacks on it; leave() hands the thread back to the one below.
    previousWorld_ = tlWorld;
    previousLock_  = tlLock;
    tlWorld        = this;
    tlLock         = new std::unique_lock<std::mutex>( mutex_ );
    runnable_      = 1;
    ina.powerOnReset();
}

int currentCore() {
    return tlCore;
}

TaskHandle_t currentTask() {
    return tlTask;
}

void setCurrentCore( int core ) {
    tlCore = core;
}

World& World::current() {
    if ( tlWorld == nullptr ) {
        std::fprintf( stderr, "sim: no world on this thread\n" );
        std::abort();
    }
    return *tlWorld;
}

bool World::anyReady( const Waiter* except, bool countYielders ) const {
    for ( const Waiter* waiter : waiters_ ) {
        if ( waiter == except ) {
            continue;
        }
        if ( waiter->yielding ) {
            if ( countYielders ) {
                return true;
            }
            continue;
        }
        if ( now_ >= waiter->deadline || waiter->ready() ) {
            return true;
        }
    }
    return false;
}

bool World::olderYielder( const Waiter* self ) const {
    for ( const Waiter* waiter : waiters_ ) {
        if ( waiter != self && waiter->yielding && waiter->sequence < self->sequence ) {
            return true;
        }
    }
    return false;
}

void World::runIdleHooks() {
    // Core 0's idle task, between its tasks: once a simulated millisecond.
    if ( idleStarved || now_ < nextIdleUs_ ) {
        return;
    }
    nextIdleUs_ = now_ + 1000;
    for ( const IdleHook hook : idleHooks[0] ) {
        (void)hook();
    }
}

void World::fireDue() {
    for ( std::size_t i = 0; i < interrupts_.size(); ++i ) {
        while ( interrupts_[i].enabled && interrupts_[i].period > 0
                && interrupts_[i].next <= now_ ) {
            interrupts_[i].next += interrupts_[i].period;
            if ( interrupts_[i].handler ) {
                interrupts_[i].handler();
            }
        }
    }
}

void World::advance() {
    Micros next = kForever;
    for ( const Waiter* waiter : waiters_ ) {
        next = std::min( next, waiter->deadline );
    }
    for ( const Interrupt& source : interrupts_ ) {
        if ( source.enabled && source.period > 0 ) {
            next = std::min( next, source.next );
        }
    }
    if ( next == kForever ) {
        std::fprintf( stderr, "sim: deadlock at t=%lld us: every task waits for ever\n",
                      static_cast<long long>( now_ ) );
        std::abort();
    }
    if ( traceLevel() > 0 ) {
        std::fprintf( stderr, "sim: advance %lld -> %lld\n", static_cast<long long>( now_ ),
                      static_cast<long long>( next ) );
    }
    if ( next > now_ ) {
        now_ = next;
    }
    runIdleHooks();  // every task waits: Core 0's idle task runs
    fireDue();
}

[[noreturn]] static void freezeForever( std::condition_variable& cv ) {
    for ( ;; ) {
        cv.wait( *tlLock );
    }
}

void World::stallIfDue() {
    if ( taskStallsUs.empty() ) {
        return;
    }
    const auto named = taskNames.find( static_cast<const void*>( currentTask() ) );
    if ( named == taskNames.end() ) {
        return;
    }
    const auto stall = taskStallsUs.find( named->second );
    if ( stall == taskStallsUs.end() ) {
        return;
    }
    const Micros until = now_ + stall->second;
    taskStallsUs.erase( stall );
    (void)wait( [] { return false; }, until );
}

bool World::wait( const std::function<bool()>& ready, Micros deadline ) {
    stallIfDue();
    if ( ready() ) {
        return true;
    }
    if ( deadline <= now_ ) {
        return false;
    }
    Waiter self{ ready, deadline, false, 0 };
    waiters_.push_back( &self );
    --runnable_;
    if ( traceLevel() > 1 ) {
        std::fprintf( stderr, "sim: %p waits t=%lld until %lld runnable=%d\n",
                      static_cast<void*>( tlLock ), static_cast<long long>( now_ ),
                      static_cast<long long>( deadline ), runnable_ );
    }
    for ( ;; ) {
        if ( frozen_ ) {
            freezeForever( cv_ );
        }
        if ( self.ready() || now_ >= self.deadline ) {
            break;
        }
        if ( runnable_ == 0 ) {
            if ( !anyReady( nullptr, true ) ) {
                advance();
            }
            if ( self.ready() || now_ >= self.deadline ) {
                break;
            }
            cv_.notify_all();
        }
        cv_.wait( *tlLock );
    }
    waiters_.remove( &self );
    ++runnable_;
    tlSliceStart = now_;
    return self.ready();
}

Micros World::readClock() {
    now_ += clockReadUs;
    // Core 0's idle task runs while another core's thread spins, never while
    // a thread of Core 0 does.
    if ( currentCore() != 0 ) {
        runIdleHooks();
    }
    if ( traceLevel() > 0 && now_ % 100000 == 0 ) {
        std::fprintf( stderr, "sim: clock t=%lld runnable=%d waiters=%zu\n",
                      static_cast<long long>( now_ ), runnable_, waiters_.size() );
    }
    fireDue();
    // A thread spinning on the clock gives way at once to a thread whose
    // wait is over, and every kSliceUs to a thread that gave way before it
    // (a yielder), oldest first, so that two busy threads take turns instead
    // of waiting on each other.
    constexpr Micros kSliceUs     = 64;
    bool             yielderWaits = false;
    for ( const Waiter* waiter : waiters_ ) {
        yielderWaits = yielderWaits || waiter->yielding;
    }
    if ( anyReady( nullptr, false ) || ( yielderWaits && now_ - tlSliceStart >= kSliceUs ) ) {
        Waiter self{ [] { return true; }, now_, true, ++yields_ };
        waiters_.push_back( &self );
        --runnable_;
        cv_.notify_all();
        for ( ;; ) {
            if ( frozen_ ) {
                freezeForever( cv_ );
            }
            if ( runnable_ == 0 && !anyReady( &self, false ) && !olderYielder( &self ) ) {
                break;
            }
            cv_.wait( *tlLock );
        }
        waiters_.remove( &self );
        ++runnable_;
        tlSliceStart = now_;
    }
    return now_;
}

void World::run( Micros duration ) {
    (void)wait( [] { return false; }, now_ + duration );
}

bool World::runUntil( const std::function<bool()>& ready, Micros timeout ) {
    return wait( ready, now_ + timeout );
}

void World::notify() {
    cv_.notify_all();
}

void World::spawn( std::function<void()> body ) {
    ++runnable_;
    std::thread( [this, body = std::move( body )] {
        tlWorld = this;
        tlLock  = new std::unique_lock<std::mutex>( mutex_ );
        if ( frozen_ ) {
            freezeForever( cv_ );
        }
        body();
        --runnable_;
        cv_.notify_all();
        tlLock->unlock();
    } ).detach();
}

void World::startMainTask( std::function<void()> body ) {
    auto* const handle     = reinterpret_cast<TaskHandle_t>( &mainTask );
    taskNames[&mainTask]   = "main";
    taskPriorities["main"] = 1;
    spawn( [handle, body = std::move( body )] {
        setCurrentCore( 0 );
        tlTask = handle;
        body();
    } );
}

std::size_t World::addInterrupt( std::function<void()> handler ) {
    interrupts_.push_back( Interrupt{ std::move( handler ), false, 0, 0 } );
    return interrupts_.size() - 1;
}

void World::leave() {
    frozen_ = true;
    --runnable_;
    tlLock->unlock();
    delete tlLock;
    tlLock  = static_cast<std::unique_lock<std::mutex>*>( previousLock_ );
    tlWorld = previousWorld_;
}

// --- The ESP-IDF ledger (idf_ledger.h) ---------------------------------------

void noteIdfAllocation( std::string_view entry ) {
    if ( std::find( kAllocatingIdf.begin(), kAllocatingIdf.end(), entry )
         == kAllocatingIdf.end() ) {
        std::fprintf( stderr, "sim: %.*s is not on kAllocatingIdf\n",
                      static_cast<int>( entry.size() ), entry.data() );
        std::abort();
    }
    World::current().idfLedger.push_back( entry );
}

std::vector<std::string> unlistedIdfAllocations( const std::vector<std::string_view>& ledger ) {
    std::vector<std::string> unlisted;
    for ( const std::string_view entry : ledger ) {
        if ( std::find( kMadeSafeIdf.begin(), kMadeSafeIdf.end(), entry ) == kMadeSafeIdf.end() ) {
            unlisted.emplace_back( entry );
        }
    }
    return unlisted;
}

std::string unlistedIdfAllocationText( const std::vector<std::string_view>& ledger ) {
    std::string text;
    for ( const std::string& entry : unlistedIdfAllocations( ledger ) ) {
        text += ( text.empty() ? "" : ", " ) + entry;
    }
    return text;
}

/// Makes one object a fake hands the firmware for good (a bus, a device, a
/// queue or semaphore), as ESP-IDF allocates it on
/// the device. Never inlined, so that LeakSanitizer's one suppression of the
/// fakes (`lsan.supp`, `leak:sim::hold`) names every such object and no frame
/// of the firmware's own code.
template <class T, class... Args>
[[gnu::noinline]] T* hold( Args&&... args ) {
    return new T{ std::forward<Args>( args )... };
}

// --- The circuit ------------------------------------------------------------

namespace {

/// A pad's coil is energized when its module input sees the active level and
/// the module has power.
bool coilOn( const World& world, int gpio, bool activeHigh ) {
    if ( !world.circuit.relayPowered ) {
        return false;
    }
    const Pad& pad = world.pads[static_cast<std::size_t>( gpio )];
    if ( activeHigh ) {
        // The LCA717's LED takes current only from a pad driving high.
        return pad.driving() && pad.level() == 1;
    }
    // The HW-316's opto conducts only when the pad sinks: driven low.
    return pad.driving() && pad.level() == 0;
}

}  // namespace

bool World::leadOpen() const {
    return coilOn( *this, wires.lead, wires.leadActiveHigh ) && !circuit.leadStuckClosed;
}

bool World::bypassClosed() const {
    const bool activeHigh = circuit.bypassPart == BypassPart::Relay2Click;
    return coilOn( *this, wires.bypass, activeHigh ) && !circuit.bypassStuckOpen;
}

bool World::dutFed() const {
    return !leadOpen() || bypassClosed();
}

std::int64_t World::dutNow() const {
    return circuit.dutNaAt ? circuit.dutNaAt( now_ ) : circuit.dutNa;
}

std::int64_t World::shuntNa() const {
    if ( leadOpen() ) {
        return 0;
    }
    if ( bypassClosed() ) {
        return dutNow() * ( 100 - circuit.bypassSharePercent ) / 100;
    }
    return dutNow();
}

void World::retimeAlert() {
    for ( auto& [pin, index] : gpioIsr ) {
        Interrupt& source = interrupts_[index];
        source.period =
            conversionPeriodUs( static_cast<std::uint16_t>( ina.regs[reg::kRegAdcConfig] ) )
            / std::max( 1, ina.alertDivider );
    }
}

void World::updateCircuit() {
    const bool fed = dutFed();
    if ( circuit.dutFed && !fed ) {
        ++circuit.disconnects;
    }
    circuit.dutFed = fed;
}

// --- The line ---------------------------------------------------------------

void World::send( const std::string& text ) {
    sendRaw( text );
    sendRaw( "\n" );
}

void World::sendRaw( std::string_view bytes ) {
    for ( const char c : bytes ) {
        if ( uart.rxFifo.size() >= UART_LL_FIFO_DEF_LEN ) {
            ++uart.lostBytes;
            uart.latched |= UART_INTR_RXFIFO_OVF;
        } else {
            uart.rxFifo.push_back( c );
        }
        dispatchUart();
    }
    // The line idles after the last byte: the time-out, if the FIFO holds any.
    if ( uart.rxTimeoutBits > 0 && !uart.rxFifo.empty() ) {
        uart.latched |= UART_INTR_RXFIFO_TOUT;
    }
    dispatchUart();
    notify();
}

std::string World::takeOutput() {
    std::string out;
    out.swap( uart.tx );
    return out;
}

void World::drainTx() {
    std::size_t carried = 0;
    while ( carried < uart.txFifo.size() && !uart.txStalled ) {
        if ( uart.txBudget == 0 ) {
            uart.txStalled = true;
            break;
        }
        if ( uart.txBudget > 0 ) {
            --uart.txBudget;
        }
        uart.tx += uart.txFifo[carried++];
    }
    uart.txFifo.erase( 0, carried );
}

void World::stallTx() {
    uart.txStalled = true;
}

void World::resumeTx() {
    uart.txStalled = false;
    uart.txBudget  = -1;
    drainTx();
}

void World::resumeTxAt( Micros atUs ) {
    const std::size_t index = addInterrupt( {} );
    Interrupt&        once  = interrupt( index );
    once.handler            = [this, index] {
        interrupt( index ).enabled = false;
        resumeTx();
    };
    once.enabled = true;
    once.period  = kForever / 4;  // one firing, then disabled
    once.next    = atUs;
}

}  // namespace sim

// =============================================================================
// The fake ESP-IDF, on the current world.
// =============================================================================

using sim::World;
using namespace sim;  // the helpers above (reg, roundDiv, encode20, conversionPeriodUs)

namespace {

World& W() {
    return World::current();
}

sim::Micros ticksToDeadline( TickType_t ticks ) {
    if ( ticks == portMAX_DELAY ) {
        return sim::kForever;
    }
    return W().now() + static_cast<sim::Micros>( ticks ) * 1000;
}

}  // namespace

const char* esp_err_to_name( esp_err_t code ) {
    switch ( code ) {
        case ESP_OK:
            return "ESP_OK";
        case ESP_FAIL:
            return "ESP_FAIL";
        case ESP_ERR_NO_MEM:
            return "ESP_ERR_NO_MEM";
        case ESP_ERR_INVALID_ARG:
            return "ESP_ERR_INVALID_ARG";
        case ESP_ERR_INVALID_STATE:
            return "ESP_ERR_INVALID_STATE";
        case ESP_ERR_NOT_FOUND:
            return "ESP_ERR_NOT_FOUND";
        case ESP_ERR_NOT_SUPPORTED:
            return "ESP_ERR_NOT_SUPPORTED";
        case ESP_ERR_TIMEOUT:
            return "ESP_ERR_TIMEOUT";
        case ESP_ERR_INVALID_RESPONSE:
            return "ESP_ERR_INVALID_RESPONSE";
        default:
            return "ESP_ERR_UNKNOWN";
    }
}

std::int64_t esp_timer_get_time() {
    return W().readClock();
}

const esp_app_desc_t* esp_app_get_description() {
    static thread_local esp_app_desc_t description = {};
    if ( W().noAppDescription ) {
        return nullptr;
    }
    std::snprintf( description.version, sizeof( description.version ), "%s",
                   W().appVersion.c_str() );
    std::snprintf( description.project_name, sizeof( description.project_name ), "kleidos-meter" );
    std::memcpy( description.app_elf_sha256, W().elfSha256.data(), W().elfSha256.size() );
    return &description;
}

esp_err_t esp_efuse_mac_get_default( std::uint8_t* mac ) {
    World& world = W();
    if ( world.failMac ) {
        return ESP_FAIL;
    }
    std::memcpy( mac, world.mac.data(), world.mac.size() );
    return ESP_OK;
}

// ESP-IDF declares the two hooks weak and calls them from every allocation
// when CONFIG_HEAP_USE_HOOKS is set; the firmware's heap_monitor.cpp defines
// them, and a test binary without it links these.
extern "C" __attribute__( ( weak ) ) void esp_heap_trace_alloc_hook( void*, std::size_t,
                                                                     std::uint32_t ) {}
extern "C" __attribute__( ( weak ) ) void esp_heap_trace_free_hook( void* ) {}

void* heap_caps_malloc( std::size_t size, std::uint32_t caps ) {
    ++W().heapAllocs;
    void* block = std::malloc( size );
    esp_heap_trace_alloc_hook( block, size, caps );
    return block;
}

void heap_caps_free( void* block ) {
    ++W().heapFrees;
    esp_heap_trace_free_hook( block );
    std::free( block );
}

std::size_t heap_caps_get_free_size( std::uint32_t caps ) {
    World& world = W();
    return ( caps & MALLOC_CAP_SPIRAM ) != 0 ? world.heapFreeSpiram : world.heapFreeInternal;
}

std::size_t heap_caps_get_minimum_free_size( std::uint32_t ) {
    return W().heapMinInternal;
}

std::size_t heap_caps_get_largest_free_block( std::uint32_t ) {
    return W().heapLargest;
}

void esp_rom_delay_us( std::uint32_t us ) {
    World& world = W();
    world.run( static_cast<sim::Micros>( us ) );
}

void esp_rom_output_putc( char c ) {
    W().uart.tx += c;
}

// --- SPI flash (esp_flash.h) -------------------------------------------------
// The calibration record log's port (flash_port.cpp) on the world's model of
// the flash, as ESP-IDF 6.0.3 makes the three calls on the main flash: a
// write and an erase ask the region check first, the image's wrapped one,
// unless a test turns that off for a window at another address. The write
// and the erase are on the deny list and record their call; the ledger
// accepts them from the list of calls the image's configuration makes
// allocate nothing (idf_ledger.h kMadeSafeIdf).

esp_flash_t* esp_flash_default_chip = nullptr;

bool esp_ptr_in_dram( const void* p ) {
    const auto at = std::bit_cast<std::uintptr_t>( p );
    return std::none_of( W().externalRam.begin(), W().externalRam.end(), [at]( const auto& range ) {
        const auto first = std::bit_cast<std::uintptr_t>( range.first );
        return at >= first && at - first < range.second;
    } );
}

esp_err_t esp_flash_read( esp_flash_t*, void* buffer, std::uint32_t address,
                          std::uint32_t length ) {
    sim::noteIdfAllocation( "esp_flash_read" );
    World& world = W();
    if ( !esp_ptr_in_dram( buffer ) ) {
        ++world.flash.bounceBufferReads;  // ESP-IDF would read through the heap
    }
    return world.flash.read( address, static_cast<std::uint8_t*>( buffer ), length ) ? ESP_OK
                                                                                     : ESP_FAIL;
}

esp_err_t esp_flash_write( esp_flash_t*, const void* buffer, std::uint32_t address,
                           std::uint32_t length ) {
    sim::noteIdfAllocation( "esp_flash_write" );
    World& world = W();
    if ( world.flash.regionChecked
         && !__wrap_esp_partition_is_flash_region_writable( address, length ) ) {
        ++world.flash.refusedByRegion;
        return ESP_ERR_NOT_ALLOWED;
    }
    return world.flash.program( address, static_cast<const std::uint8_t*>( buffer ), length )
               ? ESP_OK
               : ESP_FAIL;
}

esp_err_t esp_flash_erase_region( esp_flash_t*, std::uint32_t start, std::uint32_t len ) {
    sim::noteIdfAllocation( "esp_flash_erase_region" );
    World& world = W();
    if ( world.flash.regionChecked
         && !__wrap_esp_partition_is_flash_region_writable( start, len ) ) {
        ++world.flash.refusedByRegion;
        return ESP_ERR_NOT_ALLOWED;
    }
    if ( len != sim::Flash::kSectorBytes ) {
        ++world.flash.unalignedErase;  // the log erases one sector at a time
        return ESP_ERR_INVALID_ARG;
    }
    return world.flash.eraseSector( start ) ? ESP_OK : ESP_FAIL;
}

// --- GPIO -------------------------------------------------------------------

esp_err_t gpio_config( const gpio_config_t* config ) {
    World& world = W();
    for ( int pin = 0; pin < GPIO_NUM_MAX; ++pin ) {
        if ( ( config->pin_bit_mask & ( 1ULL << pin ) ) == 0 ) {
            continue;
        }
        if ( world.faults.failGpioConfig
             && ( world.faults.failGpioConfigPin < 0 || world.faults.failGpioConfigPin == pin ) ) {
            return ESP_ERR_INVALID_ARG;
        }
        sim::Pad&  pad   = world.pads[static_cast<std::size_t>( pin )];
        const bool drove = pad.driving();
        pad.configured   = true;
        pad.output       = ( config->mode & GPIO_MODE_OUTPUT ) != 0;
        pad.input        = ( config->mode & GPIO_MODE_INPUT ) != 0;
        pad.pullUp       = config->pull_up_en == GPIO_PULLUP_ENABLE;
        pad.pullDown     = config->pull_down_en == GPIO_PULLDOWN_ENABLE;
        pad.openDrain    = false;  // no mode of the fake's is open drain, and gpio_config() says so
        if ( pad.output ) {
            // ESP-IDF 6's gpio_output_enable() routes the pad back to the
            // GPIO's own output (gpio_hal_matrix_out_default()).
            pad.outSignal = sim::kSigGpioOut;
        } else {
            // gpio_output_disable() gives the pad's output enable back to the
            // GPIO's own register, which it has just cleared: a peripheral
            // routed to it no longer drives it.
            pad.oneByPeripheral = false;
        }
        if ( pad.driving() && !drove ) {
            pad.firstDriven = pad.level();
            ++pad.enabledAt;
        }
    }
    world.updateRmtPads();
    world.updateCircuit();
    return ESP_OK;
}

esp_err_t gpio_set_level( gpio_num_t pin, std::uint32_t level ) {
    World& world = W();
    if ( pin < 0 || pin >= GPIO_NUM_MAX ) {
        return ESP_ERR_INVALID_ARG;
    }
    world.pads[static_cast<std::size_t>( pin )].latch = level != 0 ? 1 : 0;
    world.updateCircuit();
    return ESP_OK;
}

int gpio_get_level( gpio_num_t pin ) {
    if ( pin < 0 || pin >= GPIO_NUM_MAX ) {
        return 0;
    }
    return W().pads[static_cast<std::size_t>( pin )].level();
}

// --- The GPIO's interrupt registers (hal/gpio_ll.h) ---------------------------

gpio_dev_t GPIO = {};

void gpio_ll_set_intr_type( gpio_dev_t*, std::uint32_t gpio_num, gpio_int_type_t intr_type ) {
    World&    world                   = W();
    const int pin                     = static_cast<int>( gpio_num );
    world.gpioIntrType.at( gpio_num ) = static_cast<int>( intr_type );
    if ( intr_type == GPIO_INTR_DISABLE || pin != world.wires.alert
         || world.gpioIsr.count( pin ) != 0 ) {
        return;
    }
    // An edge type on the pin ALE is wired to: its edges reach the pin, one per
    // conversion (or per the model's pin behavior), while its interrupt is
    // enabled; each latches the pin's status bit and raises the GPIO's line.
    World* self        = &world;
    world.gpioIsr[pin] = world.addInterrupt( [self, pin] {
        sim::Ina228Model& ina = self->ina;
        if ( !ina.convert ) {
            return;
        }
        switch ( ina.alertPin ) {
            case sim::Ina228Model::AlertPin::Absent:
                return;
            case sim::Ina228Model::AlertPin::Pulses:
                self->raiseGpio( pin );
                return;
            case sim::Ina228Model::AlertPin::HeldUntilRead:
                break;
        }
        if ( ina.alertGlitches > 0 ) {
            // An edge on the wire and nothing behind it: no conversion ended.
            --ina.alertGlitches;
            self->raiseGpio( pin );
            return;
        }
        ina.cnvrfPending = true;
        if ( ina.alertLow ) {
            return;  // still low from the last conversion: no edge
        }
        ina.alertLow = true;
        self->raiseGpio( pin );
    } );
}

void gpio_ll_intr_enable_on_core( gpio_dev_t*, std::uint32_t, std::uint32_t gpio_num ) {
    World& world = W();
    world.gpioIntrEnable |= 1ULL << gpio_num;
    const auto found = world.gpioIsr.find( static_cast<int>( gpio_num ) );
    if ( found == world.gpioIsr.end() ) {
        return;
    }
    sim::Interrupt& source = world.interrupt( found->second );
    source.enabled         = true;
    const sim::Micros conversion =
        conversionPeriodUs( static_cast<std::uint16_t>( world.ina.regs[reg::kRegAdcConfig] ) );
    source.period = conversion / std::max( 1, world.ina.alertDivider );
    // The next edge on the part's own grid: enabling the interrupt starts no
    // conversion, so the first edge comes at the grid's phase, anywhere up to
    // a period after the enable.
    const sim::Micros since = world.now() - world.ina.convertingSince;
    source.next =
        source.period > 0 ? world.now() + ( source.period - since % source.period ) : world.now();
    // A conversion that ended while nobody listened pulled a released pin low
    // with no edge anyone saw (the firmware clears the latched status before
    // it enables, as ESP-IDF's driver did).
    sim::Ina228Model& ina = world.ina;
    if ( ina.alertPin == sim::Ina228Model::AlertPin::HeldUntilRead && ina.convert
         && world.now() - ina.releasedAt >= conversion ) {
        ina.alertLow     = true;
        ina.cnvrfPending = true;
    }
    world.dispatchGpio();
}

void gpio_ll_intr_disable( gpio_dev_t*, std::uint32_t gpio_num ) {
    World& world = W();
    world.gpioIntrEnable &= ~( 1ULL << gpio_num );
    const auto found = world.gpioIsr.find( static_cast<int>( gpio_num ) );
    if ( found != world.gpioIsr.end() ) {
        world.interrupt( found->second ).enabled = false;
    }
}

void gpio_ll_get_intr_status( gpio_dev_t*, std::uint32_t, std::uint32_t* status ) {
    const World& world = W();
    *status = world.gpioStatus & static_cast<std::uint32_t>( world.gpioIntrEnable & 0xFFFFFFFFULL );
}

void gpio_ll_clear_intr_status( gpio_dev_t*, std::uint32_t mask ) {
    W().gpioStatus &= ~mask;
}

// --- A pad's function, output and matrix signal (hal/gpio_ll.h) --------------

namespace {

/// Pad @p gpio_num of the calling world, after the bound check the device's
/// register arithmetic does not make.
sim::Pad& llPad( std::uint32_t gpio_num ) {
    return W().pads.at( gpio_num );
}

/// Records, after a change of @p pad's output path, what drives it now.
void noteDriver( std::uint32_t gpio_num ) {
    World&    world = W();
    sim::Pad& pad   = world.pads.at( gpio_num );
    const int now   = pad.driving() ? pad.outSignal : -1;
    if ( pad.drivers.empty() || pad.drivers.back().second != now ) {
        pad.drivers.emplace_back( world.now(), now );
    }
    world.updateRmtPads();
    world.updateCircuit();
}

}  // namespace

void gpio_ll_set_level( gpio_dev_t*, std::uint32_t gpio_num, std::uint32_t level ) {
    llPad( gpio_num ).latch = level != 0 ? 1 : 0;
    W().updateCircuit();
}

void gpio_ll_func_sel( gpio_dev_t*, std::uint8_t gpio_num, std::uint32_t func ) {
    llPad( gpio_num ).configured = func == PIN_FUNC_GPIO;
    llPad( gpio_num ).function   = static_cast<int>( func );
    noteDriver( gpio_num );
}

void gpio_ll_input_enable( gpio_dev_t*, std::uint32_t gpio_num ) {
    llPad( gpio_num ).input = true;
}

void gpio_ll_set_input_signal_from( gpio_dev_t*, std::uint32_t signal_idx, bool from_gpio_matrix ) {
    W().inputFromMatrix[signal_idx] = from_gpio_matrix;
}

void gpio_ll_set_input_signal_matrix_source( gpio_dev_t*, std::uint32_t signal_idx,
                                             std::uint32_t gpio_num, bool ) {
    World& world                      = W();
    world.inputSignalPad[signal_idx]  = gpio_num;
    world.inputFromMatrix[signal_idx] = true;
}

void gpio_ll_input_disable( gpio_dev_t*, std::uint32_t gpio_num ) {
    llPad( gpio_num ).input = false;
}

void gpio_ll_pullup_dis( gpio_dev_t*, std::uint32_t gpio_num ) {
    llPad( gpio_num ).pullUp = false;
}

void gpio_ll_pullup_en( gpio_dev_t*, std::uint32_t gpio_num ) {
    llPad( gpio_num ).pullUp = true;
}

void gpio_ll_pulldown_dis( gpio_dev_t*, std::uint32_t gpio_num ) {
    llPad( gpio_num ).pullDown = false;
}

void gpio_ll_od_disable( gpio_dev_t*, std::uint32_t gpio_num ) {
    llPad( gpio_num ).openDrain = false;
}

void gpio_ll_od_enable( gpio_dev_t*, std::uint32_t gpio_num ) {
    llPad( gpio_num ).openDrain = true;
}

void gpio_ll_output_enable( gpio_dev_t*, std::uint32_t gpio_num ) {
    llPad( gpio_num ).output = true;
    noteDriver( gpio_num );
}

void gpio_ll_output_disable( gpio_dev_t*, std::uint32_t gpio_num ) {
    llPad( gpio_num ).output = false;
    noteDriver( gpio_num );
}

void gpio_ll_set_output_enable_ctrl( gpio_dev_t*, std::uint8_t gpio_num, bool ctrl_by_periph,
                                     bool ) {
    llPad( gpio_num ).oneByPeripheral = ctrl_by_periph;
}

void gpio_ll_set_output_signal_matrix_source( gpio_dev_t*, std::uint32_t gpio_num,
                                              std::uint32_t signal_idx, bool out_inv ) {
    sim::Pad& pad = llPad( gpio_num );
    pad.outSignal = static_cast<int>( signal_idx );
    pad.inverted  = out_inv;
    noteDriver( gpio_num );
}

// --- The CPU's interrupt lines (esp_cpu.h) and the interrupt matrix ---------

namespace sim {

std::array<Cpu, 2> esp32s3Cpus() {
    // {priority, type, flags on Core 0, flags on Core 1}: type 0 level, 1
    // edge, 2 configurable; flags 1 special, 2 reserved.
    struct Row {
        int           priority;
        int           type;
        std::uint32_t flags[2];
    };
    static constexpr Row kTable[32] = {
        { 1, 0, { 0, 2 } }, { 1, 0, { 0, 0 } }, { 1, 0, { 0, 0 } }, { 1, 0, { 0, 0 } },
        { 1, 0, { 2, 0 } }, { 1, 0, { 0, 0 } }, { 1, 2, { 1, 1 } }, { 1, 2, { 1, 1 } },
        { 1, 0, { 0, 0 } }, { 1, 0, { 0, 0 } }, { 1, 1, { 0, 0 } }, { 3, 2, { 1, 1 } },
        { 1, 0, { 0, 0 } }, { 1, 0, { 0, 0 } }, { 7, 0, { 0, 0 } }, { 3, 2, { 1, 1 } },
        { 5, 2, { 1, 1 } }, { 1, 0, { 0, 0 } }, { 1, 0, { 0, 0 } }, { 2, 0, { 0, 0 } },
        { 2, 0, { 0, 0 } }, { 2, 0, { 0, 0 } }, { 3, 1, { 0, 0 } }, { 3, 0, { 0, 0 } },
        { 4, 0, { 2, 2 } }, { 4, 0, { 2, 2 } }, { 5, 0, { 0, 0 } }, { 3, 0, { 0, 0 } },
        { 4, 1, { 2, 2 } }, { 3, 2, { 1, 1 } }, { 4, 1, { 0, 0 } }, { 5, 0, { 0, 0 } },
    };
    std::array<Cpu, 2> cpus{};
    for ( std::size_t core = 0; core < cpus.size(); ++core ) {
        for ( std::size_t line = 0; line < 32; ++line ) {
            CpuLine& entry = cpus[core].lines[line];
            entry.priority = kTable[line].priority;
            entry.type     = kTable[line].type;
            entry.flags    = kTable[line].flags[core];
        }
    }
    return cpus;
}

namespace {

/// The handler ESP-IDF's allocator leaves on a line it gave a driver: the
/// harness's drivers take no interrupt of their own, so it does nothing.
void driverInterrupt( void* ) {}

}  // namespace

int World::allocatorTakesLine( int core ) {
    Cpu& cpu = cpus.at( static_cast<std::size_t>( core ) );
    for ( std::size_t line = 0; line < cpu.lines.size(); ++line ) {
        CpuLine& entry = cpu.lines[line];
        if ( entry.priority == 1 && entry.type == 0 && entry.flags == 0
             && entry.handler == nullptr ) {
            entry.handler = &driverInterrupt;
            cpu.enabled |= 1U << line;
            return static_cast<int>( line );
        }
    }
    return -1;
}

void World::raiseGpio( int pin ) {
    if ( pin < 32 && gpioIntrType.at( static_cast<std::size_t>( pin ) ) != GPIO_INTR_DISABLE ) {
        gpioStatus |= 1U << static_cast<std::uint32_t>( pin );
    }
    dispatchGpio();
}

void World::dispatchGpio() {
    const auto pending = [this] {
        return ( gpioStatus & static_cast<std::uint32_t>( gpioIntrEnable & 0xFFFFFFFFULL ) ) != 0;
    };
    for ( int core = 0; core < static_cast<int>( cpus.size() ); ++core ) {
        Cpu&       cpu   = cpus[static_cast<std::size_t>( core )];
        const auto route = cpu.routes.find( kGpioSource );
        if ( route == cpu.routes.end() || ( cpu.enabled & ( 1U << route->second ) ) == 0 ) {
            continue;
        }
        const CpuLine& line = cpu.lines[static_cast<std::size_t>( route->second )];
        if ( line.handler == nullptr ) {
            continue;
        }
        // A level line: its handler runs while the source is pending, on the
        // core the line belongs to.
        const int caller = currentCore();
        setCurrentCore( core );
        int runs = 0;
        while ( pending() && runs < kStormBound ) {
            line.handler( line.arg );
            ++runs;
        }
        setCurrentCore( caller );
        if ( pending() ) {
            ++gpioStorms;
        }
    }
}

}  // namespace sim

void esp_backtrace_get_start( std::uint32_t* pc, std::uint32_t* sp, std::uint32_t* next_pc ) {
    World& world      = W();
    world.backtraceAt = 0;
    *pc               = 0x40378000U;  // note() itself: an IRAM address
    *sp               = 0x3FC90000U;
    *next_pc          = world.backtrace.empty() ? 0U : world.backtrace.front();
}

bool esp_backtrace_get_next_frame( esp_backtrace_frame_t* frame ) {
    World&            world = W();
    const std::size_t at    = world.backtraceAt++;
    if ( at == world.backtraceInvalidAt ) {
        return false;
    }
    frame->pc      = frame->next_pc;
    frame->sp      = frame->sp + 32U;
    frame->next_pc = ( at + 1U < world.backtrace.size() ) ? world.backtrace[at + 1U] : 0U;
    return true;
}

int esp_cpu_get_core_id() {
    return sim::currentCore();
}

void esp_cpu_intr_get_desc( int core_id, int intr_num, esp_cpu_intr_desc_t* intr_desc_ret ) {
    const sim::CpuLine& line = W().cpus.at( static_cast<std::size_t>( core_id ) )
                                   .lines.at( static_cast<std::size_t>( intr_num ) );
    intr_desc_ret->priority  = line.priority;
    intr_desc_ret->type      = static_cast<esp_cpu_intr_type_t>( line.type );
    intr_desc_ret->flags     = line.flags;
}

bool esp_cpu_intr_has_handler( int intr_num ) {
    return W().cpus.at( static_cast<std::size_t>( sim::currentCore() ) )
               .lines.at( static_cast<std::size_t>( intr_num ) )
               .handler
           != nullptr;
}

void esp_cpu_intr_set_handler( int intr_num, esp_cpu_intr_handler_t handler, void* handler_arg ) {
    sim::CpuLine& line = W().cpus.at( static_cast<std::size_t>( sim::currentCore() ) )
                             .lines.at( static_cast<std::size_t>( intr_num ) );
    line.handler       = handler;
    line.arg           = handler_arg;
}

void esp_cpu_intr_enable( std::uint32_t intr_mask ) {
    World& world = W();
    world.cpus.at( static_cast<std::size_t>( sim::currentCore() ) ).enabled |= intr_mask;
    world.dispatchGpio();
}

void esp_cpu_intr_disable( std::uint32_t intr_mask ) {
    W().cpus.at( static_cast<std::size_t>( sim::currentCore() ) ).enabled &= ~intr_mask;
}

void esp_rom_route_intr_matrix( int cpu_core, std::uint32_t periph_intr_id,
                                std::uint32_t cpu_intr_num ) {
    W().cpus.at( static_cast<std::size_t>( cpu_core ) ).routes[periph_intr_id] =
        static_cast<int>( cpu_intr_num );
}

UBaseType_t xPortSetInterruptMaskFromISR() {
    World& world = W();
    ++world.interruptMaskDepth;
    return static_cast<UBaseType_t>( world.interruptMaskDepth - 1 );
}

void vPortClearInterruptMaskFromISR( UBaseType_t previous ) {
    World& world             = W();
    world.interruptMaskDepth = static_cast<int>( previous );
    // An interrupt raised while masked is taken as the mask goes.
    if ( world.interruptMaskDepth == 0 && world.uart.dispatchDeferred ) {
        world.dispatchUart();
    }
}

// --- The INA228 on the bus ---------------------------------------------------

namespace {

/// One transaction's failure, if one is due, for the register it names
/// (Ina228Model); and its stall, if one is set, which holds the caller.
bool transactionFails( int address ) {
    sim::Ina228Model& ina = W().ina;
    if ( ina.stallNextUs > 0 ) {
        const sim::Micros stall = ina.stallNextUs;
        ina.stallNextUs         = 0;
        World& world            = W();
        (void)world.wait( [] { return false; }, world.now() + stall );
    }
    if ( ina.failAfter == 0 ) {
        return true;
    }
    if ( ina.failEvery > 0 && ++ina.transactions % ina.failEvery == 0 ) {
        return true;
    }
    if ( ina.failAfter > 0 ) {
        --ina.failAfter;
    }
    if ( ina.failCount > 0 && ( ina.failRegister < 0 || ina.failRegister == address ) ) {
        --ina.failCount;
        return true;
    }
    return false;
}

/// A read of the register stallReadRegister names holds its caller, once.
void stallRead( int address ) {
    sim::Ina228Model& ina = W().ina;
    if ( ina.stallReadUs > 0 && ina.stallReadRegister == address ) {
        const sim::Micros stall = ina.stallReadUs;
        ina.stallReadUs         = 0;
        World& world            = W();
        (void)world.wait( [] { return false; }, world.now() + stall );
    }
}

std::int64_t shuntCounts( World& world ) {
    sim::Ina228Model&  ina   = world.ina;
    const bool         wide  = ( ina.regs[reg::kRegConfig] & reg::kConfigAdcRange ) == 0;
    const std::int64_t lsbPv = wide ? reg::kShuntLsbPvRange0 : reg::kShuntLsbPvRange1;
    // nA x uOhm = 1e-15 V = 1e-3 pV.
    const std::int64_t pv     = world.shuntNa() * ina.trueShuntMicroOhm / 1000 + ina.offsetPv;
    std::int64_t       counts = roundDiv( pv, lsbPv );
    if ( ina.noiseCounts > 0 ) {
        ina.lcg     = ina.lcg * 1103515245U + 12345U;
        const int n = static_cast<int>( ( ina.lcg >> 16 )
                                        % static_cast<unsigned>( 2 * ina.noiseCounts + 1 ) );
        counts += n - ina.noiseCounts;
    }
    return counts;
}

std::uint64_t readRegister( World& world, std::uint8_t address ) {
    sim::Ina228Model& ina = world.ina;
    switch ( address ) {
        case reg::kRegVShunt:
        case reg::kRegCurrent:
            return encode20( shuntCounts( world ) );
        case reg::kRegVBus:
            return encode20( roundDiv( ina.busUv * 1'000'000, reg::kBusLsbPv ) );
        case reg::kRegDieTemp:
            return static_cast<std::uint16_t>( static_cast<std::int16_t>( ina.dieMc * 16 / 125 ) );
        case reg::kRegCharge: {
            if ( ina.chargeCounts >= 0 ) {
                const std::int64_t value = ina.chargeCounts;
                ina.chargeCounts += ina.chargeJump;
                return static_cast<std::uint64_t>( value ) & 0xFFFFFFFFFFULL;
            }
            const bool         wide  = ( ina.regs[reg::kRegConfig] & reg::kConfigAdcRange ) == 0;
            const std::int64_t lsbPc = wide ? reg::kChargeLsbPcRange0 : reg::kChargeLsbPcRange1;
            // nA x us = fC; / 1000 = pC.
            const std::int64_t pc = world.shuntNa() * ( world.now() - ina.chargeSince ) / 1000;
            return static_cast<std::uint64_t>( pc / lsbPc ) & 0xFFFFFFFFFFULL;
        }
        case reg::kRegDiagAlrt: {
            ++ina.diagReads;
            if ( std::this_thread::get_id() != ina.creator ) {
                ++ina.diagReadsFromTasks;
            }
            std::uint64_t value = ( ina.regs[address] & 0xFC00U ) | ina.diagFlags;
            if ( ina.memStat ) {
                value |= reg::kDiagMemStat;
            }
            bool ready = ina.convert;
            if ( ina.alertPin == sim::Ina228Model::AlertPin::HeldUntilRead ) {
                // CNVRF: a conversion ended since the last read. While the
                // ALERT interrupt listens, its ticks are the conversions; while
                // it does not, the clock says whether a period has passed. The
                // read clears CNVRF and releases the pin.
                bool listening = false;
                for ( const auto& [pin, index] : world.gpioIsr ) {
                    listening = listening || world.interrupt( index ).enabled;
                }
                const sim::Micros conversion = conversionPeriodUs(
                    static_cast<std::uint16_t>( ina.regs[reg::kRegAdcConfig] ) );
                const bool elapsed =
                    !listening && world.now() - ina.releasedAt >= conversion + ina.cnvrfDelayUs;
                ready            = ina.convert && ( ina.cnvrfPending || elapsed );
                ina.cnvrfPending = false;
                // The read releases the pin by clearing CNVRF: one an
                // ADC_CONFIG write left low with the flag clear stays low.
                ina.alertLow   = ina.alertLow && !ready;
                ina.releasedAt = world.now();
            }
            if ( ready ) {
                value |= reg::kDiagCnvrf;
            }
            return value;
        }
        default:
            return ina.regs[address];
    }
}

void writeRegister( World& world, std::uint8_t address, std::uint16_t value ) {
    sim::Ina228Model& ina = world.ina;
    if ( address == reg::kRegConfig ) {
        if ( ( value & reg::kConfigRst ) != 0 ) {
            ina.powerOnReset();
            ina.chargeSince = world.now();
            return;
        }
        if ( ( value & reg::kConfigRstAcc ) != 0 ) {
            ina.chargeSince = world.now();
        }
        value = static_cast<std::uint16_t>( value & ~( reg::kConfigRst | reg::kConfigRstAcc ) );
    }
    if ( address < ina.regs.size() ) {
        ina.regs[address] = value;
    }
    if ( address == reg::kRegAdcConfig ) {
        // Writing ADC_CONFIG clears CNVRF ([DS] SS 7.3.4) and with it the pin,
        // unless a conversion ended inside the write: then the pin went low
        // and the write cleared the flag behind it (Ina228Model).
        const bool meetsConversion = ina.conversionEndsInAdcConfigWrites > 0 && ina.convert
                                     && ina.alertPin == sim::Ina228Model::AlertPin::HeldUntilRead;
        if ( meetsConversion ) {
            --ina.conversionEndsInAdcConfigWrites;
        }
        ina.cnvrfPending    = false;
        ina.alertLow        = meetsConversion;
        ina.releasedAt      = world.now();
        ina.convertingSince = world.now();
        for ( auto& [pin, index] : world.gpioIsr ) {
            sim::Interrupt& source = world.interrupt( index );
            source.period          = conversionPeriodUs( value ) / std::max( 1, ina.alertDivider );
            source.next            = world.now() + source.period;
        }
    }
}

void putBytes( std::uint64_t value, std::size_t width, std::uint8_t* out, std::size_t length ) {
    for ( std::size_t i = 0; i < length; ++i ) {
        out[i] = i < width ? static_cast<std::uint8_t>( value >> ( 8 * ( width - 1 - i ) ) )
                           : std::uint8_t{ 0 };
    }
}

}  // namespace

// --- I2C0 (hal/i2c_ll.h: the own I2C master, FW-13) --------------------------

i2c_dev_t I2C0 = {};

namespace {

/// The crystal I2C0 counts (I2C_CLK_SRC_XTAL).
constexpr std::uint32_t kI2cSourceHz = 40000000;

/// I2C0 of the calling world, after counting a call made from a core other
/// than the one that turned its clock on (I2c::offCoreCalls).
sim::I2c& i2cRegisters() {
    World& world = W();
    if ( world.i2c.core >= 0 && sim::currentCore() != world.i2c.core ) {
        ++world.i2c.offCoreCalls;
    }
    return world.i2c;
}

/// The same, for a register write: one made with the bus clock off is lost
/// on the device, and counted (I2c::writesWhileGated).
sim::I2c& i2cWrite() {
    sim::I2c& bus = i2cRegisters();
    if ( !bus.busClock ) {
        ++bus.writesWhileGated;
    }
    return bus;
}

/// The fields of one command register (i2c_ll_hw_cmd_t's bits).
struct I2cCommand {
    std::uint32_t bytes  = 0;
    bool          ackEn  = false;
    bool          ackVal = false;
    int           op     = 0;
};

/// The register word of @p command: its fields at the hardware's bits, each
/// cut to its width as the register cuts it.
std::uint32_t encodeCommand( const i2c_ll_hw_cmd_t& command ) {
    return ( command.byte_num & 0xFFU ) | ( ( command.ack_en & 1U ) << 8U )
           | ( ( command.ack_exp & 1U ) << 9U ) | ( ( command.ack_val & 1U ) << 10U )
           | ( ( command.op_code & 7U ) << 11U );
}

/// The fields of the register word @p word.
I2cCommand decodeCommand( std::uint32_t word ) {
    I2cCommand out;
    out.bytes  = word & 0xFFU;
    out.ackEn  = ( ( word >> 8U ) & 1U ) != 0U;
    out.ackVal = ( ( word >> 10U ) & 1U ) != 0U;
    out.op     = static_cast<int>( ( word >> 11U ) & 7U );
    return out;
}

/// Brings the transaction on the wire up to now: its NACK, its fault and its
/// STOP's completion latch in the raw status as the clock reaches them.
void refreshI2c( World& world ) {
    sim::I2c& bus = world.i2c;
    if ( !bus.active ) {
        return;
    }
    const sim::Micros now = world.now();
    if ( bus.nackAt >= 0 && now >= bus.nackAt ) {
        bus.raw |= I2C_LL_INTR_NACK;
    }
    if ( bus.faultAt >= 0 && now >= bus.faultAt ) {
        bus.raw |= I2C_LL_INTR_TIMEOUT;
        bus.fsmStuck = true;
        bus.active   = false;
        return;
    }
    if ( bus.completeAt >= 0 && now >= bus.completeAt ) {
        bus.raw |= I2C_LL_INTR_MST_COMPLETE;
        bus.active = false;
    }
}

/// The bit times @p bits take at the latched clock, rounded up, in
/// microseconds.
sim::Micros bitTimes( const sim::I2c& bus, std::int64_t bits ) {
    const std::int64_t hz = bus.sclHz();
    return ( bits * 1'000'000 + hz - 1 ) / hz;
}

/// Whether the controller is set as the bus's master the way a transaction
/// needs: clocked, out of reset, master, open drain, on the crystal, FIFO
/// mode, MSB first, with a latched clock.
bool i2cConfigured( const sim::I2c& bus ) {
    return bus.busClock && bus.controllerClock && bus.master && bus.openDrain
           && bus.sourceClock == I2C_CLK_SRC_XTAL && bus.fifoMode
           && bus.txOrder == I2C_DATA_MODE_MSB_FIRST && bus.rxOrder == I2C_DATA_MODE_MSB_FIRST
           && bus.sclHz() > 0;
}

/// Whether the INA228's two lines are the controller's: the pads they are
/// wired to carry its SDA and SCL, open drain, with the output enable the
/// peripheral's, and are what it listens to.
bool onTheWires( const World& world ) {
    const auto carries = [&world]( int gpio, std::uint32_t out, std::uint32_t in ) {
        const sim::Pad& pad    = world.pads.at( static_cast<std::size_t>( gpio ) );
        const auto      listen = world.inputSignalPad.find( in );
        return pad.configured && pad.input && pad.openDrain && pad.oneByPeripheral
               && pad.outSignal == static_cast<int>( out ) && listen != world.inputSignalPad.end()
               && listen->second == static_cast<std::uint32_t>( gpio );
    };
    return carries( world.wires.sda, I2CEXT0_SDA_OUT_IDX, I2CEXT0_SDA_IN_IDX )
           && carries( world.wires.scl, I2CEXT0_SCL_OUT_IDX, I2CEXT0_SCL_IN_IDX );
}

/// Runs the command list against the INA228 model and schedules its status.
void runI2c( World& world ) {
    sim::I2c&                 bus = world.i2c;
    sim::Ina228Model&         ina = world.ina;
    std::string               text;
    std::int64_t              bits          = 0;
    std::int64_t              nackBits      = -1;
    bool                      expectAddress = false;
    bool                      reading       = false;
    bool                      addressed     = false;
    bool                      ended         = false;
    bool                      stopped       = false;
    int                       dataBytes     = 0;
    std::size_t               readTotal     = 0;
    std::size_t               readAt        = 0;
    std::vector<std::uint8_t> written;
    std::vector<std::uint8_t> readBytes;
    const bool                nackOnAddress = bus.nackAddress > 0;
    const bool                nackOnData    = bus.nackData > 0;
    if ( nackOnAddress ) {
        --bus.nackAddress;
    }
    if ( nackOnData ) {
        --bus.nackData;
    }
    // The bytes the read commands take, all from the register the pointer
    // names, as the INA228 sends them.
    for ( std::uint32_t word : bus.commands ) {
        const I2cCommand command = decodeCommand( word );
        if ( command.op == I2C_LL_CMD_READ ) {
            readTotal += command.bytes;
        }
    }
    if ( decodeCommand( bus.commands[0] ).op != I2C_LL_CMD_RESTART ) {
        ++bus.protocolErrors;
    }
    const auto commit = [&] {
        // The end of a write phase: the pointer, and a register value.
        if ( !written.empty() ) {
            ina.pointer = written[0];
            if ( written.size() >= 3 ) {
                writeRegister( world, written[0],
                               static_cast<std::uint16_t>( ( written[1] << 8 ) | written[2] ) );
            }
            written.clear();
        }
    };
    for ( std::size_t i = 0; i < bus.commands.size() && !ended; ++i ) {
        const I2cCommand command = decodeCommand( bus.commands[i] );
        switch ( command.op ) {
            case I2C_LL_CMD_RESTART:
                commit();
                bits += 1;
                text += text.empty() ? "S" : " S";
                expectAddress = true;
                break;
            case I2C_LL_CMD_WRITE:
                if ( !command.ackEn || command.bytes == 0 ) {
                    ++bus.protocolErrors;
                }
                for ( std::uint32_t b = 0; b < command.bytes && !ended; ++b ) {
                    if ( bus.txFifo.empty() ) {
                        ++bus.protocolErrors;
                        ended = true;
                        break;
                    }
                    const std::uint8_t byte = bus.txFifo.front();
                    bus.txFifo.pop_front();
                    bits += 9;
                    char hex[8] = {};
                    std::snprintf( hex, sizeof( hex ), " %02X", static_cast<unsigned>( byte ) );
                    text += hex;
                    bool acked = true;
                    if ( expectAddress ) {
                        expectAddress     = false;
                        reading           = ( byte & 1U ) != 0U;
                        const auto target = static_cast<int>( byte >> 1U );
                        addressed =
                            ( ( ina.present && onTheWires( world ) && target == ina.address )
                              || target == ina.alsoAnswers )
                            && !nackOnAddress;
                        acked = addressed;
                    } else if ( !reading ) {
                        acked = !( nackOnData && dataBytes == 0 );
                        ++dataBytes;
                        written.push_back( byte );
                    } else {
                        ++bus.protocolErrors;  // a write after a read address
                    }
                    if ( !acked ) {
                        text += "!";
                        nackBits = bits;
                        written.clear();
                        ended = true;
                    }
                }
                break;
            case I2C_LL_CMD_READ: {
                if ( !reading || !addressed || command.bytes == 0 ) {
                    ++bus.protocolErrors;
                }
                const bool last = readAt + command.bytes == readTotal;
                if ( command.ackVal != last || ( last && command.bytes != 1 ) ) {
                    ++bus.protocolErrors;  // only the last byte goes unacknowledged
                }
                if ( readBytes.empty() && readTotal > 0 ) {
                    readBytes.resize( readTotal );
                    putBytes( readRegister( world, ina.pointer ), widthOf( ina.pointer ),
                              readBytes.data(), readBytes.size() );
                }
                for ( std::uint32_t b = 0; b < command.bytes; ++b ) {
                    if ( bus.rxFifo.size() >= I2C_LL_FIFO_LEN ) {
                        ++bus.protocolErrors;
                    } else if ( readAt < readBytes.size() ) {
                        bus.rxFifo.push_back( readBytes[readAt] );
                    }
                    ++readAt;
                }
                bits += 9 * static_cast<std::int64_t>( command.bytes );
                text += ( command.ackVal ? " n" : " r" ) + std::to_string( command.bytes );
                break;
            }
            case I2C_LL_CMD_STOP:
                commit();
                bits += 1;
                text += " P";
                stopped = true;
                ended   = true;
                break;
            default:
                ++bus.protocolErrors;
                ended = true;
                break;
        }
    }
    if ( nackBits >= 0 ) {
        // The controller sends the STOP after the NACK.
        text += " P";
        stopped = true;
    }
    if ( !stopped ) {
        ++bus.protocolErrors;  // a list with no STOP never completes
    }
    if ( !bus.txFifo.empty() && nackBits < 0 ) {
        ++bus.protocolErrors;  // bytes no command sent (a NACK leaves the rest unsent)
    }
    bus.wire.push_back( text );
    const sim::Micros start = world.now();
    bus.active              = true;
    bus.nackAt              = nackBits >= 0 ? start + bitTimes( bus, nackBits ) : -1;
    bus.completeAt = stopped ? start + bitTimes( bus, nackBits >= 0 ? nackBits + 1 : bits ) : -1;
    if ( nackBits >= 0 && bus.nackHolds ) {
        bus.completeAt = -1;  // the STOP never comes: busy until a reset
    }
    bus.faultAt = -1;
    if ( bus.timeouts > 0 ) {
        --bus.timeouts;
        // SCL held through the time-out, partway into the first byte.
        bus.faultAt =
            start + bitTimes( bus, 4 )
            + ( ( 1LL << bus.live.tout ) * 1'000'000 ) / static_cast<std::int64_t>( kI2cSourceHz );
        bus.nackAt     = -1;
        bus.completeAt = -1;
        bus.rxFifo.clear();
    }
}

/// Applies the INA228 model's own faults (Ina228Model) to the command list
/// just started: a probe (the address alone, then the STOP) is counted and
/// fails by none of them; any other transaction may stall its caller first,
/// and a failing one is armed as a NACK on its register pointer, or on its
/// address byte when it only reads from the retained pointer.
void applyDeviceFaults( World& world ) {
    sim::I2c&     bus        = world.i2c;
    std::uint32_t firstWrite = 0;
    bool          reads      = false;
    bool          seenWrite  = false;
    for ( std::uint32_t word : bus.commands ) {
        const I2cCommand command = decodeCommand( word );
        if ( command.op == I2C_LL_CMD_WRITE && !seenWrite ) {
            firstWrite = command.bytes;
            seenWrite  = true;
        }
        reads = reads || command.op == I2C_LL_CMD_READ;
    }
    const bool writesData =
        firstWrite > 1U && bus.txFifo.size() > 1U && ( bus.txFifo.front() & 1U ) == 0U;
    if ( !reads && !writesData ) {
        ++world.ina.probes;
        return;
    }
    const int pointer = writesData ? bus.txFifo[1] : world.ina.pointer;
    if ( reads ) {
        stallRead( pointer );
    }
    if ( transactionFails( pointer ) ) {
        ++( writesData ? bus.nackData : bus.nackAddress );
    }
}

/// A module reset: every register back at its reset value. A device that
/// holds SDA is not the controller's, and still holds it.
void resetI2c( sim::I2c& bus ) {
    bus.controllerClock = false;
    bus.sourceClock     = -1;
    bus.master          = false;
    bus.openDrain       = false;
    bus.arbitration     = true;
    bus.rxFullAckLevel  = -1;
    bus.txOrder         = -1;
    bus.rxOrder         = -1;
    bus.fifoMode        = true;
    bus.staged          = {};
    bus.live            = {};
    bus.txFifo.clear();
    bus.rxFifo.clear();
    bus.commands   = {};
    bus.enabled    = 0;
    bus.raw        = 0;
    bus.active     = false;
    bus.nackAt     = -1;
    bus.faultAt    = -1;
    bus.completeAt = -1;
    bus.fsmStuck   = false;
}

}  // namespace

namespace sim {

std::uint32_t I2c::sclHz() const noexcept {
    const std::uint32_t cycles =
        static_cast<std::uint32_t>( live.sclLow ) + live.sclHigh + live.sclWaitHigh;
    if ( live.clkmDiv == 0 || cycles == 0 ) {
        return 0;
    }
    return kI2cSourceHz / live.clkmDiv / cycles;
}

}  // namespace sim

void i2c_ll_master_cal_bus_clk( std::uint32_t source_clk, std::uint32_t bus_freq,
                                i2c_hal_clk_config_t* clk_cal ) {
    // ESP-IDF 6.0.3's arithmetic, restated (components/esp_hal_i2c/esp32s3/
    // include/hal/i2c_ll.h), with its assertion.
    const std::uint32_t clkm_div   = source_clk / ( bus_freq * 1024 ) + 1;
    const std::uint32_t sclk_freq  = source_clk / clkm_div;
    const std::uint32_t half_cycle = sclk_freq / bus_freq / 2;
    clk_cal->clkm_div              = static_cast<std::uint16_t>( clkm_div );
    clk_cal->scl_low               = static_cast<std::uint16_t>( half_cycle );
    clk_cal->scl_wait_high =
        static_cast<std::uint16_t>( bus_freq >= 80 * 1000 ? half_cycle / 2 - 2 : half_cycle / 4 );
    clk_cal->scl_high   = static_cast<std::uint16_t>( half_cycle - clk_cal->scl_wait_high );
    clk_cal->sda_hold   = static_cast<std::uint16_t>( half_cycle / 4 );
    clk_cal->sda_sample = static_cast<std::uint16_t>( half_cycle / 2 );
    clk_cal->setup      = static_cast<std::uint16_t>( half_cycle );
    clk_cal->hold       = static_cast<std::uint16_t>( half_cycle );
    clk_cal->tout       = static_cast<std::uint16_t>( static_cast<int>( sizeof( half_cycle ) * 8 )
                                                      - __builtin_clz( 5 * half_cycle ) + 2 );
    if ( !( clk_cal->scl_wait_high < clk_cal->sda_sample
            && clk_cal->sda_sample < clk_cal->scl_high ) ) {
        std::abort();  // HAL_ASSERT
    }
}

std::uint32_t i2c_ll_calculate_timeout_us_to_reg_val( std::uint32_t src_clk_hz,
                                                      std::uint32_t timeout_us ) {
    const std::uint32_t clk_cycle_num_per_us = src_clk_hz / ( 1 * 1000 * 1000 );
    return static_cast<std::uint32_t>( 32 - __builtin_clz( clk_cycle_num_per_us * timeout_us ) );
}

void i2c_ll_enable_bus_clock( int, bool enable ) {
    World& world = W();
    if ( world.rccDepth == 0 ) {
        ++world.i2c.rccViolations;
    }
    if ( enable && !world.i2c.busClock ) {
        world.i2c.core = sim::currentCore();
    }
    world.i2c.busClock = enable;
}

void i2c_ll_reset_register( int ) {
    World& world = W();
    if ( world.rccDepth == 0 ) {
        ++world.i2c.rccViolations;
    }
    ++world.i2c.resets;
    resetI2c( world.i2c );
}

void i2c_ll_set_source_clk( i2c_dev_t*, i2c_clock_source_t src_clk ) {
    i2cWrite().sourceClock = static_cast<int>( src_clk );
}

void i2c_ll_enable_controller_clock( i2c_dev_t*, bool en ) {
    i2cWrite().controllerClock = en;
}

void i2c_ll_set_mode( i2c_dev_t*, i2c_bus_mode_t mode ) {
    i2cWrite().master = mode == I2C_BUS_MODE_MASTER;
}

void i2c_ll_enable_pins_open_drain( i2c_dev_t*, bool enable_od ) {
    i2cWrite().openDrain = enable_od;
}

void i2c_ll_enable_arbitration( i2c_dev_t*, bool enable_arbi ) {
    i2cWrite().arbitration = enable_arbi;
}

void i2c_ll_master_rx_full_ack_level( i2c_dev_t*, int ack_level ) {
    i2cWrite().rxFullAckLevel = ack_level;
}

void i2c_ll_set_data_mode( i2c_dev_t*, i2c_trans_mode_t tx_mode, i2c_trans_mode_t rx_mode ) {
    sim::I2c& bus = i2cWrite();
    bus.txOrder   = static_cast<int>( tx_mode );
    bus.rxOrder   = static_cast<int>( rx_mode );
}

void i2c_ll_enable_fifo_mode( i2c_dev_t*, bool fifo_mode_en ) {
    i2cWrite().fifoMode = fifo_mode_en;
}

void i2c_ll_master_set_bus_timing( i2c_dev_t*, i2c_hal_clk_config_t* bus_cfg ) {
    sim::I2c& bus = i2cWrite();
    ++bus.timingWrites;
    sim::I2c::Timing& timing = bus.staged;
    timing.clkmDiv           = bus_cfg->clkm_div;
    timing.sclLow            = bus_cfg->scl_low;
    timing.sclHigh           = bus_cfg->scl_high;
    timing.sclWaitHigh       = bus_cfg->scl_wait_high;
    timing.sdaHold           = bus_cfg->sda_hold;
    timing.sdaSample         = bus_cfg->sda_sample;
    timing.setup             = bus_cfg->setup;
    timing.hold              = bus_cfg->hold;
    timing.tout              = bus_cfg->tout;
}

void i2c_ll_set_tout( i2c_dev_t*, int tout ) {
    i2cWrite().staged.tout = tout;
}

void i2c_ll_master_set_fractional_divider( i2c_dev_t*, std::uint8_t div_a, std::uint8_t div_b ) {
    sim::I2c& bus    = i2cWrite();
    bus.staged.fracA = div_a;
    bus.staged.fracB = div_b;
}

void i2c_ll_master_set_filter( i2c_dev_t*, std::uint8_t filter_num ) {
    i2cWrite().staged.filter = filter_num;
}

void i2c_ll_update( i2c_dev_t* ) {
    sim::I2c& bus = i2cWrite();
    bus.live      = bus.staged;
    ++bus.updates;
}

void i2c_ll_txfifo_rst( i2c_dev_t* ) {
    i2cWrite().txFifo.clear();
}

void i2c_ll_rxfifo_rst( i2c_dev_t* ) {
    i2cWrite().rxFifo.clear();
}

void i2c_ll_write_txfifo( i2c_dev_t*, const std::uint8_t* ptr, std::uint8_t len ) {
    sim::I2c& bus = i2cWrite();
    for ( std::uint8_t i = 0; i < len; ++i ) {
        if ( bus.txFifo.size() >= I2C_LL_FIFO_LEN ) {
            ++bus.txOverruns;
            continue;
        }
        bus.txFifo.push_back( ptr[i] );
    }
}

void i2c_ll_read_rxfifo( i2c_dev_t*, std::uint8_t* ptr, std::uint8_t len ) {
    sim::I2c& bus = i2cRegisters();
    for ( std::uint8_t i = 0; i < len; ++i ) {
        if ( bus.rxFifo.empty() ) {
            ++bus.emptyReads;
            ptr[i] = 0;
            continue;
        }
        ptr[i] = bus.rxFifo.front();
        bus.rxFifo.pop_front();
    }
}

void i2c_ll_master_write_cmd_reg( i2c_dev_t*, i2c_ll_hw_cmd_t cmd, int cmd_idx ) {
    sim::I2c& bus = i2cWrite();
    if ( cmd_idx < 0 || cmd_idx >= I2C_LL_CMD_REG_NUM ) {
        ++bus.protocolErrors;
        return;
    }
    bus.commands[static_cast<std::size_t>( cmd_idx )] = encodeCommand( cmd );
}

void i2c_ll_start_trans( i2c_dev_t* ) {
    World&    world = W();
    sim::I2c& bus   = i2cWrite();
    refreshI2c( world );
    if ( bus.active || bus.fsmStuck ) {
        ++bus.startsWhileActive;
        return;
    }
    ++bus.starts;
    if ( bus.onStart ) {
        bus.onStart();
    }
    if ( !i2cConfigured( bus ) ) {
        ++bus.misconfiguredStarts;
        return;
    }
    applyDeviceFaults( world );
    if ( bus.silent ) {
        bus.active     = true;
        bus.nackAt     = -1;
        bus.faultAt    = -1;
        bus.completeAt = -1;
        return;
    }
    runI2c( world );
    bus.commands = {};  // a done bit set in each, which the next list rewrites
}

void i2c_ll_enable_intr_mask( i2c_dev_t*, std::uint32_t mask ) {
    i2cWrite().enabled |= mask;
}

void i2c_ll_disable_intr_mask( i2c_dev_t*, std::uint32_t mask ) {
    i2cWrite().enabled &= ~mask;
}

void i2c_ll_clear_intr_mask( i2c_dev_t*, std::uint32_t mask ) {
    World& world = W();
    refreshI2c( world );
    i2cWrite().raw &= ~mask;
}

void i2c_ll_get_intr_mask( i2c_dev_t*, std::uint32_t* intr_status ) {
    World&    world = W();
    sim::I2c& bus   = i2cRegisters();
    refreshI2c( world );
    *intr_status = bus.raw & bus.enabled;
}

bool i2c_ll_is_bus_busy( i2c_dev_t* ) {
    World&    world = W();
    sim::I2c& bus   = i2cRegisters();
    refreshI2c( world );
    return bus.active || bus.fsmStuck || bus.sdaHeldLow;
}

void i2c_ll_master_clr_bus( i2c_dev_t*, std::uint32_t slave_pulses, bool ) {
    sim::I2c& bus = i2cWrite();
    ++bus.clears;
    bus.clearPulses = slave_pulses;
    if ( bus.sdaHeldLow && bus.releasedByClear && slave_pulses >= 9 ) {
        bus.sdaHeldLow = false;
    }
}

// --- UART0 (hal/uart_ll.h: the console's line) --------------------------------

uart_dev_t UART0 = {};

namespace {

/// UART0 of the calling world, after counting a register reached from a core
/// other than the console's (Uart::offCoreCalls).
sim::Uart& uartRegisters() {
    World& world = W();
    if ( sim::currentCore() != 0 ) {
        ++world.uart.offCoreCalls;
    }
    return world.uart;
}

/// The interrupt bits UART0 raises now: the latched ones and the receive
/// FIFO's level above its threshold.
std::uint32_t uartRaw( const sim::Uart& uart ) {
    std::uint32_t raw = uart.latched;
    if ( uart.rxFifo.size() > uart.rxFullThreshold ) {
        raw |= UART_INTR_RXFIFO_FULL;
    }
    return raw;
}

}  // namespace

namespace sim {

void World::dispatchUart() {
    if ( interruptMaskDepth > 0 ) {
        uart.dispatchDeferred = true;  // taken when the mask is released
        return;
    }
    uart.dispatchDeferred = false;
    const auto pending    = [this] { return ( uartRaw( uart ) & uart.enabled ) != 0; };
    for ( int core = 0; core < static_cast<int>( cpus.size() ); ++core ) {
        Cpu&       cpu   = cpus[static_cast<std::size_t>( core )];
        const auto route = cpu.routes.find( kUart0Source );
        if ( route == cpu.routes.end() || ( cpu.enabled & ( 1U << route->second ) ) == 0 ) {
            continue;
        }
        const CpuLine& line = cpu.lines[static_cast<std::size_t>( route->second )];
        if ( line.handler == nullptr ) {
            continue;
        }
        const int caller = currentCore();
        setCurrentCore( core );
        int runs = 0;
        while ( pending() && runs < kStormBound ) {
            line.handler( line.arg );
            ++runs;
        }
        setCurrentCore( caller );
        if ( pending() ) {
            ++uart.storms;
        }
    }
}

}  // namespace sim

void uart_ll_enable_bus_clock( uart_port_t, bool enable ) {
    World& world = W();
    if ( world.rccDepth == 0 ) {
        ++world.uart.rccViolations;
    }
    world.uart.busClock = enable;
}

void uart_ll_reset_register( uart_port_t ) {
    World& world = W();
    if ( world.rccDepth == 0 ) {
        ++world.uart.rccViolations;
    }
    // The module's registers back at their reset values; the line and the
    // knobs a test set stay.
    sim::Uart& uart      = world.uart;
    uart.sourceClock     = 0;
    uart.baud            = 115200;
    uart.dataBits        = -1;
    uart.parity          = -1;
    uart.stopBits        = -1;
    uart.flowCtrl        = -1;
    uart.normalMode      = false;
    uart.txIdle          = 10;
    uart.rxFullThreshold = 96;
    uart.rxTimeoutBits   = 0;
    uart.rxFifo.clear();
    uart.txFifo.clear();
    uart.latched = 0;
    uart.enabled = 0;
    ++uart.resets;
}

void uart_ll_set_sclk( uart_dev_t*, soc_module_clk_t source_clk ) {
    uartRegisters().sourceClock = static_cast<int>( source_clk );
}

bool _uart_ll_set_baudrate( uart_dev_t*, std::uint32_t baud, std::uint32_t sclk_freq ) {
    sim::Uart& uart = uartRegisters();
    if ( uart.refuseBaud || baud == 0 ) {
        return false;
    }
    uart.baud   = baud;
    uart.sclkHz = sclk_freq;
    return true;
}

void uart_ll_set_mode_normal( uart_dev_t* ) {
    uartRegisters().normalMode = true;
}

void uart_ll_set_data_bit_num( uart_dev_t*, uart_word_length_t data_bit ) {
    uartRegisters().dataBits = static_cast<int>( data_bit );
}

void uart_ll_set_parity( uart_dev_t*, uart_parity_t parity_mode ) {
    uartRegisters().parity = static_cast<int>( parity_mode );
}

void uart_ll_set_stop_bits( uart_dev_t*, uart_stop_bits_t stop_bit ) {
    uartRegisters().stopBits = static_cast<int>( stop_bit );
}

void uart_ll_set_hw_flow_ctrl( uart_dev_t*, uart_hw_flowcontrol_t flow_ctrl, std::uint32_t ) {
    uartRegisters().flowCtrl = static_cast<int>( flow_ctrl );
}

void uart_ll_set_tx_idle_num( uart_dev_t*, std::uint32_t idle_num ) {
    uartRegisters().txIdle = idle_num;
}

void uart_ll_rxfifo_rst( uart_dev_t* ) {
    sim::Uart& uart = uartRegisters();
    uart.rxFifo.clear();
    ++uart.rxFifoResets;
}

void uart_ll_txfifo_rst( uart_dev_t* ) {
    sim::Uart& uart = uartRegisters();
    uart.txFifo.clear();
    ++uart.txFifoResets;
}

void uart_ll_set_rxfifo_full_thr( uart_dev_t*, std::uint16_t full_thrhd ) {
    uartRegisters().rxFullThreshold = full_thrhd;
}

void uart_ll_set_rx_tout( uart_dev_t*, std::uint16_t tout_thrd ) {
    uartRegisters().rxTimeoutBits = tout_thrd;
}

void uart_ll_ena_intr_mask( uart_dev_t*, std::uint32_t mask ) {
    uartRegisters().enabled |= mask;
    W().dispatchUart();
}

void uart_ll_disable_intr_mask( uart_dev_t*, std::uint32_t mask ) {
    uartRegisters().enabled &= ~mask;
}

std::uint32_t uart_ll_get_intsts_mask( uart_dev_t* ) {
    const sim::Uart& uart = uartRegisters();
    return uartRaw( uart ) & uart.enabled;
}

void uart_ll_clr_intsts_mask( uart_dev_t*, std::uint32_t mask ) {
    uartRegisters().latched &= ~mask;
}

std::uint32_t uart_ll_get_rxfifo_len( uart_dev_t* ) {
    return static_cast<std::uint32_t>( uartRegisters().rxFifo.size() );
}

void uart_ll_read_rxfifo( uart_dev_t*, std::uint8_t* buf, std::uint32_t rd_len ) {
    sim::Uart& uart = uartRegisters();
    for ( std::uint32_t i = 0; i < rd_len; ++i ) {
        if ( uart.rxFifo.empty() ) {
            ++uart.emptyReads;
            buf[i] = 0;
            continue;
        }
        buf[i] = static_cast<std::uint8_t>( uart.rxFifo.front() );
        uart.rxFifo.pop_front();
    }
}

std::uint32_t uart_ll_get_txfifo_len( uart_dev_t* ) {
    World& world = W();
    world.drainTx();
    return static_cast<std::uint32_t>( UART_LL_FIFO_DEF_LEN - uartRegisters().txFifo.size() );
}

void uart_ll_write_txfifo( uart_dev_t*, const std::uint8_t* buf, std::uint32_t wr_len ) {
    World&     world = W();
    sim::Uart& uart  = uartRegisters();
    for ( std::uint32_t i = 0; i < wr_len; ++i ) {
        if ( uart.txFifo.size() >= UART_LL_FIFO_DEF_LEN ) {
            ++uart.txOverruns;
            continue;
        }
        uart.txFifo += static_cast<char>( buf[i] );
    }
    world.drainTx();
}

// --- RMT (hal/rmt_ll.h: the LED's off frame) --------------------------------

// The RMT's symbol RAM: the device's is at the address ESP-IDF's linker script
// gives RMTMEM, which status_led.cpp declares as these words.
extern "C" {
// NOLINTNEXTLINE(readability-identifier-naming): the linker script's name (D7)
volatile std::uint32_t RMTMEM[8 * 48] = {};
}

rmt_dev_t RMT = {};

namespace {

/// The APB clock the group divides, in picoseconds per cycle (80 MHz).
constexpr std::int64_t kApbPicoseconds = 12'500;

/// Whether the RMT's registers take a write: its bus clock on and its reset
/// pulsed since. A write that does not land is counted.
bool rmtLive( World& world ) {
    sim::Rmt& rmt = world.rmt;
    if ( rmt.busClock && rmt.resetPulsed ) {
        return true;
    }
    ++rmt.writesWhileGated;
    return false;
}

/// The transmit channel @p channel addresses, or nullptr (and the write lost)
/// when the registers take none.
sim::RmtTxChannel* txChannel( World& world, std::uint32_t channel ) {
    if ( !rmtLive( world ) ) {
        return nullptr;
    }
    return &world.rmt.tx.at( channel );
}

/// The frame channel 0 is sending now, if any.
sim::RmtFrame* runningFrame( World& world ) {
    sim::Rmt& rmt = world.rmt;
    if ( rmt.frames.empty() ) {
        return nullptr;
    }
    sim::RmtFrame& frame = rmt.frames.back();
    return world.now() < frame.endUs ? &frame : nullptr;
}

/// Plays channel 0's RAM as the hardware would from @p startUs: every half
/// of every word to the end marker, once, or once per counted pass in loop
/// mode. Leaves @p frame's halves empty and its end at kForever when the
/// channel would send nothing or never stop.
void playChannel( const sim::Rmt& rmt, sim::RmtFrame& frame ) {
    const sim::RmtTxChannel& channel = rmt.tx[0];
    frame.endUs                      = sim::kForever;
    const bool clocked               = rmt.groupClock && rmt.groupSource == RMT_CLK_SRC_APB
                                       && rmt.groupIntegral >= 1 && rmt.groupNumerator == 0
                                       && channel.divider >= 1;
    const bool runnable = clocked && rmt.memPowered && rmt.nonFifo && channel.memBlocks >= 1
                          && !channel.carrier && channel.pointerReset;
    if ( rmt.stall || !runnable ) {
        return;
    }
    const std::int64_t tick = kApbPicoseconds * rmt.groupIntegral * channel.divider;
    std::vector<std::pair<int, std::int64_t>> pass;
    bool                                      ended = false;
    for ( std::size_t i = 0; i < channel.memBlocks * 48 && !ended; ++i ) {
        const std::uint32_t word = RMTMEM[i];
        for ( const std::uint32_t half : { word & 0xFFFFU, word >> 16 } ) {
            const std::uint32_t ticks = half & 0x7FFFU;
            if ( ticks == 0 ) {
                ended = true;
                break;
            }
            pass.emplace_back( static_cast<int>( half >> 15 ), ticks * tick );
        }
    }
    if ( !ended ) {
        return;  // no end marker in the channel's memory: the model stops here
    }
    std::uint32_t passes = 1;
    if ( channel.loop ) {
        if ( !channel.loopCountEnabled || !channel.autostop || !channel.loopCountReset
             || channel.loopCount == 0 || channel.loopCount > RMT_LL_MAX_LOOP_COUNT_PER_BATCH ) {
            return;  // an endless loop, or a count the hardware does not run
        }
        passes = channel.loopCount;
    }
    std::int64_t total = 0;
    for ( std::uint32_t p = 0; p < passes; ++p ) {
        for ( const auto& half : pass ) {
            frame.halves.push_back( half );
            total += half.second;
        }
    }
    frame.endUs = frame.startUs + ( total + 999'999 ) / 1'000'000;
}

}  // namespace

namespace sim {

void World::updateRmtPads() {
    const RmtTxChannel& channel = rmt.tx[0];
    const bool          defined = rmt.busClock && channel.idleFixed;
    for ( std::size_t pin = 0; pin < pads.size(); ++pin ) {
        Pad& pad = pads[pin];
        if ( pad.outSignal != kSigRmtOut0 ) {
            continue;
        }
        const int level = defined ? channel.idleLevel : -1;
        if ( pad.driving() && level < 0 ) {
            ++rmt.undefinedLevels;
        }
        pad.peripheralLevel = level;
    }
}

}  // namespace sim

void periph_rcc_enter() {
    ++W().rccDepth;
}

void periph_rcc_exit() {
    --W().rccDepth;
}

std::uint8_t periph_rcc_acquire_enter( shared_periph_module_t periph ) {
    World& world = W();
    ++world.rccDepth;
    return static_cast<std::uint8_t>(
        world.rccReferences.at( static_cast<std::size_t>( periph ) ) );
}

void periph_rcc_acquire_exit( shared_periph_module_t periph, std::uint8_t ref_count ) {
    World& world                                                 = W();
    world.rccReferences.at( static_cast<std::size_t>( periph ) ) = ref_count + 1;
    --world.rccDepth;
}

// --- Timer groups and Timer Group 0's watchdog (hal/timg_ll.h, hal/mwdt_ll.h) -

timg_dev_t TIMERG0 = { 0 };
timg_dev_t TIMERG1 = { 1 };

namespace {

/// The group a register block stands for.
sim::TimerGroup& groupOf( World& world, int group ) {
    return world.timerGroups.at( static_cast<std::size_t>( group ) );
}

/// Whether a write to @p hw's watchdog reaches it: Timer Group 0's (the one
/// the model has), clocked, not stuck, and unlocked unless @p key is the
/// write to the key itself. A lost write is counted.
bool mwdtWrites( timg_dev_t* hw, bool key = false ) {
    World& world = W();
    if ( hw != &TIMERG0 ) {
        std::fprintf( stderr, "sim: only Timer Group 0's watchdog is modeled\n" );
        std::abort();
    }
    world.noteMwdt();
    if ( !groupOf( world, 0 ).busClock ) {
        ++world.mwdt.writesWhileGated;
        return false;
    }
    if ( world.mwdt.stuck ) {
        return false;
    }
    if ( !key && world.mwdt.locked ) {
        ++world.mwdt.lockedWrites;
        return false;
    }
    return true;
}

/// Restarts the counter at the current time, keeping the longest run.
void restartMwdt( World& world ) {
    sim::Mwdt& wdt = world.mwdt;
    if ( wdt.enabled ) {
        wdt.longestGapUs = std::max( wdt.longestGapUs, world.now() - wdt.lastFeedUs );
    }
    wdt.lastFeedUs = world.now();
}

}  // namespace

void sim::World::noteMwdt() {
    const bool armed = mwdt.enabled && mwdt.stageAction[0] != WDT_STAGE_ACTION_OFF;
    if ( armed && mwdt.resetAtUs < 0 && now_ - mwdt.lastFeedUs > mwdt.stage0Us() ) {
        mwdt.resetAtUs = mwdt.lastFeedUs + mwdt.stage0Us();
    }
}

void _timg_ll_enable_bus_clock( int group_id, bool enable ) {
    World& world = W();
    if ( world.rccDepth <= 0 ) {
        ++groupOf( world, group_id ).rccViolations;
    }
    groupOf( world, group_id ).busClock = enable;
}

void _timg_ll_reset_register( int group_id ) {
    World&           world = W();
    sim::TimerGroup& group = groupOf( world, group_id );
    if ( world.rccDepth <= 0 ) {
        ++group.rccViolations;
    }
    ++group.resets;
    if ( group_id != 0 || !group.busClock || world.mwdt.stuck ) {
        return;
    }
    // The watchdog's registers to their reset values (the key's is the
    // unlocking one), then the flash boot protection off, as the device's
    // function leaves it; the model's counts and faults stay.
    world.noteMwdt();
    sim::Mwdt& wdt       = world.mwdt;
    wdt.locked           = false;
    wdt.enabled          = false;
    wdt.stageAction      = {};
    wdt.stageHold        = { 26000000U, 134217727U, 1048575U, 1048575U };
    wdt.prescaler        = 1;
    wdt.flashboot        = false;
    wdt.cpuResetLength   = 1;
    wdt.sysResetLength   = 1;
    wdt.levelInterrupt   = false;
    wdt.interruptEnabled = false;
}

void brownout_hal_config( const brownout_hal_config_t* cfg ) {
    sim::Brownout& detector = W().brownout;
    ++detector.configs;
    detector.threshold      = cfg->threshold;
    detector.enabled        = cfg->enabled;
    detector.resetEnabled   = cfg->reset_enabled;
    detector.flashPowerDown = cfg->flash_power_down;
    detector.rfPowerDown    = cfg->rf_power_down;
}

void mwdt_ll_enable( timg_dev_t* hw ) {
    if ( mwdtWrites( hw ) && !W().mwdt.enabled ) {
        W().mwdt.enabled    = true;
        W().mwdt.lastFeedUs = W().now();
    }
}

void mwdt_ll_disable( timg_dev_t* hw ) {
    if ( mwdtWrites( hw ) ) {
        W().mwdt.enabled = false;
    }
}

bool mwdt_ll_check_if_enabled( timg_dev_t* hw ) {
    World& world = W();
    (void)hw;
    return groupOf( world, 0 ).busClock && !world.mwdt.stuck && world.mwdt.enabled;
}

void mwdt_ll_config_stage( timg_dev_t* hw, wdt_stage_t stage, std::uint32_t timeout,
                           wdt_stage_action_t behavior ) {
    if ( mwdtWrites( hw ) ) {
        const auto at                 = static_cast<std::size_t>( stage );
        W().mwdt.stageAction.at( at ) = static_cast<int>( behavior );
        W().mwdt.stageHold.at( at )   = timeout;
    }
}

void mwdt_ll_disable_stage( timg_dev_t* hw, std::uint32_t stage ) {
    if ( mwdtWrites( hw ) ) {
        W().mwdt.stageAction.at( stage ) = WDT_STAGE_ACTION_OFF;
    }
}

void mwdt_ll_set_level_intr( timg_dev_t* hw, bool enable ) {
    if ( mwdtWrites( hw ) ) {
        W().mwdt.levelInterrupt = enable;
    }
}

void mwdt_ll_set_cpu_reset_length( timg_dev_t* hw, wdt_reset_sig_length_t length ) {
    if ( mwdtWrites( hw ) ) {
        W().mwdt.cpuResetLength = static_cast<int>( length );
    }
}

void mwdt_ll_set_sys_reset_length( timg_dev_t* hw, wdt_reset_sig_length_t length ) {
    if ( mwdtWrites( hw ) ) {
        W().mwdt.sysResetLength = static_cast<int>( length );
    }
}

void mwdt_ll_set_flashboot_en( timg_dev_t* hw, bool enable ) {
    if ( mwdtWrites( hw ) ) {
        W().mwdt.flashboot = enable;
    }
}

void mwdt_ll_set_prescaler( timg_dev_t* hw, std::uint32_t prescaler ) {
    if ( mwdtWrites( hw ) ) {
        W().mwdt.prescaler = prescaler;
    }
}

void mwdt_ll_feed( timg_dev_t* hw ) {
    if ( mwdtWrites( hw ) ) {
        ++W().mwdt.feeds;
        restartMwdt( W() );
    }
}

void mwdt_ll_write_protect_enable( timg_dev_t* hw ) {
    if ( mwdtWrites( hw, true ) ) {
        W().mwdt.locked = true;
    }
}

void mwdt_ll_write_protect_disable( timg_dev_t* hw ) {
    if ( mwdtWrites( hw, true ) ) {
        W().mwdt.locked = false;
    }
}

void mwdt_ll_clear_intr_status( timg_dev_t* hw ) {
    if ( mwdtWrites( hw ) ) {
        ++W().mwdt.interruptClears;
    }
}

void mwdt_ll_set_intr_enable( timg_dev_t* hw, bool enable ) {
    if ( mwdtWrites( hw ) ) {
        W().mwdt.interruptEnabled = enable;
    }
}

esp_err_t esp_register_freertos_idle_hook_for_cpu( esp_freertos_idle_cb_t new_idle_cb,
                                                   UBaseType_t            cpuid ) {
    World& world = W();
    if ( cpuid >= world.idleHooks.size() ) {
        return ESP_ERR_INVALID_ARG;
    }
    if ( world.failIdleHook || world.idleHooks.at( cpuid ).size() >= 8 ) {
        return ESP_ERR_NO_MEM;
    }
    world.idleHooks.at( cpuid ).push_back( new_idle_cb );
    return ESP_OK;
}

void rmt_ll_enable_bus_clock( int, bool enable ) {
    World& world = W();
    if ( world.rccDepth <= 0 ) {
        ++world.rmt.rccViolations;
    }
    world.rmt.busClock = enable;
    if ( !enable ) {
        world.rmt.resetPulsed = false;
    }
    world.updateRmtPads();
}

void rmt_ll_reset_register( int ) {
    World& world = W();
    if ( world.rccDepth <= 0 ) {
        ++world.rmt.rccViolations;
    }
    if ( !world.rmt.busClock ) {
        ++world.rmt.writesWhileGated;
        return;
    }
    // Every register to its reset value; the RAM keeps what it holds.
    sim::Rmt& rmt       = world.rmt;
    rmt.resetPulsed     = true;
    rmt.memPowered      = false;
    rmt.nonFifo         = false;
    rmt.groupSource     = 0;
    rmt.groupIntegral   = 0;
    rmt.groupNumerator  = 0;
    rmt.groupClock      = false;
    rmt.interruptEnable = 0;
    rmt.raw             = 0;
    rmt.tx              = {};
    world.updateRmtPads();
}

void rmt_ll_mem_force_power_on( rmt_dev_t* ) {
    World& world = W();
    if ( rmtLive( world ) ) {
        world.rmt.memPowered = true;
    }
}

void rmt_ll_mem_force_low_power( rmt_dev_t* ) {
    World& world = W();
    if ( rmtLive( world ) ) {
        world.rmt.memPowered = false;
    }
}

void rmt_ll_mem_power_by_pmu( rmt_dev_t* ) {
    World& world = W();
    if ( rmtLive( world ) ) {
        world.rmt.memPowered = true;  // awake: the PMU keeps the RAM up
    }
}

void rmt_ll_enable_mem_access_nonfifo( rmt_dev_t*, bool enable ) {
    World& world = W();
    if ( rmtLive( world ) ) {
        world.rmt.nonFifo = enable;
    }
}

void rmt_ll_set_group_clock_src( rmt_dev_t*, std::uint32_t, rmt_clock_source_t src,
                                 std::uint32_t divider_integral, std::uint32_t,
                                 std::uint32_t divider_numerator ) {
    World& world = W();
    if ( rmtLive( world ) ) {
        world.rmt.groupSource    = static_cast<int>( src );
        world.rmt.groupIntegral  = divider_integral;
        world.rmt.groupNumerator = divider_numerator;
    }
}

void rmt_ll_enable_group_clock( rmt_dev_t*, bool en ) {
    World& world = W();
    if ( rmtLive( world ) ) {
        world.rmt.groupClock = en;
    }
}

void rmt_ll_enable_interrupt( rmt_dev_t*, std::uint32_t mask, bool enable ) {
    World& world = W();
    if ( rmtLive( world ) ) {
        world.rmt.interruptEnable =
            enable ? ( world.rmt.interruptEnable | mask ) : ( world.rmt.interruptEnable & ~mask );
    }
}

void rmt_ll_clear_interrupt_status( rmt_dev_t*, std::uint32_t mask ) {
    World& world = W();
    if ( rmtLive( world ) ) {
        world.rmt.raw &= ~mask;
    }
}

std::uint32_t rmt_ll_tx_get_interrupt_status_raw( rmt_dev_t*, std::uint32_t channel ) {
    World&    world = W();
    sim::Rmt& rmt   = world.rmt;
    if ( !rmt.frames.empty() && channel == 0 ) {
        sim::RmtFrame& frame = rmt.frames.back();
        if ( world.now() >= frame.endUs && !frame.stopped ) {
            rmt.raw |= static_cast<std::uint32_t>( rmt.tx[0].loop ? RMT_LL_EVENT_TX_LOOP_END( 0 )
                                                                  : RMT_LL_EVENT_TX_DONE( 0 ) );
        }
    }
    const std::uint32_t mask =
        static_cast<std::uint32_t>( RMT_LL_EVENT_TX_MASK( 0 ) | RMT_LL_EVENT_TX_ERROR( 0 ) )
        << channel;
    return rmt.raw & mask;
}

void rmt_ll_tx_reset_channels_clock_div( rmt_dev_t*, std::uint32_t ) {
    (void)rmtLive( W() );
}

void rmt_ll_tx_set_channel_clock_div( rmt_dev_t*, std::uint32_t channel, std::uint32_t div ) {
    if ( sim::RmtTxChannel* tx = txChannel( W(), channel ) ) {
        tx->divider = div;
    }
}

void rmt_ll_tx_reset_pointer( rmt_dev_t*, std::uint32_t channel ) {
    if ( sim::RmtTxChannel* tx = txChannel( W(), channel ) ) {
        tx->pointerReset = true;
    }
}

void rmt_ll_tx_start( rmt_dev_t*, std::uint32_t channel ) {
    World&             world = W();
    sim::RmtTxChannel* tx    = txChannel( world, channel );
    if ( tx == nullptr || channel != 0 ) {
        return;  // the model sends on channel 0 alone
    }
    sim::RmtFrame frame;
    frame.startUs = world.now();
    for ( std::size_t pin = 0; pin < world.pads.size(); ++pin ) {
        const sim::Pad& pad = world.pads[pin];
        if ( pad.driving() && pad.outSignal == sim::kSigRmtOut0 ) {
            frame.pins.push_back( static_cast<int>( pin ) );
        }
    }
    playChannel( world.rmt, frame );
    world.rmt.frames.push_back( std::move( frame ) );
    tx->pointerReset   = false;
    tx->loopCountReset = false;
}

void rmt_ll_tx_stop( rmt_dev_t*, std::uint32_t channel ) {
    World& world = W();
    if ( txChannel( world, channel ) == nullptr ) {
        return;
    }
    ++world.rmt.stops;
    if ( sim::RmtFrame* frame = runningFrame( world ) ) {
        frame->endUs   = world.now();
        frame->stopped = true;
    }
}

void rmt_ll_tx_set_mem_blocks( rmt_dev_t*, std::uint32_t channel, std::uint8_t block_num ) {
    if ( sim::RmtTxChannel* tx = txChannel( W(), channel ) ) {
        tx->memBlocks = block_num;
    }
}

void rmt_ll_tx_enable_wrap( rmt_dev_t*, std::uint32_t channel, bool enable ) {
    if ( sim::RmtTxChannel* tx = txChannel( W(), channel ) ) {
        tx->wrap = enable;
    }
}

void rmt_ll_tx_enable_loop( rmt_dev_t*, std::uint32_t channel, bool enable ) {
    if ( sim::RmtTxChannel* tx = txChannel( W(), channel ) ) {
        tx->loop = enable;
    }
}

void rmt_ll_tx_set_loop_count( rmt_dev_t*, std::uint32_t channel, std::uint32_t count ) {
    if ( sim::RmtTxChannel* tx = txChannel( W(), channel ) ) {
        tx->loopCount = count;
    }
}

void rmt_ll_tx_reset_loop_count( rmt_dev_t*, std::uint32_t channel ) {
    if ( sim::RmtTxChannel* tx = txChannel( W(), channel ) ) {
        tx->loopCountReset = true;
    }
}

void rmt_ll_tx_enable_loop_count( rmt_dev_t*, std::uint32_t channel, bool enable ) {
    if ( sim::RmtTxChannel* tx = txChannel( W(), channel ) ) {
        tx->loopCountEnabled = enable;
    }
}

void rmt_ll_tx_enable_loop_autostop( rmt_dev_t*, std::uint32_t channel, bool enable ) {
    if ( sim::RmtTxChannel* tx = txChannel( W(), channel ) ) {
        tx->autostop = enable;
    }
}

void rmt_ll_tx_fix_idle_level( rmt_dev_t*, std::uint32_t channel, std::uint8_t level,
                               bool enable ) {
    World& world = W();
    if ( sim::RmtTxChannel* tx = txChannel( world, channel ) ) {
        tx->idleFixed = enable;
        tx->idleLevel = level != 0 ? 1 : 0;
        world.updateRmtPads();
    }
}

void rmt_ll_tx_enable_carrier_modulation( rmt_dev_t*, std::uint32_t channel, bool enable ) {
    if ( sim::RmtTxChannel* tx = txChannel( W(), channel ) ) {
        tx->carrier = enable;
    }
}

// --- The interrupt allocator --------------------------------------------------

esp_err_t esp_intr_alloc( int, int, intr_handler_t, void*, intr_handle_t* ret_handle ) {
    sim::noteIdfAllocation( "esp_intr_alloc" );
    if ( ret_handle != nullptr ) {
        *ret_handle = nullptr;
    }
    return ESP_ERR_NOT_SUPPORTED;
}

esp_err_t esp_intr_free( intr_handle_t ) {
    sim::noteIdfAllocation( "esp_intr_free" );
    return ESP_ERR_NOT_SUPPORTED;
}

// --- FreeRTOS ---------------------------------------------------------------

struct SimQueue {
    std::size_t                   itemSize = 0;
    std::size_t                   capacity = 0;
    std::deque<std::vector<char>> items;
    bool                          binary = false;
    bool                          given  = false;
    int                           takers = 0;  ///< Tasks blocked taking the semaphore.
};

TaskHandle_t xTaskCreateStaticPinnedToCore( TaskFunction_t entry, const char* name,
                                            std::uint32_t stackBytes, void* argument,
                                            UBaseType_t priority, StackType_t* stack,
                                            StaticTask_t* tcb, BaseType_t core ) {
    if ( stack == nullptr || tcb == nullptr ) {
        return nullptr;
    }
    (void)stackBytes;
    World& world = W();
    if ( world.faults.failTask
         && ( world.faults.failTaskName.empty() || world.faults.failTaskName == name ) ) {
        return nullptr;
    }
    // The harness's tasks are threads with no handle of their own; the
    // control block stands for the created task, and the thread knows it as
    // its own (xTaskGetCurrentTaskHandle()).
    auto* const handle         = reinterpret_cast<TaskHandle_t>( tcb );
    world.taskNames[tcb]       = name;
    world.taskPriorities[name] = priority;
    world.spawn( [entry, argument, core, handle] {
        sim::setCurrentCore( core );
        tlTask = handle;
        entry( argument );
    } );
    return handle;
}

BaseType_t xTaskCreatePinnedToCore( TaskFunction_t entry, const char* name, std::uint32_t,
                                    void* argument, UBaseType_t priority, TaskHandle_t* handle,
                                    BaseType_t core ) {
    World& world = W();
    if ( world.faults.failTask
         && ( world.faults.failTaskName.empty() || world.faults.failTaskName == name ) ) {
        return pdFAIL;
    }
    if ( handle != nullptr ) {
        *handle = nullptr;
    }
    world.taskPriorities[name] = priority;
    world.spawn( [entry, argument, core] {
        sim::setCurrentCore( core );
        entry( argument );
    } );
    return pdPASS;
}

TaskHandle_t xTaskGetCurrentTaskHandle() {
    return sim::currentTask();
}

void vTaskPrioritySet( TaskHandle_t task, UBaseType_t priority ) {
    World&             world   = W();
    const TaskHandle_t subject = task != nullptr ? task : sim::currentTask();
    const auto         named   = world.taskNames.find( static_cast<const void*>( subject ) );
    const std::string  name    = named != world.taskNames.end() ? named->second : "";
    world.taskPriorities[name] = priority;
}

UBaseType_t uxTaskGetStackHighWaterMark( TaskHandle_t task ) {
    sim::World& world = sim::World::current();
    const auto  named = world.taskNames.find( static_cast<const void*>( task ) );
    if ( named != world.taskNames.end() ) {
        const auto own = world.stackFreeByName.find( named->second );
        if ( own != world.stackFreeByName.end() ) {
            return static_cast<UBaseType_t>( own->second );
        }
    }
    return static_cast<UBaseType_t>( world.stackFreeBytes );
}

esp_err_t esp_ipc_call_blocking( std::uint32_t cpu_id, esp_ipc_func_t func, void* arg ) {
    W().faults.ipcCpu = static_cast<int>( cpu_id );
    if ( W().faults.failIpc ) {
        return ESP_ERR_INVALID_STATE;
    }
    const int caller = sim::currentCore();
    sim::setCurrentCore( static_cast<int>( cpu_id ) );
    func( arg );
    sim::setCurrentCore( caller );
    return ESP_OK;
}

BaseType_t xPortGetCoreID() {
    return sim::currentCore();
}

esp_reset_reason_t esp_reset_reason() {
    return static_cast<esp_reset_reason_t>( sim::World::current().resetReason );
}

void vTaskDelay( TickType_t ticks ) {
    World& world = W();
    world.run( static_cast<sim::Micros>( ticks ) * 1000 );
}

QueueHandle_t xQueueCreate( UBaseType_t length, UBaseType_t itemSize ) {
    if ( W().faults.failQueue ) {
        return nullptr;
    }
    auto* queue     = sim::hold<SimQueue>();
    queue->itemSize = itemSize;
    queue->capacity = length;
    return queue;
}

BaseType_t xQueueSend( QueueHandle_t queue, const void* item, TickType_t wait ) {
    World& world = W();
    if ( !world.wait( [queue] { return queue->items.size() < queue->capacity; },
                      ticksToDeadline( wait ) ) ) {
        return pdFALSE;
    }
    const auto* bytes = static_cast<const char*>( item );
    queue->items.emplace_back( bytes, bytes + queue->itemSize );
    world.notify();
    return pdTRUE;
}

BaseType_t xQueueReceive( QueueHandle_t queue, void* item, TickType_t wait ) {
    World& world = W();
    if ( !world.wait( [queue] { return !queue->items.empty(); }, ticksToDeadline( wait ) ) ) {
        return pdFALSE;
    }
    std::memcpy( item, queue->items.front().data(), queue->itemSize );
    queue->items.pop_front();
    world.notify();
    return pdTRUE;
}

QueueHandle_t xQueueCreateStatic( UBaseType_t length, UBaseType_t itemSize, std::uint8_t* storage,
                                  StaticQueue_t* buffer ) {
    if ( storage == nullptr || buffer == nullptr ) {
        return nullptr;
    }
    return xQueueCreate( length, itemSize );
}

QueueHandle_t xQueueCreateMutexStatic( std::uint8_t type, StaticQueue_t* buffer ) {
    if ( buffer == nullptr
         || ( type != queueQUEUE_TYPE_MUTEX && type != queueQUEUE_TYPE_RECURSIVE_MUTEX ) ) {
        return nullptr;
    }
    SemaphoreHandle_t mutex = xSemaphoreCreateBinary();
    if ( mutex != nullptr ) {
        (void)xSemaphoreGive( mutex );  // a mutex starts free
    }
    return mutex;
}

SemaphoreHandle_t xSemaphoreCreateBinaryStatic( StaticSemaphore_t* buffer ) {
    if ( buffer == nullptr ) {
        return nullptr;
    }
    return xSemaphoreCreateBinary();
}

SemaphoreHandle_t xSemaphoreCreateBinary() {
    sim::Faults& faults = W().faults;
    const int    index  = faults.semaphoresMade++;
    if ( faults.failSemaphore
         && ( faults.failSemaphoreAt < 0 || faults.failSemaphoreAt == index ) ) {
        return nullptr;
    }
    auto* semaphore   = sim::hold<SimQueue>();
    semaphore->binary = true;
    return semaphore;
}

BaseType_t xSemaphoreTake( SemaphoreHandle_t semaphore, TickType_t wait ) {
    World& world = W();
    // A stall comes before the take: the task is not waiting meanwhile.
    world.stallIfDue();
    ++semaphore->takers;
    const bool taken =
        world.wait( [semaphore] { return semaphore->given; }, ticksToDeadline( wait ) );
    --semaphore->takers;
    if ( !taken ) {
        return pdFALSE;
    }
    semaphore->given = false;
    return pdTRUE;
}

BaseType_t xSemaphoreGive( SemaphoreHandle_t semaphore ) {
    if ( semaphore->given ) {
        return pdFALSE;
    }
    semaphore->given = true;
    W().notify();
    return pdTRUE;
}

BaseType_t xSemaphoreGiveFromISR( SemaphoreHandle_t semaphore, BaseType_t* woken ) {
    // A task blocked taking it is woken; FreeRTOS reports that only for a
    // task that outranks the one interrupted, and leaves the flag alone
    // otherwise (harness/sim.h, World::isrYields).
    ++W().isrGives;
    const bool       waiting = semaphore->takers > 0;
    const BaseType_t given   = xSemaphoreGive( semaphore );
    if ( woken != nullptr && given == pdTRUE && waiting ) {
        *woken = pdTRUE;
    }
    return given;
}

void simYieldFromIsr() {
    ++W().isrYields;
}
