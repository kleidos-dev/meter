// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file heap_monitor.h
 * @ingroup heap_monitor
 * @brief Counts every heap allocation and free the image makes, from the
 *        first one ESP-IDF's startup makes, split by phase, so that what
 *        allocates and when is a measurement.
 *
 * The firmware's own code allocates nothing (docs/misra.md): its FreeRTOS
 * objects and task stacks are static, the TRACE ring is a static array in
 * PSRAM, and a source gate and an object gate hold it there. What can still
 * allocate is ESP-IDF, on its own behalf before `app_main()` and, until the
 * zero-allocation work packages are done, on the firmware's behalf after.
 * Until FW-14 a change of the I2C clock allocated (ESP-IDF's driver created
 * the device handle again), and until FW-16 a calibration save did (NVS);
 * neither does since: the own I2C master rewrites its timing registers, and
 * the calibration is a record of the flash log (`flash_log.h`).
 *
 * The counts come from ESP-IDF's heap hooks (`CONFIG_HEAP_USE_HOOKS`):
 * `esp_heap_trace_alloc_hook()` and `esp_heap_trace_free_hook()` are called
 * from every allocation and free in the image, whatever task or interrupt
 * makes it, and heap_monitor.cpp defines them. The counts are kept **by
 * phase, from the first hook call** (FW-07): what was allocated and freed
 * before `app_main()`'s first line (arm()), which is ESP-IDF's startup and its
 * own tasks, and what was since, which is everything the firmware's boot, its
 * operations and ESP-IDF's tasks have done since. The main task never ends
 * (FW-17: it runs the console), so nothing frees its stack, and the required
 * count since that line is zero allocations and zero frees, the boot's
 * included. Two older views of the same counts keep their keys
 * (docs/scpi.md): the steady state of 0.7.0 (`armed`, `allocs_after_init`,
 * `frees_after_init`), which until FW-17 began a quarter of a second after
 * the console's announcement and now begins at the same line, so its counts
 * are the since-main ones and `armed` reads 1 whenever the firmware runs; and
 * maintenance (`maint_allocs`, `maint_frees`), the allocations of a
 * maintenance operation, counted apart until FW-16 removed the last one, and
 * 0 since.
 *
 * `DIAGnostic:HEAP?` reports all of them and the heap's free figures
 * (docs/scpi.md).
 *
 * **The census** (FW-07) is a build option, off by default: compiled with
 * `METER_HEAP_CENSUS` defined (`PLATFORMIO_BUILD_SRC_FLAGS=-DMETER_HEAP_CENSUS`,
 * no environment of its own), the hooks also keep each event, its size, its
 * capabilities, its block and the call sites above the hook, in a
 * static ring of kCensusEvents entries in internal RAM. The console then
 * answers two diagnostic queries no other image has
 * (`DIAGnostic:HEAP:CENSus?` and `DIAGnostic:HEAP:CENSus:EVENt?`,
 * docs/scpi.md), and `scripts/heap_census.py` reads them and names each
 * allocation's site from the build's `firmware.elf`.
 */
#ifndef KLEIDOS_METER_HEAP_MONITOR_H
#define KLEIDOS_METER_HEAP_MONITOR_H

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <span>

namespace kleidos::meter::heap {

/// @addtogroup heap_monitor
/// @{

/// @brief What the hooks counted.
struct Counts {
    bool          armed;              ///< arm() has run: app_main() has begun.
    std::uint32_t allocsAfterInit;    ///< Allocations since arm(): allocsSinceMain (FW-17).
    std::uint32_t freesAfterInit;     ///< Frees since arm(): freesSinceMain (FW-17).
    std::uint32_t maintenanceAllocs;  ///< 0: no operation is maintenance since FW-16.
    std::uint32_t maintenanceFrees;   ///< 0: no operation is maintenance since FW-16.
    std::uint32_t preMainAllocs;      ///< Allocations before arm(), from the first.
    std::uint32_t preMainFrees;       ///< Frees before arm().
    std::uint32_t allocsSinceMain;    ///< Allocations since arm(), every one.
    std::uint32_t freesSinceMain;     ///< Frees since arm(), every one.
};

/// @brief Marks `app_main()`'s first line: what the hooks see from here on is
///        counted as since-main, what they saw before as pre-main, and the
///        steady state of 0.7.0 begins with it.
///
/// Called once, as `app_main()`'s first statement (FW-17: it was the
/// console's, a quarter of a second after its announcement, and the boot's
/// allocations were left out of the steady state); the host tests call it
/// to stand for that line. Nothing in the firmware's boot is exempt: what
/// ESP-IDF still allocates before it is listed in docs/misra.md (D1).
void arm() noexcept;

/// @brief Clears every count and the census and undoes arm(): the state
///        before the first hook call.
///
/// For the host tests, which boot the firmware more than once in a process.
void disarm() noexcept;

/// @brief Reads the counts.
/// @return The nine figures of Counts.
Counts counts() noexcept;

// --- The census -------------------------------------------------------------

/// The call sites kept for each census event: enough to climb out of the
/// hook and ESP-IDF's allocator (`heap_caps_*_base`, `heap_caps_malloc`,
/// `malloc` and the like, up to six frames) and through the deepest chain of
/// ESP-IDF calls the census has met (NVS's initialization until FW-16, ten
/// more) to `app_main()`.
inline constexpr std::size_t kCensusFrames = 16;

#ifdef METER_HEAP_CENSUS
/// The census ring's size in a census build: room for the whole boot (153
/// events on 0.16.6's image) and the operations a census session runs after
/// it, in 32 KiB of internal RAM.
inline constexpr std::size_t kCensusEvents = 384;
#else
/// The census ring's size in the plain image: none.
inline constexpr std::size_t kCensusEvents = 0;
#endif

/// @brief One slot of the census ring, written by the hooks on any task or
///        interrupt and read by the console.
///
/// Every field is an atomic so that a read racing a write is defined: the
/// writer clears #stamp, writes the rest and then sets #stamp, and the reader
/// keeps a slot only when #stamp names the event it asked for both before and
/// after it copied the rest.
struct CensusSlot {
    std::atomic<std::uint32_t>  stamp{ 0 };  ///< The event's index plus one, 0 while written.
    std::atomic<std::uint32_t>  flags{ 0 };  ///< kCensusAllocation, kCensusBeforeMain.
    std::atomic<std::uint32_t>  size{ 0 };   ///< Bytes asked for (0 for a free).
    std::atomic<std::uint32_t>  caps{ 0 };   ///< The capabilities asked for (0 for a free).
    std::atomic<std::uintptr_t> block{ 0 };  ///< The block allocated or freed.
    /// The call sites above the hook, innermost first, 0 past the last.
    std::array<std::atomic<std::uintptr_t>, kCensusFrames> callers{};
};

/// CensusSlot::flags: the event is an allocation (clear: a free).
inline constexpr std::uint32_t kCensusAllocation = 1U;
/// CensusSlot::flags: the event came before arm().
inline constexpr std::uint32_t kCensusBeforeMain = 2U;

/// @brief One census event as the console reads it.
struct CensusEvent {
    std::uint32_t                             index      = 0;      ///< Its place, 0 the first.
    bool                                      allocation = false;  ///< An allocation, else a free.
    bool                                      beforeMain = false;  ///< Before arm().
    std::uint32_t                             size       = 0;      ///< Bytes asked for.
    std::uint32_t                             caps       = 0;      ///< Capabilities asked for.
    std::uintptr_t                            block      = 0;      ///< The block.
    std::array<std::uintptr_t, kCensusFrames> callers    = {};     ///< Call sites, innermost first.
};

/// @brief What the census holds.
struct CensusSummary {
    std::size_t   capacity = 0;  ///< Slots of the ring; 0: the census is not built.
    std::uint32_t events   = 0;  ///< Events recorded since the first hook call.
    std::uint32_t first    = 0;  ///< The oldest event still held (older ones were overwritten).
};

/// @brief Replaces the census ring's storage and empties it.
///
/// The census build's ring is static and in place before the first hook call;
/// this is for the host tests, which give the census a ring of their own and
/// take it away (an empty span) to stand for the plain image.
/// @param[in] storage The slots, which must outlive their use; empty for none.
void attachCensus( std::span<CensusSlot> storage ) noexcept;

/// @brief Whether this image keeps a census (a ring of at least one slot).
/// @return True in a census build, or once a host test attached a ring.
bool censusBuilt() noexcept;

/// @brief Reads the census's size and how much of it is held.
/// @return The capacity, the events recorded and the oldest one held.
CensusSummary censusSummary() noexcept;

/// @brief Reads one census event.
/// @param[in]  index The event's place, 0 the first since the first hook call.
/// @param[out] event The event; written once its slot was found, and to be
///                   used only when the call returns true.
/// @return False when the event has not happened, was overwritten, or was
///         being written while it was read.
bool censusEvent( std::uint32_t index, CensusEvent& event ) noexcept;

/// @}

}  // namespace kleidos::meter::heap

#endif  // KLEIDOS_METER_HEAP_MONITOR_H
