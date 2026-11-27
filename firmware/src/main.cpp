// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file main.cpp
 * @ingroup app_entry
 * @brief `app_main()`: release the relays, blank the LED, bring the INA228 up,
 *        hand the bus to the sampler, then become the console.
 *
 * The order below is the whole of the boot sequence, and each step is where it
 * is for a reason. Before step 0, as `app_main()`'s first line, the heap
 * monitor marks the end of ESP-IDF's startup (heap::arm()), which touches no
 * hardware: every allocation and free from that line on is counted against
 * the firmware, the boot's included, and the required count is zero
 * (FW-17):
 *
 *  0. **The pin map is read, and the relay inputs are driven to their released
 *     levels before anything else** (pin_boot.h, relay.h). Released is the DUT
 *     connected through the shunt and the bypass open. Which GPIOs and which
 *     levels those are is the map's, so the stored map is read first, two
 *     flash sectors and nothing else (`docs/architecture.md` SS 9 gives the
 *     time it adds); until then the pins are the floating inputs reset left,
 *     which both parts read as released. From here on they are driven and
 *     never floating, so nothing that follows -- an LED frame, the
 *     calibration's read, a bring-up that hangs -- can leave a coil to chance.
 *     **The brownout detector is armed** right after them (brownout.h,
 *     FW-18): a hardware reset below 2.44 V, which ESP-IDF's startup no
 *     longer arms because its way allocated.
 *  1. **The LED goes dark next**, before anything that can fail. A
 *     WS2812-class part latches its last color across a reset, so flashing
 *     this image over the factory demo leaves the demo's color lit until an
 *     explicit all-zero frame is written (status_led.h;
 *     `docs/architecture.md` SS 8). It is also the one thing the
 *     owner is waiting for, so it does not depend on a sensor being wired yet.
 *     **The watchdog starts** right after it (watchdog.h): Timer Group 0's
 *     timer, waiting at first for Core 0's idle task alone, so a boot that
 *     hangs on this core resets the chip within its 5 s; each task subscribes
 *     itself when it runs. Nothing is allocated, and its verdict is
 *     `DIAGnostic:WATChdog?`'s.
 *  2. **The calibration**, the newest record of the calibration record log in
 *     the `calib` partition (calibration.h, flash_log.h), because an
 *     uncalibrated instrument is still a working one -- it just reports
 *     `q=uncal`. It reads the flash and allocates nothing (FW-16: NVS, which
 *     did, is gone).
 *  3. **The I2C bus and the bring-up**, on the map's bus or, when a stored
 *     map's does not answer, on the default one (pin_boot.h). Its verdict is
 *     carried, not thrown away: on anything but `BringUpResult::Ok` the console queues one error
 *     for it at boot and then refuses every command that would otherwise
 *     report an invented number (ina228.h).
 *  4. **The sampler, then the console**, in that order, so that the queue and
 *     the semaphores both tasks may reach exist before either task does (the
 *     dual-core rule of the Kleidos firmware repository, kept here).
 *  5. **`app_main()` becomes the console and never returns** (FW-17): the
 *     task ESP-IDF created to run it, `main` on Core 0, raises its priority
 *     to the console's and runs its loop (Console::run()). A returning
 *     `app_main()` deletes that task, and the idle task then frees its stack
 *     and control block, two frees after the boot; a console of its own
 *     would hold a second 8 KiB stack while the first went unused.
 *     `CONFIG_ESP_MAIN_TASK_STACK_SIZE` is the console's 8192
 *     (`sdkconfig.defaults`).
 *
 * Steps 3 and the sampler's half of 4 run on Core 1, through the IPC task
 * (0.14.0): the own I2C master takes the core its begin() runs on as the
 * bus's and serves no other (i2c_bus.h), and the ALERT pin's interrupt is
 * routed to a line of the core that installs it (the sampler's own static
 * route, interrupt.h), so both are where the sampler that uses them runs,
 * and UART0's, routed by the console's own driver here on Core 0 (uart0.h),
 * stays with the console.
 *
 * No GPIO is named here: how the instrument is wired is the pin map's
 * (pinmap.h), whose defaults are the rig's, quoted in `firmware/board/pins.toml`
 * from `docs/carrier-board.md`.
 */
#include "brownout.h"
#include "calibration.h"
#include "console.h"
#include "heap_monitor.h"
#include "ina228.h"
#include "pin_boot.h"
#include "pin_table.h"
#include "pinmap.h"
#include "relay.h"
#include "rtos_ticks.h"
#include "sampler.h"
#include "status_led.h"
#include "watchdog.h"

#include <driver/gpio.h>
#include <esp_attr.h>
#include <esp_err.h>
#include <esp_ipc.h>
#include <esp_rom_serial_output.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <array>
#include <cstdint>
#include <span>
#include <string_view>

namespace kleidos::meter {

namespace {

/// What the ROM writes when UART0 could not be set up (app_main()'s fallback).
constexpr std::string_view kConsoleUartFailed = "kleidos-meter: console uart install failed\n";

/// The core the sampler task is pinned to, and so the one its interrupts
/// belong on (sampler.cpp).
constexpr std::uint32_t kSamplerCpu = 1;

/// What the half of the boot that runs on the sampler's core takes and gives.
struct SamplerCoreBoot {
    Ina228Driver*           device       = nullptr;                  ///< The INA228.
    Sampler*                sampler      = nullptr;                  ///< The sampling engine.
    CalibrationStore*       calibration  = nullptr;                  ///< The calibration store.
    pin_boot::Live*         pins         = nullptr;                  ///< The pin map.
    std::span<std::int32_t> ring         = {};                       ///< The TRACE ring.
    BringUpResult           result       = BringUpResult::BusError;  ///< The bring-up's verdict.
    bool                    samplerReady = false;                    ///< The sampler started.
};

/// The bring-up and the sampler's begin(), run on the sampler's core: the own
/// I2C master serves the core Ina228Driver::openBus() runs on (i2c_bus.h), and
/// the ALERT pin's interrupt is routed by Sampler::begin() to a line of the
/// core it runs on (interrupt::routeHere()).
/// @param[in,out] context The SamplerCoreBoot to fill.
void bootOnSamplerCore( void* context ) noexcept {
    auto& boot                = *static_cast<SamplerCoreBoot*>( context );
    boot.result               = pin_boot::bringUp( *boot.device, *boot.pins );
    const pinmap::PinMap& map = boot.pins->map;
    boot.samplerReady         = boot.sampler->begin(
        *boot.device, *boot.calibration,
        static_cast<gpio_num_t>( map.pins[pinmap::roleIndex( pin_table::Role::Alert )] ),
        map.pullAlert == pinmap::Pull::Up, boot.ring );
}

}  // namespace

}  // namespace kleidos::meter

/// @addtogroup app_entry
/// @{

/// @brief The entry point ESP-IDF calls, by that name.
///
/// Runs the boot sequence the file describes -- map read, relays released, LED dark,
/// the watchdog running, the calibration, the bring-up, the sampler and the
/// console -- and then runs the console on this task for good: it never
/// returns, so the main task is never deleted and nothing frees its stack.
extern "C" void app_main( void ) {  // NOLINT(readability-identifier-naming): ESP-IDF's name
    using namespace kleidos::meter;

    // The first line: the heap monitor counts what the hooks saw before it as
    // ESP-IDF's startup and everything from here as since app_main()
    // (heap_monitor.h, `DIAGnostic:HEAP?`'s `pre_main_` and `_since_main`,
    // and the 0.7.0 steady state's, which starts here too). The statics below
    // are constant-initialized and run no code.
    heap::arm();

    // The instrument. Static, though app_main() never returns: this task's
    // 8 KiB stack is the console's working stack, which objects of this size
    // (the console's buffers alone are kilobytes) would eat into, and the
    // sampler's task holds references to them from before the console runs.
    // Local to this function, so nothing else can reach them but through the
    // references it hands out. None has a constructor that runs code -- every
    // member carries a default initializer -- so each is initialized before
    // app_main() runs (`constinit` makes the compiler prove it, and leaves no
    // guard) and there is no initialization order to reason about. The pins
    // themselves are the map's (pin_boot.h).
    static constinit relay::Bank      gRelays;
    static constinit CalibrationStore gCalibration;
    static constinit Ina228Driver     gDevice;
    static constinit Sampler          gSampler;
    static constinit Console          gConsole;
    static constinit pin_boot::Live   gPins;

    // The TRACE ring: 4 MiB, zero, reserved at link time in PSRAM's
    // .ext_ram.bss (`CONFIG_SPIRAM_ALLOW_BSS_SEG_EXTERNAL_MEMORY`) and handed
    // to the sampler, so that no capture allocates. The section is NOLOAD and
    // zeroed at boot, which is why it holds no initializer: one would be
    // dropped without a word (tests/firmware/test_no_dynamic_memory.py holds
    // that). Not `constinit`: a compiler will not evaluate a million elements
    // as a constant expression, and it needs none, because an array of int is
    // trivially constructible and so is zero-initialized with no guard.
    EXT_RAM_BSS_ATTR static std::array<std::int32_t, kTraceSamples> gRing;

    // The stored map, or the defaults, before a pin is driven: it says where
    // the relays are. A read of the flash and nothing else.
    gPins = pin_boot::load();

    // Its verdict is the console's to report: a pin that could not be driven
    // stays a floating input, which the module reads as off, and every relay
    // order is then refused rather than claimed.
    (void)gRelays.begin( gPins.map );

    // The brownout detector armed to reset the chip in hardware, in place of
    // ESP-IDF's handling, which allocated (brownout.h, FW-18): right after the
    // relays, the one step that must come first. Until this line the
    // bootloader's analog brownout reset holds.
    brownout::arm();

    led::blankAll();
    // A refusal leaves nothing supervised, which DIAGnostic:WATChdog? reports
    // as both subscriptions refused (watchdog::subscribe()).
    (void)watchdog::begin();

    // A load that finds no record it takes keeps the defaults: `q=uncal`.
    (void)gCalibration.load();

    SamplerCoreBoot boot;
    boot.device      = &gDevice;
    boot.sampler     = &gSampler;
    boot.calibration = &gCalibration;
    boot.pins        = &gPins;
    boot.ring        = gRing;
    // Run on the sampler's core and waited for. Should the IPC call be
    // refused, the same steps run here instead: the interrupts then sit on
    // Core 0, as before 0.14.0, the bus belongs to Core 0 (ina228.h), and the
    // instrument works the same, only with every sample's transactions made
    // there for the sampler.
    if ( esp_ipc_call_blocking( kSamplerCpu, &bootOnSamplerCore, &boot ) != ESP_OK ) {
        bootOnSamplerCore( &boot );
    }
    const BringUpResult result = boot.result;

    // The sampler is created even when the bring-up failed, so that the command
    // surface is one shape in both cases: its own start() refuses with
    // "no device (scan found none)" rather than the console having to know two
    // ways of saying no.
    //
    // Its verdict is carried, not discarded. If the queue, a semaphore, the
    // ALERT `gpio_config`, the interrupt's route or the task itself failed, the
    // console still answers -- but `announce()` queues an error for it at boot
    // and every `INITiate` is refused by name, instead of opening a capture
    // that can never produce a sample.
    const bool samplerReady = boot.samplerReady;

    if ( gConsole.begin( gDevice, gSampler, gCalibration, gRelays, gPins ) ) {
        gConsole.announce( result, samplerReady );
        // From here this task is the console: `main` on Core 0, its priority
        // raised to the console's, beside `meter-sample` on Core 1. It never
        // returns (Console::run()).
        gConsole.run();
    }
    // UART0 could not be set up (uart0::begin() refused the baud rate or
    // found no free CPU line), so nothing this firmware prints will ever
    // reach the wire. The ROM's character output needs no driver and is the
    // one way left to say why, into UART0's FIFO at whatever rate the port
    // was left at: 115200, the divider's value after the port's reset, when
    // the baud rate was refused, and kConsoleBaud when the route was.
    for ( const char c : kConsoleUartFailed ) {
        esp_rom_output_putc( c );
    }
    // The sampler keeps running and the watchdog keeps its subscriptions;
    // this task waits for good rather than return, which would free its
    // stack. The console never subscribed, so the watchdog does not wait for
    // it.
    while ( true ) {
        vTaskDelay( rtos::kWaitForever );
    }
}

/// @}
