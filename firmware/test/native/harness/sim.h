// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file sim.h
 * @brief The host stand-in for the meter's hardware and its RTOS: a
 *        deterministic scheduler on virtual time, and a model of the board
 *        the firmware drives (GPIO pads, the relay module and the circuit it
 *        switches, the INA228 on its I2C bus, I2C0's registers, UART0, the
 *        record logs' flash, the RMT, Timer Group 0's watchdog, the
 *        heap).
 *
 * The firmware's own sources are compiled unchanged against the fake ESP-IDF
 * headers in `idf/`, whose functions are implemented on this model. So a host
 * test runs the real Console, Sampler, Ina228Driver, relay::Bank and
 * CalibrationStore -- tasks, queues, semaphores and the ALERT interrupt
 * included -- and talks to them over the same line a host does.
 *
 * **The scheduler.** Every FreeRTOS task is a host thread, but only one runs
 * at a time: a thread holds the world's lock while it executes firmware code
 * and gives it up only inside a blocking call (a delay, a semaphore or queue
 * wait with a timeout, a UART read). Time is virtual. It moves only when every
 * thread is blocked -- then it jumps to the earliest deadline or interrupt --
 * and by one microsecond at every read of the clock, so that a busy-wait on
 * `esp_timer_get_time()` ends. A test is itself a thread of the world: it
 * holds the lock while it runs and lets the firmware run with `world.run()`.
 *
 * **Isolation.** Each test builds its own World and never destroys it: a
 * FreeRTOS task never returns, so its thread cannot be joined. A world left
 * behind is frozen -- every thread blocked in it stays blocked -- and its
 * memory is simply not reclaimed until the process exits.
 */
#pragma once

#include <freertos/FreeRTOS.h>

#include <algorithm>
#include <array>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <limits>
#include <list>
#include <map>
#include <mutex>
#include <span>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace sim {

using Micros = std::int64_t;

/// Never: a wait with no deadline (portMAX_DELAY).
constexpr Micros kForever = INT64_MAX;

/// The interrupt matrix's source for the GPIO (soc/interrupts.h).
constexpr std::uint32_t kGpioSource = 16;

/// The interrupt matrix's source for UART0 (soc/interrupts.h).
constexpr std::uint32_t kUart0Source = 27;

/// How many times in a row a level interrupt's handler may run with the
/// source still pending before the harness counts a storm (World::gpioStorms).
constexpr int kStormBound = 4;

// --- The board --------------------------------------------------------------

/// The GPIO matrix's output signals the harness models (soc/gpio_sig_map.h).
constexpr int kSigRmtOut0 = 81;   ///< RMT transmit channel 0.
constexpr int kSigGpioOut = 256;  ///< The pad's own output register.

/// One GPIO pad.
struct Pad {
    bool configured = false;  ///< gpio_config(), or the GPIO function selected (gpio_ll).
    /// The IO MUX function gpio_ll_func_sel() last selected (-1: as reset left it).
    int  function  = -1;
    bool output    = false;  ///< Output enabled (GPIO_MODE_OUTPUT / INPUT_OUTPUT).
    bool input     = false;
    int  latch     = 0;   ///< The output register, written whether or not the pad drives.
    int  forced    = -1;  ///< A level an external short holds the pad at, or -1.
    bool pullUp    = false;
    bool pullDown  = false;
    bool openDrain = false;
    /// The output enable taken from the peripheral, not the enable register.
    bool oneByPeripheral = false;
    /// What the matrix drives the pad with: kSigGpioOut (the latch) or a
    /// peripheral's signal, and whether it inverts it.
    int  outSignal = kSigGpioOut;
    bool inverted  = false;
    /// The level a peripheral signal gives the pad (World::rmt's idle level),
    /// or -1 while it gives none the harness can name.
    int peripheralLevel = 0;
    /// Each change of what drives the pad, in order: when, and the signal (or
    /// -1 once its output is disabled).
    std::vector<std::pair<std::int64_t, int>> drivers;

    /// The level on the pad the last time its output was enabled, and how
    /// often it was: what the wire saw first (-1 until then).
    int firstDriven = -1;
    int enabledAt   = 0;

    /// The level the pad reads back.
    int level() const noexcept;
    /// True while the pad drives its latch or a signal onto the wire.
    bool driving() const noexcept { return configured && output; }
};

/// Which part closes the shunt bypass on the modeled bench.
enum class BypassPart : std::uint8_t {
    Hw316,        ///< The HW-316's K2 channel: active low (before 0.8.0).
    Relay2Click,  ///< The Relay 2 Click's channel: active high, the rig.
};

/// Where the modeled bench's wires land: the GPIOs the INA228's lines and the
/// relays' inputs are on, rev D's unless a case moves them. What the firmware
/// is told of it is the pin map; the two disagree exactly when a case says so.
struct RigWires {
    int sda    = 4;   ///< The INA228's SDA.
    int scl    = 5;   ///< The INA228's SCL.
    int alert  = 6;   ///< The INA228's ALE.
    int lead   = 7;   ///< Relay 1's input, the DUT's lead.
    int bypass = 15;  ///< Relay 2's input, the shunt's bypass.

    bool leadActiveHigh = false;  ///< Relay 1's part is energized by a high level.
};

/// The DUT, the shunt and the relays that route its battery lead.
struct Circuit {
    BypassPart bypassPart = BypassPart::Relay2Click;

    /// What the DUT draws from its cell, nanoamps.
    std::int64_t dutNa = 50'000'000;

    /// When set, what the DUT draws at a moment of virtual time instead of
    /// dutNa: a burst, a square wave, a step.
    std::function<std::int64_t( Micros )> dutNaAt;

    /// The share of the DUT's current the closed bypass takes while K1 is also
    /// closed, in percent (the contact resistances' split).
    int bypassSharePercent = 60;

    /// Faults: a bypass contact that does not close the dry circuit, a lead
    /// contact that does not open.
    bool bypassStuckOpen = false;
    bool leadStuckClosed = false;

    /// The relay supply: false models a module with no 5 V, every coil off.
    bool relayPowered = true;

    /// Transitions into "the DUT has no battery" since the world began.
    int disconnects = 0;

    /// The last state seen, to count transitions.
    bool dutFed = true;
};

/// The INA228 on the bus, as a register file and a physical model.
struct Ina228Model {
    bool          present        = true;
    std::uint8_t  address        = 0x40;
    std::uint16_t manufacturerId = 0x5449;
    std::uint16_t deviceId       = 0x2281;  ///< DIEID 0x228, REV_ID 1.
    std::uint16_t adcConfigReset = 0xFB68;
    std::uint16_t shuntCalReset  = 0x1000;
    bool          memStat        = true;  ///< DIAG_ALRT MEMSTAT: the trim memory is good.

    /// The shunt the current really crosses, micro-ohms, and the offset the
    /// sense loop adds, picovolts.
    std::int64_t trueShuntMicroOhm = 100'000;
    std::int64_t offsetPv          = 480'000;

    /// What VBUS sees, microvolts, and the die temperature, milli-degrees.
    std::int64_t busUv = 3'900'000;
    std::int32_t dieMc = 25'000;

    /// Deterministic noise on VSHUNT, +/- this many counts.
    int noiseCounts = 0;

    /// DIAG_ALRT flags the model raises (MATHOF, SHNTOL, ...) besides CNVRF.
    std::uint16_t diagFlags = 0;

    /// Whether a conversion completes: CNVRF and the ALERT pulse.
    bool convert = true;

    /// How much later than its period a conversion sets CNVRF while nothing
    /// listens to the ALERT pin (`HeldUntilRead`, the clock deciding): a
    /// conversion the CONFIG write of a range switch restarted that ends after
    /// the switch's discard window, which `docs/ina228-reference.md` SS 8.2
    /// item 3 leaves possible. 0: the period itself.
    Micros cnvrfDelayUs = 0;

    /// A second part on the bus that acknowledges this 7-bit address (-1:
    /// none), as another device beside the INA228 would: a scan finds both.
    /// It holds no registers, so nothing but a probe should reach it.
    int alsoAnswers = -1;

    /// What the ALERT pin does at the end of a conversion.
    ///
    /// `HeldUntilRead` is the part on the bench (2026-11-20, `docs/architecture.md`
    /// SS 3.3): the pin goes low with CNVRF and stays low until DIAG_ALRT is
    /// read, whatever ALATCH says, so a conversion that ends while the pin is
    /// still low makes no edge. `Pulses` gives an edge per conversion whether
    /// or not anything reads (a pin that rings, with alertDivider above 1), and
    /// `Absent` none at all (a wire off its terminal).
    enum class AlertPin : std::uint8_t {
        HeldUntilRead,
        Pulses,
        Absent
    };
    AlertPin alertPin     = AlertPin::HeldUntilRead;
    int      alertDivider = 1;

    /// The next this many conversion edges are glitches: an edge on the wire
    /// with no conversion behind it, so CNVRF stays clear (`HeldUntilRead`).
    int alertGlitches = 0;

    /// The next this many ADC_CONFIG writes meet a conversion's end
    /// (`HeldUntilRead`): the conversion pulls the pin low and the write
    /// clears CNVRF behind it, so the pin stays low and a DIAG_ALRT read, with
    /// no flag to clear, does not release it; the first read after a later
    /// conversion does. The part on the bench, 2026-11-21: in 6 of 60 and 23
    /// of 150 `*TST?` calls the pin read high just before the self-test's
    /// ADC_CONFIG write and low just after it, and stayed low through the
    /// pacing probe's DIAG_ALRT read (`docs/architecture.md` SS 3.3).
    int conversionEndsInAdcConfigWrites = 0;

    /// `HeldUntilRead` state: the pin is low; a conversion ended since the last
    /// DIAG_ALRT read; and when that read (or an ADC_CONFIG write) last
    /// released the pin.
    bool   alertLow     = false;
    bool   cnvrfPending = false;
    Micros releasedAt   = 0;

    /// When the conversions now running began: the last ADC_CONFIG write (0
    /// from power-on). The part converts on its own grid from there, whether
    /// or not the ALERT interrupt is enabled, so an enable meets that grid at
    /// whatever phase it has instead of starting a conversion of its own.
    Micros convertingSince = 0;

    /// A CHARGE register value to report instead of the integral, or -1;
    /// with chargeJump non-zero, every read moves it by that many counts.
    std::int64_t chargeCounts = -1;
    std::int64_t chargeJump   = 0;

    /// The device's faults, which the bus (I2c, sim.cpp's runI2c()) turns
    /// into what the wire shows: a failing transaction is not acknowledged,
    /// on the register pointer it writes or, for a read from the retained
    /// pointer, on its address byte, so the own master answers it with
    /// `ESP_ERR_INVALID_RESPONSE`. A probe (an address and a STOP) never
    /// fails by these and does not count as a transaction.
    ///
    /// Fail every n-th transaction (0: never).
    int failEvery    = 0;
    int transactions = 0;

    /// Transactions to fail from now, and the register that fails (or -1 for
    /// any).
    int failCount    = 0;
    int failRegister = -1;
    /// Fail every transaction after this many more succeed (-1: never).
    int failAfter = -1;
    /// The next transaction holds its caller this long before it starts on
    /// the wire, whichever task makes it (0: none): a task stuck in a
    /// transaction, which the own master's budget cannot see because its
    /// clock has not moved for it (a preemption, a stall of the core).
    std::int64_t stallNextUs = 0;
    /// The next read of this register holds its caller the same way (0:
    /// none; one read, then cleared).
    std::int64_t stallReadUs       = 0;
    int          stallReadRegister = -1;
    /// DIAG_ALRT reads, all and those made by a thread other than the one
    /// that built the board (the test's, which stands for the console's).
    /// Probes of the bus. A call made off the bus's core is I2c's
    /// offCoreCalls, which every Bench and Scope requires at 0 (bench.h).
    int             probes             = 0;
    int             diagReads          = 0;
    int             diagReadsFromTasks = 0;
    std::thread::id creator            = std::this_thread::get_id();

    // Registers.
    std::array<std::uint64_t, 64> regs        = {};
    std::uint8_t                  pointer     = 0;
    std::uint32_t                 lcg         = 12345;
    Micros                        chargeSince = 0;

    void powerOnReset() noexcept;
};

/// UART0 as the firmware reaches it through hal/uart_ll.h (FW-12): its
/// clock, its configuration, its two 128-byte FIFOs, its interrupt registers,
/// and the line on both sides of it. A host's bytes arrive in the receive
/// FIFO one at a time (World::send(), World::sendRaw()), each raising the
/// interrupt the FIFO's level calls for, and the line idles after the last
/// one (the receive time-out); a byte that finds the FIFO full is lost, and
/// latches the overflow. The transmit FIFO empties onto the line at once,
/// into `tx`, unless the line is stalled (World::stallTx()).
struct Uart {
    /// The bus clock, on as the ROM leaves UART0, and the module resets.
    bool busClock = true;
    int  resets   = 0;
    /// Bus clock and reset calls made outside periph_rcc_enter(): every
    /// Bench and Scope requires none.
    int rccViolations = 0;
    /// The clock source (soc_module_clk_t, 0 before any) and what
    /// _uart_ll_set_baudrate() was asked for, with the source's frequency.
    int           sourceClock = 0;
    std::uint32_t baud        = 115200;
    std::uint32_t sclkHz      = 0;
    /// Fault: no divider makes the rate asked for (the LL answers false).
    bool refuseBaud = false;
    /// The frame (uart_word_length_t, uart_parity_t, uart_stop_bits_t,
    /// uart_hw_flowcontrol_t; -1 before any), the mode, the idle between
    /// frames.
    int           dataBits   = -1;
    int           parity     = -1;
    int           stopBits   = -1;
    int           flowCtrl   = -1;
    bool          normalMode = false;
    std::uint32_t txIdle     = 10;
    /// The receive FIFO's interrupt threshold and time-out (bit times, 0:
    /// off), at their reset values.
    std::uint32_t rxFullThreshold = 96;
    std::uint32_t rxTimeoutBits   = 0;
    /// The receive FIFO and its resets; the bytes a full FIFO lost.
    std::deque<char> rxFifo;
    int              rxFifoResets = 0;
    std::size_t      lostBytes    = 0;
    /// The latched interrupt bits (the time-out, the overflow) and the
    /// enables; the FIFO's threshold is a level, read from rxFifo.
    std::uint32_t latched = 0;
    std::uint32_t enabled = 0;
    /// The transmit FIFO's bytes not yet on the line, its resets, and what
    /// the line carried to the host.
    std::string txFifo;
    int         txFifoResets = 0;
    std::string tx;
    /// The line: stalled (the FIFO keeps every byte), and how many more
    /// bytes it carries before it stalls by itself (-1: no limit).
    bool         txStalled = false;
    std::int64_t txBudget  = -1;
    /// Faults of the firmware: a byte written into a full transmit FIFO, a
    /// byte read from an empty receive FIFO, a register reached from a core
    /// other than Core 0's console, and a level interrupt its handler left
    /// pending. Every Bench and Scope requires none.
    int txOverruns   = 0;
    int emptyReads   = 0;
    int offCoreCalls = 0;
    int storms       = 0;
    /// A dispatch the interrupt mask held back (World::dispatchUart()).
    bool dispatchDeferred = false;
};

/// I2C0 as the firmware reaches it through hal/i2c_ll.h (FW-13): its bus
/// clock and reset, its configuration, the timing registers staged and
/// latched, the two 32-byte FIFOs, the eight command registers, the
/// interrupt status, and the bus on the far side: a transaction runs the
/// command list against the INA228 model (Ina228Model's address, presence and
/// register file) at the latched clock, its status bits appearing as the
/// virtual clock reaches the bit times they take. Faults the harness can
/// arm: NACKs, a time-out that leaves the state machine stuck until a reset,
/// a device that holds SDA low until a bus clear's pulses (or for good), and
/// a controller that never answers.
struct I2c {
    /// The bus clock (off until the firmware turns it on) and the module's
    /// resets; bus clock and reset calls made outside periph_rcc_enter().
    bool busClock      = false;
    int  resets        = 0;
    int  rccViolations = 0;
    /// The core that turned the bus clock on, and register calls from any
    /// other: every Bench and Scope requires none.
    int core         = -1;
    int offCoreCalls = 0;
    /// Register calls while the bus clock was off: lost on the device.
    int writesWhileGated = 0;
    /// The configuration, as the reset leaves it (-1: never set).
    int  sourceClock     = -1;  ///< i2c_clock_source_t
    bool controllerClock = false;
    bool master          = false;
    bool openDrain       = false;
    bool arbitration     = true;
    int  rxFullAckLevel  = -1;
    int  txOrder         = -1;  ///< i2c_trans_mode_t
    int  rxOrder         = -1;
    bool fifoMode        = true;
    /// The timing registers as written (staged) and as the last
    /// i2c_ll_update() latched them (live): the cycles, the time-out's
    /// exponent, the fractional divider and the glitch filter.
    struct Timing {
        std::uint16_t clkmDiv     = 0;
        std::uint16_t sclLow      = 0;
        std::uint16_t sclHigh     = 0;
        std::uint16_t sclWaitHigh = 0;
        std::uint16_t sdaHold     = 0;
        std::uint16_t sdaSample   = 0;
        std::uint16_t setup       = 0;
        std::uint16_t hold        = 0;
        int           tout        = 0;
        int           fracA       = -1;
        int           fracB       = -1;
        int           filter      = 0;
    };
    Timing staged  = {};
    Timing live    = {};
    int    updates = 0;
    /// The SCL frequency the latched timing makes from the 40 MHz crystal (0
    /// when it makes none).
    std::uint32_t sclHz() const noexcept;
    /// The FIFOs and the command registers.
    std::deque<std::uint8_t>     txFifo;
    std::deque<std::uint8_t>     rxFifo;
    std::array<std::uint32_t, 8> commands = {};
    /// The interrupt enables and the raw status.
    std::uint32_t enabled = 0;
    std::uint32_t raw     = 0;
    /// The transaction on the wire: when its NACK, its fault and its STOP's
    /// completion come (-1: not this one), and a state machine a time-out
    /// left stuck until a reset.
    bool   active     = false;
    Micros nackAt     = -1;
    Micros faultAt    = -1;
    Micros completeAt = -1;
    bool   fsmStuck   = false;
    /// Faults to arm: the next this many transactions meet a NACK on their
    /// address byte, or on their first data byte; time out (SCL held past the
    /// time-out: TIME_OUT, the state machine stuck); and a controller that
    /// never raises anything (stays busy until a reset).
    int  nackAddress = 0;
    int  nackData    = 0;
    int  timeouts    = 0;
    bool silent      = false;
    /// A NACK whose STOP never completes: the bus stays busy after it until a
    /// reset.
    bool nackHolds = false;
    /// A device that holds SDA low: the bus reads busy until a bus clear of
    /// nine pulses or more frees it (or never, when releasedByClear is false).
    bool sdaHeldLow      = false;
    bool releasedByClear = true;
    /// Runs when a transaction starts, before it runs: a test's stand-in for
    /// whatever preempts the caller then.
    std::function<void()> onStart;
    /// What was asked: transactions started, the bus clears and their
    /// pulses, the timing registers written (i2c_ll_master_set_bus_timing():
    /// every set-up, recovery and clock change), and each transaction on the
    /// wire, as text: `S` a START, a hex byte written (`!` after one not
    /// acknowledged), `rN` N bytes read and acknowledged, `n1` the last byte
    /// read and not, `P` the STOP.
    int                      timingWrites = 0;
    int                      starts       = 0;
    int                      clears       = 0;
    std::uint32_t            clearPulses  = 0;
    std::vector<std::string> wire;
    /// Faults of the firmware: a start with the controller not set as the
    /// bus's master, a start while a transaction is still on the wire, a
    /// command list the hardware would not run as meant, a byte written into
    /// a full transmit FIFO and one read from an empty receive one. Every
    /// Bench and Scope requires none.
    int misconfiguredStarts = 0;
    int startsWhileActive   = 0;
    int protocolErrors      = 0;
    int txOverruns          = 0;
    int emptyReads          = 0;
};

/// The SPI flash under the record logs (flash_log.h, FW-15): four sectors
/// from @c base, the `calib` partition's offset
/// (flash_port::kLogBaseAddress), which are the calibration log's two and,
/// right after them, the pin map log's (flash_port::kPinLogBaseAddress,
/// FW-03), every byte erased (0xFF) on a new world,
/// reached through the harness's fakes of `esp_flash_read()`,
/// `esp_flash_write()` and `esp_flash_erase_region()` (harness/idf/
/// esp_flash.h), which flash_port.cpp calls as on the device (FW-16). As NOR
/// flash does, a write only clears bits (the bytes become old AND new) and an
/// erase sets a whole sector to 0xFF.
///
/// **The region check.** As ESP-IDF does on the main flash, the fake write
/// and erase ask the image's region check first
/// (`__wrap_esp_partition_is_flash_region_writable()`, flash_port.cpp) and
/// are refused with ESP_ERR_NOT_ALLOWED, the model untouched, where it
/// answers false; with @c regionChecked cleared they are not asked, for a
/// test of the log over a window at another address.
///
/// **A power cut.** With @c cutAt set, the change of that index (writes and
/// erases counted together from 0, in @c changes) is the last the flash
/// takes: only its first @c cutKeep bytes land (of the write, or erased from
/// the sector's start), and with @c cutTears the next byte of a write takes
/// only the bits of its low nibble, and the rest of an erased sector has bits
/// set at random (OR 0xA5), as an erase stopped part-way leaves it. Then the
/// power is gone: every later call fails and changes nothing, until
/// powerOn().
///
/// **Defects the firmware made**, which every Bench and Scope requires 0: a
/// write that asks a 1 where the flash holds a 0 (the write cannot make it;
/// the model keeps the AND), any access outside the window, an erase that
/// does not start a sector or is not one sector long, and a write or an
/// erase the region check refused.
struct Flash {
    static constexpr std::uint32_t kSectorBytes = 4096;         ///< As flash_log.h.
    static constexpr std::size_t   kSectors     = 4;            ///< Both logs' two.
    std::uint32_t                  base         = 0x00009000U;  ///< As flash_port.h.
    std::vector<std::uint8_t> bytes = std::vector<std::uint8_t>( kSectors * kSectorBytes, 0xFF );
    /// The power cut: the index of the change that is the last, the bytes of
    /// it that land, and whether the next byte or the rest is torn.
    long long   cutAt    = -1;
    std::size_t cutKeep  = 0;
    bool        cutTears = false;
    bool        powered  = true;
    /// Writes and erases the flash took (made with power), and every call.
    std::size_t                       changes  = 0;
    std::size_t                       reads    = 0;
    std::size_t                       programs = 0;
    std::array<std::size_t, kSectors> erases   = {};  ///< Per sector of the window.
    /// Faults: the next read fails after this many succeed (-1: none); a
    /// write or an erase reports an error and changes nothing; a write or
    /// an erase reports success and changes nothing; the byte at an address
    /// never takes a write (a worn cell), -1 for none.
    long long readsUntilFailure = -1;
    bool      failProgram       = false;
    bool      failErase         = false;
    bool      programLost       = false;
    bool      eraseLost         = false;
    long long stuckAddress      = -1;
    /// The fakes of `esp_flash_write()` and `esp_flash_erase_region()` ask
    /// the image's region check first, as ESP-IDF's do.
    bool regionChecked = true;
    /// The firmware's defects.
    int zeroToOne       = 0;
    int outOfWindow     = 0;
    int unalignedErase  = 0;
    int refusedByRegion = 0;
    /// Reads into a destination outside internal RAM, which ESP-IDF makes
    /// through a bounce buffer it takes from the heap (esp_flash_api.c).
    /// Every Scope requires 0.
    int bounceBufferReads = 0;

    bool read( std::uint32_t address, std::uint8_t* data, std::size_t length );
    bool program( std::uint32_t address, const std::uint8_t* data, std::size_t length );
    bool eraseSector( std::uint32_t address );
    /// Gives the power back: the cut is disarmed, the flash keeps what it holds.
    void powerOn();
    /// The byte at @p address, for a test to read or plant (inside the window).
    std::uint8_t& at( std::uint32_t address );
    /// Whether [@p address, +@p length) lies in the window.
    bool inWindow( std::uint32_t address, std::size_t length ) const;
};

struct Faults {
    bool        failQueue       = false;
    bool        failSemaphore   = false;
    int         failSemaphoreAt = -1;  ///< Fail the n-th semaphore created (0-based).
    bool        failTask        = false;
    std::string failTaskName;  ///< Only the task of this name fails.
    bool        failGpioConfig    = false;
    int         failGpioConfigPin = -1;
    int         semaphoresMade    = 0;
    bool        failIpc           = false;
    int         ipcCpu            = -1;  ///< The core the last IPC call asked for.
};

/// One CPU interrupt line of a core: its entry in the CPU's descriptor table
/// (esp_cpu_intr_get_desc()) and in the port's handler table.
struct CpuLine {
    int           priority     = 1;
    int           type         = 0;  ///< esp_cpu_intr_type_t: 0 level, 1 edge, 2 configurable.
    std::uint32_t flags        = 0;  ///< ESP_CPU_INTR_DESC_FLAG_SPECIAL (1), _RESVD (2).
    void ( *handler )( void* ) = nullptr;
    void* arg                  = nullptr;
};

/// One core's interrupt lines: the descriptor and handler tables, the lines it
/// has enabled, and the interrupt matrix's routes to it (source -> line).
struct Cpu {
    std::array<CpuLine, 32>      lines   = {};
    std::uint32_t                enabled = 0;
    std::map<std::uint32_t, int> routes;
};

/// The ESP32-S3's two cores as ESP-IDF 6.0.3 describes their lines
/// (components/esp_hw_support/port/esp32s3/esp_cpu_intr.c, with no Wi-Fi task
/// pinned to Core 0): no handler set and nothing enabled or routed.
std::array<Cpu, 2> esp32s3Cpus();

/// The core the calling thread stands for (sim.cpp): a task's pinned core,
/// the target core inside esp_ipc_call_blocking(), 0 for the test's thread.
int  currentCore();
void setCurrentCore( int core );
/// The task the calling thread is: the handle a static create returned for
/// it (xTaskGetCurrentTaskHandle()), null for the test's own thread.
TaskHandle_t currentTask();

/// One transmission of an RMT transmit channel, as the pads the matrix
/// routes it to see it.
struct RmtFrame {
    std::vector<int> pins;  ///< The pads the channel drove when it started.
    std::int64_t     startUs = 0;
    /// The waveform, half-symbol by half-symbol: the level and its duration
    /// in picoseconds. Empty when the channel could not send (no clock, a
    /// configuration the hardware does not run).
    std::vector<std::pair<int, std::int64_t>> halves;
    /// When the hardware stops: the end of the last counted pass, or
    /// kForever for a channel that never stops by itself.
    std::int64_t endUs   = 0;
    bool         stopped = false;  ///< rmt_ll_tx_stop() ended it first.
};

/// One RMT transmit channel's configuration registers.
struct RmtTxChannel {
    std::uint32_t divider          = 0;  ///< div_cnt: the group clock's divider (0: unset).
    std::uint32_t memBlocks        = 0;
    bool          carrier          = false;
    bool          idleFixed        = false;
    int           idleLevel        = 0;
    bool          wrap             = false;
    bool          loop             = false;  ///< tx_conti_mode
    bool          loopCountEnabled = false;
    std::uint32_t loopCount        = 0;
    bool          autostop         = false;
    /// Whether the read pointer and the loop counter were reset since the
    /// channel last started: a channel started without either replays from
    /// where the last one stopped, which the model does not run.
    bool pointerReset   = false;
    bool loopCountReset = false;
};

/// The ESP32-S3's RMT as the firmware reaches it through hal/rmt_ll.h (FW-10).
struct Rmt {
    bool busClock    = false;
    bool resetPulsed = false;  ///< rmt_ll_reset_register() since the clock went on.
    /// Bus clock and reset calls made outside periph_rcc_enter() (the device
    /// requires PERIPH_RCC_ATOMIC()'s scope for both).
    int rccViolations = 0;
    /// Register writes while the bus clock was gated or the module held in
    /// reset: lost on the device.
    int                         writesWhileGated = 0;
    bool                        memPowered       = false;
    bool                        nonFifo          = false;
    int                         groupSource      = 0;  ///< rmt_clock_source_t, 0 before any.
    std::uint32_t               groupIntegral    = 0;
    std::uint32_t               groupNumerator   = 0;
    bool                        groupClock       = false;
    std::uint32_t               interruptEnable  = 0;
    std::uint32_t               raw              = 0;
    std::array<RmtTxChannel, 4> tx               = {};
    std::vector<RmtFrame>       frames;
    int                         stops = 0;
    /// Fault: the channel never counts its loop out (a stuck count).
    bool stall = false;
    /// Times a pad the channel drives had no level the harness can name: the
    /// channel's idle level not fixed, or its clock gated, while routed.
    int undefinedLevels = 0;
};

/// A timer group's bus clock as the firmware reaches it through
/// hal/timg_ll.h (FW-11). On at boot, as the device's startup leaves Timer
/// Group 0 (its bootloader uses the group's watchdog).
struct TimerGroup {
    bool busClock = true;
    int  resets   = 0;  ///< _timg_ll_reset_register() pulses.
    /// Bus clock and reset calls made outside periph_rcc_enter() or
    /// periph_rcc_acquire_enter() (the device requires the lock for both).
    int rccViolations = 0;
};

/// The supply's brownout detector as the firmware reaches it through
/// hal/brownout_hal.h (FW-18): the configuration last written, which the
/// device's RTC controller would act on.
struct Brownout {
    int  configs        = 0;      ///< brownout_hal_config() calls.
    int  threshold      = -1;     ///< The level, or -1 never set.
    bool enabled        = false;  ///< The detector on.
    bool resetEnabled   = false;  ///< A hardware reset below the level.
    bool flashPowerDown = false;  ///< The flash suspended on a brownout.
    bool rfPowerDown    = false;  ///< The RF powered down on one.
};

/// Timer Group 0's main watchdog timer as the firmware reaches it through
/// hal/mwdt_ll.h (FW-11): its registers, and when the device would have
/// reset. The timer counts the APB's 80 MHz divided by the prescaler; a stage
/// whose hold runs out unfed takes its action, which the model records
/// rather than takes (resetAtUs) because a host test cannot reset.
struct Mwdt {
    /// The write key is not MWDT_LL_WKEY_VALUE: every other write is lost. A
    /// group reset leaves the key at its reset value, which is the unlocking
    /// one; ESP-IDF's startup leaves it locked.
    bool locked = true;
    /// Writes lost to the lock, and writes while the group's bus clock was
    /// off: every Bench and Scope requires none.
    int                          lockedWrites     = 0;
    int                          writesWhileGated = 0;
    bool                         enabled          = false;
    std::array<int, 4>           stageAction      = {};  ///< wdt_stage_action_t, by stage.
    std::array<std::uint32_t, 4> stageHold        = { 26000000U, 134217727U, 1048575U, 1048575U };
    std::uint32_t                prescaler        = 1;
    bool                         flashboot        = false;  ///< Off, as the bootloader leaves it.
    int                          cpuResetLength   = 1;      ///< wdt_reset_sig_length_t.
    int                          sysResetLength   = 1;      ///< wdt_reset_sig_length_t.
    bool                         levelInterrupt   = false;
    bool                         interruptEnabled = false;
    int                          interruptClears  = 0;
    /// The counter's last restart (a feed, or the enable), the feeds made,
    /// and the longest the timer ran between two of them while enabled.
    Micros      lastFeedUs   = 0;
    std::size_t feeds        = 0;
    Micros      longestGapUs = 0;
    /// When the first stage's hold ran out unfed with an action set: the
    /// device's reset, or -1 while it never did. Latched.
    Micros resetAtUs = -1;
    /// Fault: the registers keep no write, a group reset included, as with
    /// the group's clock lost.
    bool stuck = false;
    /// Set by a test that provokes the reset (DIAGnostic:WATChdog:TEST):
    /// every other Bench requires resetAtUs to stay -1.
    bool resetExpected = false;
    /// The first stage's hold in microseconds at the prescaler set.
    Micros stage0Us() const noexcept {
        return static_cast<Micros>( stageHold[0] ) * static_cast<Micros>( prescaler ) / 80;
    }
};

/// A Core 0 idle hook (esp_freertos_hooks.h).
using IdleHook = bool ( * )();

// --- The world --------------------------------------------------------------

/// A periodic interrupt source.
struct Interrupt {
    std::function<void()> handler;
    bool                  enabled = false;
    Micros                period  = 0;
    Micros                next    = 0;
};

class World {
   public:
    World();
    World( const World& )            = delete;
    World& operator=( const World& ) = delete;

    /// The world of the calling thread.
    static World& current();

    // --- Time and threads (callers hold the world's lock) -----------------

    Micros now() const noexcept { return now_; }

    /// Reads the clock as firmware does: clockReadUs pass, interrupts that
    /// fall due fire, and other ready threads get the CPU.
    Micros readClock();
    /// How long one clock read stands for: 1 us, or more to stand for a
    /// slower CPU's work between two reads (a test of work that never
    /// blocks).
    Micros clockReadUs = 1;

    /// Blocks until @p ready() or @p deadline; returns ready().
    bool wait( const std::function<bool()>& ready, Micros deadline );

    /// Lets the firmware run for @p duration of virtual time.
    void run( Micros duration );

    /// Runs until @p ready() or @p timeout passes; returns ready().
    bool runUntil( const std::function<bool()>& ready, Micros timeout );

    /// Wakes every waiter to re-check its condition.
    void notify();

    /// Starts a task.
    void spawn( std::function<void()> body );

    /// Starts ESP-IDF's main task, as its startup does before `app_main()`:
    /// `main`, on Core 0, at priority 1 (ESP_TASK_MAIN_PRIO), its handle
    /// mainTask, running @p body. No firmware fault refuses it: the firmware
    /// does not create it (FW-17: the console runs on it).
    void startMainTask( std::function<void()> body );
    /// The main task's control block, which stands for its handle.
    StaticTask_t mainTask = {};

    /// Registers a periodic interrupt; returns its index.
    std::size_t addInterrupt( std::function<void()> handler );
    Interrupt&  interrupt( std::size_t index ) { return interrupts_[index]; }

    /// Freezes the world and lets the calling test thread go.
    void leave();

    // --- The board ----------------------------------------------------------

    std::array<Pad, 49> pads = {};
    /// The input signals gpio_ll_set_input_signal_from() took off the GPIO
    /// matrix, to their IO MUX pins (U0RXD_IN_IDX for UART0's RX).
    std::map<std::uint32_t, bool> inputFromMatrix;
    /// The pad each input signal is read from through the matrix
    /// (gpio_ll_set_input_signal_matrix_source(): I2C0's two lines).
    std::map<std::uint32_t, std::uint32_t> inputSignalPad;
    /// The RMT (hal/rmt_ll.h) and the depth of the peripheral clock lock
    /// (esp_private/periph_ctrl.h), which every Bench and Scope requires back
    /// at 0, with no RMT clock call outside it.
    Rmt rmt      = {};
    int rccDepth = 0;
    /// Each shared module's references (periph_rcc_acquire_exit()), by
    /// shared_periph_module_t.
    std::array<int, 12> rccReferences = {};
    /// The timer groups (hal/timg_ll.h) and Timer Group 0's watchdog
    /// (hal/mwdt_ll.h, FW-11).
    std::array<TimerGroup, 2> timerGroups = {};
    Mwdt                      mwdt        = {};
    /// The brownout detector (hal/brownout_hal.h, FW-18): how many times it
    /// was configured, and the last configuration.
    Brownout brownout = {};
    /// Brings mwdt.resetAtUs up to now: latches the reset when the timer has
    /// run its first stage's hold unfed.
    void noteMwdt();
    /// The idle hooks registered on each core, in order, and the two faults:
    /// the next registration refused as a full table would be, and Core 0's
    /// idle task starved (its hooks stop running). The harness runs Core 0's
    /// hooks at most once a simulated millisecond, from every jump of time
    /// (every task waits) and from every clock read of another core's
    /// thread, as that core's idle task would run between its tasks: a
    /// model, not a measure of how busy Core 0 is. Never from a clock read
    /// of a Core 0 thread: a task of that core that spins starves them, as
    /// on the device.
    std::array<std::vector<IdleHook>, 2> idleHooks    = {};
    bool                                 failIdleHook = false;
    bool                                 idleStarved  = false;
    /// Re-evaluates the level every pad the RMT drives: its idle level, or
    /// none (Rmt::undefinedLevels).
    void        updateRmtPads();
    Circuit     circuit    = {};
    Ina228Model ina        = {};
    Uart        uart       = {};
    I2c         i2c        = {};
    Flash       flash      = {};
    Faults      faults     = {};
    std::string appVersion = "0.18.0";
    /// The TRACE ring the harness hands Sampler::begin(), in samples: smaller
    /// than the device's 1,048,576 so that a test can fill it in reasonable
    /// time (the device's is a static array in PSRAM, main.cpp).
    std::size_t ringSamples = 65536;
    /// heap_caps_malloc / heap_caps_free calls, by anyone: the firmware's own
    /// code makes none (docs/misra.md), and test_heap counts them.
    std::size_t heapAllocs = 0;
    std::size_t heapFrees  = 0;
    /// Every call of a fake whose ESP-IDF original allocates or frees on the
    /// device (idf_ledger.h, kAllocatingIdf), by entry point, in order, since
    /// this world was built: what the case's `app_main()` asked of ESP-IDF.
    /// bench::Bench and bench::Scope fail a case that called one outside
    /// kMadeSafeIdf.
    std::vector<std::string_view> idfLedger;
    /// What heap_caps_get_free_size() and its siblings report.
    std::size_t heapFreeInternal = 300 * 1024;
    std::size_t heapMinInternal  = 280 * 1024;
    std::size_t heapLargest      = 200 * 1024;
    std::size_t heapFreeSpiram   = 4 * 1024 * 1024;

    /// A TRACE ring of ringSamples samples for Sampler::begin(), owned by the
    /// world so that it outlives every task the sampler started on it.
    std::span<std::int32_t> ring() {
        rings_.emplace_back( ringSamples, 0 );
        return { rings_.back().data(), rings_.back().size() };
    }
    /// The ELF SHA-256 esptool writes into the image's description (made up),
    /// and whether esp_app_get_description() finds no description at all.
    std::array<std::uint8_t, 32> elfSha256 = { 0x5E, 0x1F, 0x0B, 0xA4, 0x7C, 0x22, 0x91, 0xD3,
                                               0x08, 0x6A, 0xF0, 0x3E, 0x44, 0xB7, 0x19, 0xC5 };
    bool                         noAppDescription = false;
    /// The factory MAC esp_efuse_mac_get_default reads (a made-up one), and
    /// whether that read fails.
    std::array<std::uint8_t, 6> mac     = { 0x02, 0x00, 0x5E, 0x10, 0x20, 0x30 };
    bool                        failMac = false;

    /// What esp_reset_reason() answers (an esp_reset_reason_t value), and the
    /// bytes uxTaskGetStackHighWaterMark() reports a task never touched.
    int           resetReason    = 1;
    std::uint32_t stackFreeBytes = 2048;
    /// A task's own figure, by its name, over stackFreeBytes.
    std::map<std::string, std::uint32_t> stackFreeByName;
    /// The name each created task's handle stands for.
    std::map<const void*, std::string> taskNames;
    /// Each task's priority by its name: the one it was created with, then
    /// what vTaskPrioritySet() last set ("" for the test's own thread).
    std::map<std::string, UBaseType_t> taskPriorities;

    /// The CPU's interrupt lines, by core (esp_cpu.h, esp_rom_sys.h's
    /// matrix route).
    std::array<Cpu, 2> cpus = esp32s3Cpus();
    /// How deep the calling core's interrupts are masked
    /// (portSET_INTERRUPT_MASK_FROM_ISR()): every Bench and Scope requires
    /// it back at 0.
    int interruptMaskDepth = 0;
    /// portYIELD_FROM_ISR() calls: how many times an interrupt handler asked
    /// for a switch because the semaphore it gave woke a task.
    /// xSemaphoreGiveFromISR() reports a wake when a task was blocked taking
    /// that semaphore, as FreeRTOS reports one that outranks the task it
    /// interrupted (every waiter does here: an interrupt interrupts an idle
    /// core or a task of lower priority than the sampler's).
    std::size_t isrYields = 0;
    /// Stalls of a task, by its name: its next wait (a delay, a semaphore or a
    /// queue wait) first holds it this long, as a preemption or a stall of its
    /// core would. It holds nothing meanwhile (the bus, the line and every
    /// other task go on) and sees nothing until the stall ends; then the entry
    /// is gone and the wait runs as it would have.
    std::map<std::string, Micros> taskStallsUs;
    /// Holds the calling task for its stall in taskStallsUs, if one is due.
    void stallIfDue();
    /// xSemaphoreGiveFromISR() calls: the interrupts that gave a semaphore,
    /// whether or not a task was waiting for it (isrYields counts those).
    std::size_t isrGives = 0;

    /// Takes a CPU line of @p core as ESP-IDF's allocator does for a driver's
    /// non-shared level-1 interrupt (the I2C master's did, until FW-14): the
    /// lowest line of priority 1, level-triggered, neither reserved nor
    /// special and with no handler (intr_alloc.c, is_vect_desc_usable()),
    /// which it marks with a handler of its own. Returns the line, or -1.
    int allocatorTakesLine( int core );

    /// The GPIO's interrupt registers (hal/gpio_ll.h): each pin's interrupt
    /// type, the pins whose interrupt is enabled, and the status register
    /// an edge latches.
    std::array<int, 49> gpioIntrType   = {};
    std::uint64_t       gpioIntrEnable = 0;
    std::uint32_t       gpioStatus     = 0;
    /// Times a GPIO interrupt was still pending after its handler ran
    /// sim::kStormBound times in a row: a level line its handler did not clear.
    /// Every Bench and Scope requires 0.
    int gpioStorms = 0;
    /// The return addresses the census's backtrace walk climbs, innermost
    /// first (esp_debug_helpers.h): the first is the caller of the hook's
    /// note(), and the walk ends after the last.
    std::vector<std::uint32_t> backtrace;
    /// The frame, counted from 0 for the first climb, that the walk finds not
    /// sane, so that esp_backtrace_get_next_frame() answers false there; past
    /// the script for none.
    std::size_t backtraceInvalidAt = std::numeric_limits<std::size_t>::max();
    /// The next climb's frame, counted from 0 (esp_backtrace_get_start() resets it).
    std::size_t backtraceAt = 0;
    /// Ranges a test places in PSRAM, as (first byte, length): esp_ptr_in_dram()
    /// answers false inside them, true everywhere else.
    std::vector<std::pair<const void*, std::size_t>> externalRam;
    /// Latches @p pin's status bit and raises the GPIO's interrupt on every
    /// core it is routed to and enabled on.
    void raiseGpio( int pin );
    /// Runs the GPIO's routed handlers while its interrupt is pending.
    void dispatchGpio();

    /// Where the INA228 and the relays are wired.
    RigWires wires;
    /// The ALERT edges on wires.alert once it is given an edge interrupt type
    /// (gpio_ll_set_intr_type()), and their interrupt index: the INA228's
    /// conversion grid, ticking while the pin's interrupt is enabled.
    std::map<int, std::size_t> gpioIsr;

    /// Re-times the ALERT interrupt after the model's divider changed.
    void retimeAlert();

    /// Re-evaluates the circuit after a pad changed.
    void updateCircuit();

    /// The current through the shunt now, nanoamps.
    std::int64_t shuntNa() const;

    /// What the DUT draws now, nanoamps (Circuit::dutNaAt or dutNa).
    std::int64_t dutNow() const;

    /// Whether the DUT is fed by its cell now.
    bool dutFed() const;

    /// The coil state the pads command, for a test's assertions.
    bool leadOpen() const;
    bool bypassClosed() const;

    // --- The line, as a host sees it ----------------------------------------

    /// Sends @p text and a newline to UART0, byte by byte (sendRaw()).
    void send( const std::string& text );

    /// Sends @p bytes to UART0 as they are: each into the receive FIFO (lost
    /// when it is full), the interrupt raised as the FIFO's level calls for,
    /// and, after the last, the receive time-out.
    void sendRaw( std::string_view bytes );

    /// Takes everything the line carried from UART0 so far.
    std::string takeOutput();

    /// Stalls the line: the transmit FIFO keeps what it is given and fills.
    void stallTx();
    /// Lets the line carry again: what the FIFO holds goes out at once.
    void resumeTx();
    /// Resumes the line at @p atUs of virtual time (resumeTx()).
    void resumeTxAt( Micros atUs );

    /// Runs UART0's routed handler while its interrupt is pending, unless the
    /// interrupt mask holds it back (then on the mask's release); counts a
    /// handler that leaves it pending (Uart::storms).
    void dispatchUart();
    /// Moves what the transmit FIFO holds onto the line, as far as the line
    /// carries.
    void drainTx();

   private:
    std::deque<std::vector<std::int32_t>> rings_;
    friend struct Lock;
    struct Waiter {
        std::function<bool()> ready;
        Micros                deadline;
        bool                  yielding = false;
        std::uint64_t         sequence = 0;  ///< A yielder's place in line.
    };
    bool anyReady( const Waiter* except, bool countYielders ) const;
    bool olderYielder( const Waiter* self ) const;
    void advance();
    void fireDue();
    void runIdleHooks();

    World*                  previousWorld_ = nullptr;
    void*                   previousLock_  = nullptr;
    mutable std::mutex      mutex_;
    std::condition_variable cv_;
    Micros                  now_      = 0;
    int                     runnable_ = 0;
    std::uint64_t           yields_   = 0;
    bool                    frozen_   = false;
    std::list<Waiter*>      waiters_;
    std::vector<Interrupt>  interrupts_;
    Micros                  nextIdleUs_ = 0;
};

}  // namespace sim
