// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file watchdog.h
 * @ingroup watchdog_timer
 * @brief The instrument's own watchdog: Timer Group 0's main watchdog timer,
 *        fed only once every supervised party has checked in.
 *
 * ESP-IDF's task watchdog kept a list of its subscribers on the heap, one
 * entry per task and one for Core 0's idle task, and claimed its timer's
 * interrupt through the allocator (docs/misra.md D1). This module does what it
 * did with nothing allocated (FW-11):
 *
 *  - **The timer** is Timer Group 0's MWDT, the one ESP-IDF's task watchdog
 *    used (`CONFIG_ESP_TASK_WDT_EN` is off, and the interrupt watchdog keeps
 *    Timer Group 1's). begin() claims the group's bus clock the way that
 *    watchdog did and sets one stage through `hal/mwdt_ll.h`: a **system
 *    reset** kTimeoutSeconds after the last feed. No interrupt is used and no
 *    panic handler runs: the chip resets, every relay releases to the
 *    measuring circuit, RTC memory survives with the coils the reset found
 *    (relay.h), and `esp_reset_reason()` reads the timer's reset of the
 *    digital core as `ESP_RST_TASK_WDT`, so `SYSTem:HEALth?` still says
 *    `reset=task_wdt`.
 *  - **The parties** are the console task, the sampler task and Core 0's idle
 *    task, one bit each in a static check-in mask. A task subscribes itself
 *    from its own body (subscribe()); the idle task checks in through an idle
 *    hook begin() registers on Core 0 (`esp_register_freertos_idle_hook_for_cpu()`,
 *    a static table of ESP-IDF's, nothing allocated), so a console that spins
 *    without blocking starves it and is caught too. Core 1's idle task is not
 *    supervised: the timer-paced sampler spins there by design
 *    (`docs/architecture.md` SS 5.1).
 *  - **The feed** is made by whichever party completes the round: checkIn()
 *    sets its bit, and the one whose bit makes the mask equal to the
 *    subscribed set clears it and feeds the timer. A party that stops
 *    checking in therefore resets the chip between kTimeoutSeconds and
 *    kTimeoutSeconds plus the longest gap of the others (the sampler's idle
 *    wait, about a second) after its last check-in, which is how ESP-IDF's
 *    task watchdog decided too.
 *
 * Everything is static: one State of atomics, constant-initialized, and the
 * timer's own registers. checkIn() is in IRAM, because the sampler's loop
 * calls it (`docs/architecture.md` SS 5.2).
 */
#ifndef KLEIDOS_METER_WATCHDOG_H
#define KLEIDOS_METER_WATCHDOG_H

#include <cstdint>

namespace kleidos::meter::watchdog {

/// @addtogroup watchdog_timer
/// @{

/// @brief Who the watchdog waits for, one check-in bit each.
enum class Party : std::uint8_t {
    Console = 0,  ///< The console: ESP-IDF's main task, `main`, Core 0 (FW-17).
    Sampler = 1,  ///< The sampler task (`meter-sample`, Core 1).
    Idle0   = 2,  ///< Core 0's idle task, through the idle hook begin() registers.
};

/// @brief How long the timer runs unfed before it resets the chip, in
///        seconds: the 5 s ESP-IDF's task watchdog had (`DIAGnostic:WATChdog?`
///        `timeout_s`).
constexpr std::uint32_t kTimeoutSeconds = 5;

/// @brief The timer's clock prescaler from the 80 MHz APB clock: 40,000, a
///        tick of 500 us (`MWDT_LL_DEFAULT_CLK_PRESCALER`, the value ESP-IDF's
///        watchdogs use).
constexpr std::uint32_t kPrescaler = 40000;

/// @brief The APB clock the timer group counts, in hertz. The ESP32-S3 keeps
///        it at 80 MHz at every CPU frequency this image runs (no power
///        management in `sdkconfig.defaults`).
constexpr std::uint32_t kApbHz = 80000000;

/// @brief The timer's ticks in one second at kPrescaler.
constexpr std::uint32_t kTicksPerSecond = kApbHz / kPrescaler;

static_assert( kTicksPerSecond * kPrescaler == kApbHz, "the tick is a whole number of APB cycles" );

/// @brief The stage's hold: kTimeoutSeconds in ticks.
constexpr std::uint32_t kTimeoutTicks = kTimeoutSeconds * kTicksPerSecond;

static_assert( kTimeoutTicks == 10000U, "5 s of 500 us ticks" );

/// @brief Whether a starved party reboots the instrument: the stage begin()
///        sets resets the system (`DIAGnostic:WATChdog?` `panic`, 1 on every
///        image since 0.14.0).
constexpr bool kResetsWhenStarved = true;

/// @brief The bit @p party holds in the check-in mask.
/// @param[in] party The party.
/// @return One bit, below bit 3.
constexpr std::uint32_t bitOf( Party party ) noexcept {
    return std::uint32_t{ 1 } << static_cast<std::uint32_t>( party );
}

/// @brief Whether @p seen, the parties that checked in since the last feed,
///        completes a round of @p subscribed.
/// @param[in] seen       The check-in mask.
/// @param[in] subscribed The parties the feed waits for; 0 before begin().
/// @retval true  Every subscribed party has checked in, and there is one.
/// @retval false A subscribed party has not, or nothing is subscribed.
constexpr bool roundComplete( std::uint32_t seen, std::uint32_t subscribed ) noexcept {
    return subscribed != 0U && ( seen & subscribed ) == subscribed;
}

/// @brief Starts the watchdog: forgets every subscription, registers Core 0's
///        idle hook, claims Timer Group 0's bus clock, sets the timer's one
///        stage (a system reset after kTimeoutTicks, no interrupt, the flash
///        boot protection off) and enables it, with only Party::Idle0
///        subscribed.
///
/// Called once, from `app_main()` before either task exists; it allocates
/// nothing. From its return a Core 0 that stops idling for kTimeoutSeconds
/// resets the chip, the boot's remaining steps included.
/// @retval true  The timer reads back enabled and the idle hook is set.
/// @retval false The idle hook could not be registered (the timer is left
///               untouched) or the timer did not take its configuration
///               (it is left disabled): nothing is supervised, and every
///               subscribe() answers false.
bool begin() noexcept;

/// @brief Adds @p party to the parties the feed waits for, and checks it in.
///
/// Called by each task from its own body before its loop, so that a task
/// that never reaches its loop is not waited for: the console's missing
/// answer is then the symptom, and `DIAGnostic:WATChdog?` names the party.
/// @param[in] party The calling task's party.
/// @retval true  The watchdog runs and now waits for @p party.
/// @retval false begin() did not start it; nothing changed.
bool subscribe( Party party ) noexcept;

/// @brief Checks @p party in, and feeds the timer when its check-in completes
///        the round (roundComplete()). Does nothing for a party that is not
///        subscribed. In IRAM; safe from either core.
/// @param[in] party The calling party.
void checkIn( Party party ) noexcept;

/// @brief Whether @p party is subscribed.
/// @param[in] party The party.
/// @retval true  The feed waits for it.
/// @retval false It never subscribed, or the watchdog does not run.
bool subscribed( Party party ) noexcept;

/// @}

}  // namespace kleidos::meter::watchdog

#endif  // KLEIDOS_METER_WATCHDOG_H
