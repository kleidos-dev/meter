// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file heap_monitor.cpp
 * @ingroup heap_monitor
 * @brief The heap hooks, the counters behind `DIAGnostic:HEAP?` and the
 *        census ring a census build keeps.
 */
#include "heap_monitor.h"

#include <esp_attr.h>
#include <esp_cpu_utils.h>
#include <esp_debug_helpers.h>
#include <esp_heap_caps.h>

#include <array>
#include <atomic>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <span>

namespace kleidos::meter::heap {

namespace {

/// The counters and the census, in one object reached through a function so
/// that no variable has namespace scope (docs/misra.md, rule 6.7.2). The hooks
/// run on whatever task or interrupt allocates, from the first allocation of
/// ESP-IDF's startup, and from IRAM, so everything they touch is an atomic in
/// internal RAM, constant-initialized before any hook can run. With PSRAM on,
/// ESP-IDF compiles atomics as calls (`CONFIG_STDATOMIC_S32C1I_SPIRAM_WORKAROUND`)
/// to its own `stdatomic_s32c1i`, which is placed in IRAM (`noflash`) and uses
/// the S32C1I instruction for an internal-RAM address, so they stay safe here.
struct State {
    std::atomic<bool>          armed{ false };        ///< arm() has run: app_main() has begun.
    std::atomic<std::uint32_t> preMainAllocs{ 0 };    ///< Allocations before arm().
    std::atomic<std::uint32_t> preMainFrees{ 0 };     ///< Frees before arm().
    std::atomic<std::uint32_t> sinceMainAllocs{ 0 };  ///< Allocations since arm().
    std::atomic<std::uint32_t> sinceMainFrees{ 0 };   ///< Frees since arm().
    std::atomic<std::uint32_t> censusNext{ 0 };       ///< The next census event's index.
    /// The census build's ring: kCensusEvents slots, none in the plain image.
    std::array<CensusSlot, kCensusEvents> ownSlots{};
    /// The ring the hooks write: #ownSlots, or one a host test attached.
    std::span<CensusSlot> census{ ownSlots };
};

/// @brief The one State, constant-initialized before any hook can run.
/// @return The counters and the census.
IRAM_ATTR State& state() noexcept {
    static constinit State instance;
    return instance;
}

/// The call sites of one event, innermost first.
using Frames = std::array<std::uintptr_t, kCensusFrames>;

/// @brief Reads the call sites above the function that called this one, the
///        way ESP-IDF's panic handler walks a stack.
///
/// `esp_backtrace_get_start()` spills the register windows onto the stack and
/// gives the frame of this function's caller (the hook's note()); each
/// `esp_backtrace_get_next_frame()` climbs one frame and says whether the
/// frame it reached has a sane stack pointer and code for its PC, and the walk
/// stops at the first that does not, or at the stack's last frame. Each
/// address kept is the call instruction itself (`esp_cpu_process_stack_pc()`:
/// the window bits replaced and the call's three bytes taken off), so a
/// symboliser reads it as is. Both helpers are in IRAM
/// (`CONFIG_ESP_SYSTEM_IN_IRAM`), as the hooks must be. Not inlined, so that
/// the first frame is always its caller's. The host tests build it against
/// the harness's walk, over frames a test scripts (`sim::World::backtrace`).
/// @param[out] frames The call sites, innermost first, 0 past the last.
IRAM_ATTR __attribute__( ( noinline ) ) void captureCallers( Frames& frames ) noexcept {
    esp_backtrace_frame_t frame = {};
    esp_backtrace_get_start( &frame.pc, &frame.sp, &frame.next_pc );
    for ( std::uintptr_t& site : frames ) {
        if ( frame.next_pc == 0U ) {
            return;
        }
        if ( !esp_backtrace_get_next_frame( &frame ) ) {
            return;
        }
        site = esp_cpu_process_stack_pc( frame.pc );
    }
}

/// @brief Writes one event into the census ring, overwriting the oldest once
///        the ring is full.
/// @param[in,out] s       The state.
/// @param[in]     flags   kCensusAllocation and kCensusBeforeMain as they apply.
/// @param[in]     block   The block allocated or freed.
/// @param[in]     size    Bytes asked for (0 for a free).
/// @param[in]     caps    Capabilities asked for (0 for a free).
/// @param[in]     callers The call sites.
IRAM_ATTR void record( State& s, std::uint32_t flags, std::uintptr_t block, std::uint32_t size,
                       std::uint32_t caps, const Frames& callers ) noexcept {
    const std::uint32_t index = s.censusNext.fetch_add( 1, std::memory_order_relaxed );
    CensusSlot&         slot  = s.census[index % s.census.size()];
    slot.stamp.store( 0, std::memory_order_release );
    slot.flags.store( flags, std::memory_order_relaxed );
    slot.size.store( size, std::memory_order_relaxed );
    slot.caps.store( caps, std::memory_order_relaxed );
    slot.block.store( block, std::memory_order_relaxed );
    for ( std::size_t i = 0; i < kCensusFrames; ++i ) {
        slot.callers[i].store( callers[i], std::memory_order_relaxed );
    }
    slot.stamp.store( index + 1U, std::memory_order_release );
}

/// @brief Empties the census ring: every slot unstamped, no event recorded.
/// @param[in,out] s The state.
void clearCensus( State& s ) noexcept {
    for ( CensusSlot& slot : s.census ) {
        slot.stamp.store( 0 );
    }
    s.censusNext.store( 0 );
}

/// @brief Counts one heap event in its phase.
/// @param[in,out] s          The state.
/// @param[in]     allocation True for an allocation, false for a free.
/// @param[in]     beforeMain True before arm().
IRAM_ATTR void countPhase( State& s, bool allocation, bool beforeMain ) noexcept {
    if ( beforeMain ) {
        (void)( allocation ? s.preMainAllocs : s.preMainFrees )
            .fetch_add( 1, std::memory_order_relaxed );
        return;
    }
    (void)( allocation ? s.sinceMainAllocs : s.sinceMainFrees )
        .fetch_add( 1, std::memory_order_relaxed );
}

/// @brief Counts one heap event by phase and keeps it in the census when one
///        is built.
///
/// Reached only from the two hooks at the end of this file, which name it
/// through the enclosing namespace.
/// @param[in] allocation True for an allocation, false for a free.
/// @param[in] block      The block, never null.
/// @param[in] size       Bytes asked for (0 for a free).
/// @param[in] caps       Capabilities asked for (0 for a free).
IRAM_ATTR void note( bool allocation, const void* block, std::size_t size,
                     std::uint32_t caps ) noexcept {
    State&     s          = state();
    const bool beforeMain = !s.armed.load( std::memory_order_relaxed );
    countPhase( s, allocation, beforeMain );
    if ( !s.census.empty() ) {
        Frames callers{};
        captureCallers( callers );
        const std::uint32_t flags =
            ( allocation ? kCensusAllocation : 0U ) | ( beforeMain ? kCensusBeforeMain : 0U );
        record( s, flags, std::bit_cast<std::uintptr_t>( block ),
                static_cast<std::uint32_t>( size ), caps, callers );
    }
}

}  // namespace

void arm() noexcept {
    state().armed.store( true );
}

void disarm() noexcept {
    State& s = state();
    s.armed.store( false );
    s.preMainAllocs.store( 0 );
    s.preMainFrees.store( 0 );
    s.sinceMainAllocs.store( 0 );
    s.sinceMainFrees.store( 0 );
    clearCensus( s );
}

Counts counts() noexcept {
    const State& s = state();
    // The 0.7.0 keys count what the since-main keys count (FW-17): the window
    // they read now opens at app_main()'s first line.
    return Counts{ s.armed.load(),
                   s.sinceMainAllocs.load(),
                   s.sinceMainFrees.load(),
                   0U,
                   0U,
                   s.preMainAllocs.load(),
                   s.preMainFrees.load(),
                   s.sinceMainAllocs.load(),
                   s.sinceMainFrees.load() };
}

void attachCensus( std::span<CensusSlot> storage ) noexcept {
    State& s = state();
    s.census = storage;
    clearCensus( s );
}

bool censusBuilt() noexcept {
    return !state().census.empty();
}

CensusSummary censusSummary() noexcept {
    const State&        s        = state();
    const std::size_t   capacity = s.census.size();
    const std::uint32_t events   = s.censusNext.load();
    const std::uint32_t first =
        ( events > capacity ) ? ( events - static_cast<std::uint32_t>( capacity ) ) : 0U;
    return CensusSummary{ capacity, events, first };
}

bool censusEvent( std::uint32_t index, CensusEvent& event ) noexcept {
    const State&        s        = state();
    const std::uint32_t recorded = s.censusNext.load();
    // With no ring nothing is ever recorded (note() writes only to a ring,
    // and attachCensus() and disarm() empty the count), so the first test
    // also refuses every index there, before the modulo below could divide
    // by its size.
    if ( index >= recorded || recorded - index > s.census.size() ) {
        return false;
    }
    const CensusSlot& slot = s.census[index % s.census.size()];
    if ( slot.stamp.load( std::memory_order_acquire ) != index + 1U ) {
        return false;
    }
    CensusEvent         copy;
    const std::uint32_t flags = slot.flags.load( std::memory_order_relaxed );
    copy.index                = index;
    copy.allocation           = ( flags & kCensusAllocation ) != 0U;
    copy.beforeMain           = ( flags & kCensusBeforeMain ) != 0U;
    copy.size                 = slot.size.load( std::memory_order_relaxed );
    copy.caps                 = slot.caps.load( std::memory_order_relaxed );
    copy.block                = slot.block.load( std::memory_order_relaxed );
    for ( std::size_t i = 0; i < kCensusFrames; ++i ) {
        copy.callers[i] = slot.callers[i].load( std::memory_order_relaxed );
    }
    // The slot is the event asked for only if no hook began to overwrite it
    // while the fields were copied.
    event = copy;
    return slot.stamp.load( std::memory_order_acquire ) == index + 1U;
}

}  // namespace kleidos::meter::heap

// ESP-IDF declares both hooks weak and calls them from every allocation and
// free when CONFIG_HEAP_USE_HOOKS is set (esp_heap_caps.h); these are the
// definitions it calls. In IRAM, because an allocation can come from an
// interrupt or while the flash cache is off: the declaration in
// esp_heap_caps.h places them there (HEAP_IRAM_ATTR), and a second section
// attribute here would conflict with it.

/// @cond
// cppcheck-suppress constParameterPointer ; the signature is ESP-IDF's
extern "C" void esp_heap_trace_alloc_hook( void* ptr, std::size_t size, std::uint32_t caps ) {
    if ( ptr != nullptr ) {
        kleidos::meter::heap::note( true, ptr, size, caps );
    }
}

// cppcheck-suppress constParameterPointer ; the signature is ESP-IDF's
extern "C" void esp_heap_trace_free_hook( void* ptr ) {
    if ( ptr != nullptr ) {
        kleidos::meter::heap::note( false, ptr, 0, 0 );
    }
}
/// @endcond
