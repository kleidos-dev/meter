// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file watchdog.cpp
 * @ingroup watchdog_timer
 * @brief Timer Group 0's MWDT set through its low-level layer, the check-in
 *        mask, and Core 0's idle hook.
 *
 * No driver, no interrupt and no allocation (FW-11): the timer is set and fed
 * through `hal/mwdt_ll.h`, the group's bus clock is claimed under ESP-IDF's
 * peripheral clock lock, and the mask is two atomics in internal RAM.
 */
#include "watchdog.h"

#include <esp_attr.h>
#include <esp_err.h>
#include <esp_freertos_hooks.h>
#include <esp_private/periph_ctrl.h>
#include <hal/mwdt_ll.h>
#include <hal/timg_ll.h>

#include <atomic>

namespace kleidos::meter::watchdog {

namespace {

/// The core whose idle task is supervised: Core 0, the console's. Core 1's
/// idle task is starved by the timer-paced sampler's spin by design.
constexpr std::uint32_t kIdleCore = 0;

/// The timer group the MWDT belongs to, as ESP-IDF's clock and reset calls
/// number it.
constexpr std::int32_t kTimerGroup = 0;

/// The stages of the timer: the first resets the system, the others are off.
constexpr std::uint32_t kStageCount = 4;

/// The subscriptions and the check-in mask, in one object reached through a
/// function so that no variable has namespace scope (docs/misra.md, rule
/// 6.7.2). Both are atomics in internal RAM, constant-initialized, read and
/// written by the two tasks and Core 0's idle hook on either core.
struct State {
    std::atomic<std::uint32_t> subscribed{ 0 };  ///< The parties the feed waits for; 0: stopped.
    std::atomic<std::uint32_t> seen{ 0 };        ///< The parties checked in since the last feed.
};

/// @brief The one State.
/// @return The subscriptions and the mask.
IRAM_ATTR State& state() noexcept {
    static constinit State instance;
    return instance;
}

/// @brief Feeds the timer: its registers unlocked, the feed written, and
///        locked again. The inlined register writes need no flash.
IRAM_ATTR void feedTimer() noexcept {
    mwdt_ll_write_protect_disable( &TIMERG0 );
    mwdt_ll_feed( &TIMERG0 );
    mwdt_ll_write_protect_enable( &TIMERG0 );
}

/// @brief Core 0's idle hook: checks the idle task in.
/// @retval true Always: the idle task may wait for an interrupt as usual.
bool idleCheckIn() noexcept {
    checkIn( Party::Idle0 );
    return true;
}

/// @brief Takes a reference on Timer Group 0's bus clock, as ESP-IDF's task
///        watchdog did (`task_wdt_impl_timergroup.c`): the first user turns
///        the clock on and pulses the group's reset, which also leaves the
///        MWDT's flash boot protection off (`timg_ll.h`).
///
/// The underscored LL functions are the ones the macros of the same name
/// without the underscore wrap; the macros' only addition is to require
/// `PERIPH_RCC_ACQUIRE_ATOMIC()`'s scope (a `for` with a comma expression,
/// MISRA's to refuse), whose lock periph_rcc_acquire_enter() and
/// periph_rcc_acquire_exit() are.
void claimTimerGroup() noexcept {
    const std::uint8_t users = periph_rcc_acquire_enter( PERIPH_TIMG0_MODULE );
    if ( users == 0U ) {
        _timg_ll_enable_bus_clock( kTimerGroup, true );
        _timg_ll_reset_register( kTimerGroup );
    }
    periph_rcc_acquire_exit( PERIPH_TIMG0_MODULE, users );
}

/// @brief Sets the timer up and starts it: stage 0 resets the system after
///        kTimeoutTicks of kPrescaler, the other stages, the interrupt and the
///        flash boot protection are off, and both reset signals are the
///        longest (3.2 us), as ESP-IDF's own watchdogs set them.
void startTimer() noexcept {
    mwdt_ll_write_protect_disable( &TIMERG0 );
    mwdt_ll_disable( &TIMERG0 );
    mwdt_ll_set_flashboot_en( &TIMERG0, false );
    for ( std::uint32_t stage = 0; stage < kStageCount; ++stage ) {
        mwdt_ll_disable_stage( &TIMERG0, stage );
    }
    mwdt_ll_set_level_intr( &TIMERG0, false );
    mwdt_ll_clear_intr_status( &TIMERG0 );
    mwdt_ll_set_intr_enable( &TIMERG0, false );
    mwdt_ll_set_cpu_reset_length( &TIMERG0, WDT_RESET_SIG_LENGTH_3_2us );
    mwdt_ll_set_sys_reset_length( &TIMERG0, WDT_RESET_SIG_LENGTH_3_2us );
    mwdt_ll_set_prescaler( &TIMERG0, kPrescaler );
    mwdt_ll_config_stage( &TIMERG0, WDT_STAGE0, kTimeoutTicks, WDT_STAGE_ACTION_RESET_SYSTEM );
    mwdt_ll_feed( &TIMERG0 );
    mwdt_ll_enable( &TIMERG0 );
    mwdt_ll_write_protect_enable( &TIMERG0 );
}

}  // namespace

bool begin() noexcept {
    State& own = state();
    own.subscribed.store( 0U );
    own.seen.store( 0U );
    if ( esp_register_freertos_idle_hook_for_cpu( &idleCheckIn, kIdleCore ) != ESP_OK ) {
        return false;
    }
    claimTimerGroup();
    // The idle task is waited for before the timer runs, so the first feed
    // needs it, and nothing else, until a task subscribes.
    own.subscribed.store( bitOf( Party::Idle0 ) );
    startTimer();
    if ( !mwdt_ll_check_if_enabled( &TIMERG0 ) ) {
        own.subscribed.store( 0U );
        return false;
    }
    return true;
}

bool subscribe( Party party ) noexcept {
    State& own = state();
    if ( own.subscribed.load() == 0U ) {
        return false;
    }
    (void)own.subscribed.fetch_or( bitOf( party ) );
    checkIn( party );
    return true;
}

IRAM_ATTR void checkIn( Party party ) noexcept {
    State&              own        = state();
    const std::uint32_t bit        = bitOf( party );
    const std::uint32_t subscribed = own.subscribed.load();
    if ( ( subscribed & bit ) == 0U ) {
        return;
    }
    const std::uint32_t seen = own.seen.fetch_or( bit ) | bit;
    if ( !roundComplete( seen, subscribed ) ) {
        return;
    }
    // The round is complete: a new one starts, and the timer is fed. Two
    // parties on the two cores that complete it at the same instant both
    // feed, which costs nothing; a check-in that lands between this one's
    // fetch_or() and this store is forgotten, and that party's next one
    // counts: the round ends later by at most one of its intervals.
    own.seen.store( 0U );
    feedTimer();
}

bool subscribed( Party party ) noexcept {
    return ( state().subscribed.load() & bitOf( party ) ) != 0U;
}

}  // namespace kleidos::meter::watchdog
