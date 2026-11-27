// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file ina228.h
 * @ingroup ina228_driver
 * @brief Driver for the TI INA228 on the bench meter's I2C bus.
 *
 * Small and focused, written from the datasheet rather than vendored, which is
 * the standing rule (`AGENTS.md`, "Firmware": no third-party device library)
 * and also keeps the notice obligation at zero.
 *
 * The driver owns the bus, the device's address and the identity it read at
 * bring-up. It does not sample: pacing, the ring and the protocol belong to
 * sampler.h and console.h. It refuses to hand out numbers it has not proved it
 * can trust -- a failed bring-up is a verdict with a reason, and the sampler
 * will not start on one.
 *
 * **One core owns the bus** (0.16.5; on the own I2C master since FW-14):
 * every transaction -- a probe, a register transfer, a change of the bus
 * clock -- is made on the core that opened the bus. The bus is the own I2C
 * master (i2c_bus.h), which records the core its begin() ran on and refuses
 * every later call from the other one before it touches a register, so the
 * rule is the bus's own as well as the driver's. Asked from the other core,
 * the driver makes the call there through `esp_ipc_call_blocking()`, one
 * transaction per call, and makes none at all when that core refuses. Up to
 * FW-14 the bus was ESP-IDF's I2C master, whose interrupt ran on that core
 * and whose NACK path, met from the far core, left a STOP's completion
 * latched for the next transaction to take as its own (`docs/architecture.md`
 * SS 2.2); the own master clears every status before a transaction ends, and
 * the routing stays because the bus refuses the far core. The sampler, on
 * the bus's core, pays one comparison of the core number per transaction for
 * the rule.
 *
 * Register addresses, fields and LSBs are in ina228_regs.h, each citing the
 * datasheet section it came from.
 */
#ifndef KLEIDOS_METER_INA228_H
#define KLEIDOS_METER_INA228_H

#include "i2c_bus.h"
#include "ina228_regs.h"

#include <driver/gpio.h>
#include <esp_err.h>

#include <cstddef>
#include <cstdint>

namespace kleidos::meter {

/// @addtogroup ina228_driver
/// @{

/// Outcome of the bring-up sequence (`docs/architecture.md` SS 2.3). Every
/// failure has its own error text (queued as `-241` at boot), and every one of
/// them stops the sampler: an instrument that cannot prove what it is talking
/// to must not print numbers.
enum class BringUpResult : std::uint8_t {
    Ok,                ///< Identified, defaults verified, trim memory good, configured.
    NoDevice,          ///< Nothing acknowledged anywhere in the strap address space.
    NotIna228,         ///< Something answered, but MANUFACTURER_ID / DIEID say it is not an INA228.
    BadResetDefaults,  ///< ADC_CONFIG or SHUNT_CAL did not read back their documented reset values.
    TrimChecksum,      ///< DIAG_ALRT MEMSTAT = 0: the device's own trim memory is suspect.
    BusError,          ///< An I2C transaction failed outright.
};

/// @brief A human-readable reason for @p result, in the wording the console
/// prints.
///
/// @param[in] result The bring-up outcome to describe.
/// @return A string literal; `"unknown"` for a value outside the enum.
const char* bringUpReason( BringUpResult result ) noexcept;

/// The INA228 as this instrument uses it.
class Ina228Driver {
   public:
    Ina228Driver()                                 = default;
    ~Ina228Driver()                                = default;
    Ina228Driver( const Ina228Driver& )            = delete;
    Ina228Driver& operator=( const Ina228Driver& ) = delete;
    Ina228Driver( Ina228Driver&& )                 = delete;
    Ina228Driver& operator=( Ina228Driver&& )      = delete;

    /// The slowest bus clock setBusSpeed() takes: standard mode's 100 kHz
    /// halved (i2c_bus::kMinClockHz).
    static constexpr std::uint32_t kMinBusSpeedHz = i2c_bus::kMinClockHz;
    /// The fastest: fast-mode plus (i2c_bus::kMaxClockHz).
    static constexpr std::uint32_t kMaxBusSpeedHz = i2c_bus::kMaxClockHz;

    /// @brief Sets up the own I2C master (i2c_bus::begin()) on @p sda / @p scl
    /// at @p sclHz.
    ///
    /// The breakout carries its own pull-ups, and the bus speed the design
    /// settles on assumes those (`docs/architecture.md` SS 2.1); @p pulls says
    /// which lines have the chip's as well, a safety net the default map sets
    /// on both. The calling core becomes the bus's, and every later
    /// transaction is made on it (the file's comment; app_main() opens it on
    /// the sampler's). Nothing is allocated.
    ///
    /// @param[in] sda   GPIO for the I2C data line.
    /// @param[in] scl   GPIO for the I2C clock line.
    /// @param[in] sclHz Bus clock in Hz, kMinBusSpeedHz to kMaxBusSpeedHz.
    /// @param[in] pulls Which lines have the internal pull-up.
    /// @return `ESP_OK`; `ESP_ERR_INVALID_STATE` if the bus is already open; or
    ///         `ESP_ERR_INVALID_ARG` for a pin or clock i2c_bus::begin()
    ///         refuses (the bus then stays closed).
    esp_err_t openBus( gpio_num_t sda, gpio_num_t scl, std::uint32_t sclHz,
                       i2c_bus::Pulls pulls ) noexcept;

    /// @brief Forgets the bus, so that the next openBus() may set it up on
    /// other pads.
    ///
    /// Touches no register and makes no transaction: every transaction is
    /// refused until the next openBus(), and the address, the clock and the
    /// identity read back as before the first one. The pads stay routed to the
    /// controller until their owner parks them (relay::Bank::park()).
    void closeBus() noexcept;

    /// @brief Runs @p work on the bus's core and waits for it: in place when
    /// called there, through `esp_ipc_call_blocking()` from the other core.
    ///
    /// What moves the bus to other pads runs through it (pin_apply.h), because
    /// the own master serves the core its begin() ran on.
    /// @param[in]     work    What to run.
    /// @param[in,out] context Handed to @p work.
    /// @return `ESP_OK` once @p work has run, or the error of the IPC call the
    ///         bus's core refused, with @p work not run.
    esp_err_t runOnBusCore( void ( *work )( void* ), void* context ) noexcept;

    /// @brief Probes every address the A0/A1 straps can select ([DS] Table 7-2)
    /// and writes those that acknowledged into @p found.
    ///
    /// A cold joint on a strap shows up as a wandering address, so the
    /// instrument scans rather than trusting the photograph.
    ///
    /// Each probe is made on the bus's core, as every transaction is (the
    /// file's comment; the scan was the first, in 0.16.4, when a probe of a
    /// silent address from the far core left its STOP's completion latched in
    /// ESP-IDF's driver, `docs/architecture.md` SS 2.2).
    ///
    /// @param[out] found    Receives the addresses that acknowledged, lowest
    ///                      first.
    /// @param[in]  capacity Entries @p found can hold; answers past it are not
    ///                      stored or counted.
    /// @return how many addresses answered (at most @p capacity); 0 if the bus
    ///         is not open, @p found is null, or the bus's core refused the
    ///         calls (its error is then lastError()).
    std::size_t scan( std::uint8_t* found, std::size_t capacity ) noexcept;

    /// The pinned address that asks bringUp() to scan instead.
    static constexpr std::uint8_t kScanForAddress = 0U;

    /// @brief Resets, identifies and configures the device, then leaves it in
    /// the known state the design specifies: `SHUNT_CAL = 4096`,
    /// `CONFIG = 0x0000` (ADCRANGE = 0, TEMPCOMP = 0, CONVDLY = 0).
    ///
    /// Requires openBus() to have succeeded. On anything but
    /// BringUpResult::Ok the device is left alone and ready() stays false.
    ///
    /// With @p pinned an address, that address alone is asked for, and
    /// address() is it whether or not anything answered; with kScanForAddress
    /// the whole strap space is probed and the lowest answer taken.
    ///
    /// @param[in] pinned The pin map's address, or kScanForAddress.
    /// @return BringUpResult::Ok, or the first failure the sequence met
    ///         (BringUpResult::BusError when the bus is not open).
    BringUpResult bringUp( std::uint8_t pinned ) noexcept;

    /// @brief Sets the bus clock to @p hz, on the bus's core.
    ///
    /// The INA228 does not clock-stretch ([DS] Figure 7-8 notes), so the only
    /// cost of a wrong speed is a bus that does not work -- which is why this
    /// is settable at runtime instead of being a reflash. The own master
    /// rewrites its timing registers (i2c_bus::setClock()): nothing is freed
    /// or allocated (FW-14; until then ESP-IDF's driver removed the device
    /// handle and allocated it again, `docs/misra.md`).
    ///
    /// @param[in] hz New bus clock in Hz, kMinBusSpeedHz to kMaxBusSpeedHz.
    /// @return `ESP_OK`; `ESP_ERR_INVALID_STATE` if the bus is not open, or
    ///         the error of an IPC call the bus's core refused; or
    ///         i2c_bus::setClock()'s error (`ESP_ERR_INVALID_ARG` out of
    ///         range). On any error the clock is left as it was.
    esp_err_t setBusSpeed( std::uint32_t hz ) noexcept;

    /// @brief Whether bring-up succeeded.
    /// @return true once bringUp() has returned BringUpResult::Ok.
    bool ready() const noexcept { return ready_; }

    /// @brief The I2C address every register transaction is made to.
    /// @return The address the scan selected or bringUp() was given,
    ///         ina228::kAddressDefault before either.
    std::uint8_t address() const noexcept { return address_; }
    /// @brief MANUFACTURER_ID as read at bring-up.
    /// @return The raw register value, 0 before bring-up read it.
    std::uint16_t manufacturerId() const noexcept { return manufacturerId_; }
    /// @brief DIEID, DEVICE_ID bits 15-4, as read at bring-up.
    /// @return The die identifier, 0 before bring-up read it.
    std::uint16_t dieId() const noexcept { return dieId_; }
    /// @brief REV_ID, DEVICE_ID bits 3-0, as read at bring-up.
    /// @return The silicon revision, 0 before bring-up read it.
    std::uint8_t revisionId() const noexcept { return revisionId_; }
    /// @brief The bus clock.
    /// @return The SCL frequency in Hz, 0 before openBus().
    std::uint32_t busSpeedHz() const noexcept { return sclHz_; }
    /// @brief The ADC_CONFIG word last read at bring-up or written.
    /// @return The ADC_CONFIG word, 0 before bring-up.
    std::uint16_t adcConfig() const noexcept { return adcConfig_; }
    /// @brief The CONFIG word last written by setRange().
    /// @return The CONFIG word, 0 before any write.
    std::uint16_t config() const noexcept { return config_; }
    /// @brief SHUNT_CAL as read and then programmed at bring-up.
    /// @return The SHUNT_CAL value, 0 before bring-up.
    std::uint16_t shuntCal() const noexcept { return shuntCal_; }
    /// @brief The shunt range ADCRANGE is set to.
    /// @return The current range, ina228::ShuntRange::Wide163mV by default.
    ina228::ShuntRange range() const noexcept { return range_; }

    /// @brief The last DIAG_ALRT configuration word written.
    /// @return The word, 0 before any.
    std::uint16_t diagConfig() const noexcept { return diagConfig_; }
    /// @brief The last `SOVL` written.
    /// @return The limit word, its reset value before any.
    std::uint16_t shuntOverLimit() const noexcept { return shuntOverLimit_; }
    /// @brief The last `SUVL` written.
    /// @return The limit word, its reset value before any.
    std::uint16_t shuntUnderLimit() const noexcept { return shuntUnderLimit_; }

    /// @brief The `esp_err_t` of the last failed transaction, for the error
    /// text `i2c <name>`.
    /// @return The last error, `ESP_OK` if none has failed.
    esp_err_t lastError() const noexcept { return lastError_; }

    // --- Register access ----------------------------------------------------

    // Every transaction below is made on the bus's core (the file's comment).
    // Asked from the other core, it waits for that core's IPC task, which
    // runs at the caller's priority (`CONFIG_ESP_IPC_USES_CALLERS_PRIORITY`):
    // the sampler's loop, at a higher one, is never preempted by it, and a
    // transaction asked while a capture spins waits for the capture's end,
    // which is why every command that makes one is refused while a window
    // runs. "The bus's error" is i2c_bus.h's: `ESP_ERR_INVALID_RESPONSE`
    // for a NACK, `ESP_ERR_TIMEOUT` for a time-out or a stuck bus (recovered
    // before it returns), and `ESP_ERR_INVALID_STATE` for a call refused,
    // whether by the bus or as an IPC call the bus's core refused, with no
    // transaction made.

    /// @brief Writes a 16-bit register: the pointer, then the data MSB first.
    /// @param[in] reg   Register address.
    /// @param[in] value Word to write.
    /// @return `ESP_OK`; `ESP_ERR_INVALID_STATE` before the bring-up selected an address; or
    ///         the bus's error.
    esp_err_t write16( std::uint8_t reg, std::uint16_t value ) noexcept;
    /// @brief Reads a 16-bit register.
    /// @param[in]  reg   Register address.
    /// @param[out] value Receives the word; left untouched on failure.
    /// @return As readRegister().
    esp_err_t read16( std::uint8_t reg, std::uint16_t& value ) noexcept;
    /// @brief Reads a 24-bit register.
    /// @param[in]  reg   Register address.
    /// @param[out] value Receives the 24 bits, right-aligned; left untouched
    ///                   on failure.
    /// @return As readRegister().
    esp_err_t read24( std::uint8_t reg, std::uint32_t& value ) noexcept;
    /// @brief Reads a 40-bit register.
    /// @param[in]  reg   Register address.
    /// @param[out] value Receives the 40 bits, right-aligned.
    /// @return As readRegister().
    esp_err_t read40( std::uint8_t reg, std::uint64_t& value ) noexcept;

    /// @brief Reads @p width bytes (1..5) of @p reg, MSB first, into @p value.
    ///
    /// Backs the read-only `DIAGnostic:REGister?` query.
    ///
    /// @param[in]  reg   Register address.
    /// @param[in]  width Bytes to read, 1..5.
    /// @param[out] value Receives the bytes, right-aligned; 0 on failure.
    /// @return `ESP_OK`; `ESP_ERR_INVALID_STATE` before the bring-up selected an address;
    ///         `ESP_ERR_INVALID_ARG` for a width outside 1..5; or the I2C
    ///         driver's error.
    esp_err_t readRegister( std::uint8_t reg, std::size_t width, std::uint64_t& value ) noexcept;

    /// @brief Points the device at @p reg once, for a run of repeated reads.
    ///
    /// "If repeated reads from the same register are desired, it is not
    /// necessary to continually send the register pointer bytes; the device
    /// retains the register pointer value until it is changed by the next write
    /// operation" ([DS] SS 7.5.1.1). This is what makes the 280 us TRACE
    /// cadence affordable: 95 us of bus time per sample instead of 143 us.
    ///
    /// @param[in] reg Register address to leave the pointer at.
    /// @return `ESP_OK`; `ESP_ERR_INVALID_STATE` before the bring-up selected an address; or
    ///         the bus's error.
    esp_err_t setPointer( std::uint8_t reg ) noexcept;

    /// @brief Reads @p length bytes from the retained pointer. See setPointer().
    /// @param[out] buffer Receives the bytes, MSB first.
    /// @param[in]  length Bytes to read.
    /// @return `ESP_OK`; `ESP_ERR_INVALID_STATE` before the bring-up selected an address or
    ///         with a null @p buffer; or the bus's error.
    esp_err_t readRetained( std::uint8_t* buffer, std::size_t length ) noexcept;

    // --- Configuration ------------------------------------------------------

    /// @brief Writes ADC_CONFIG.
    ///
    /// [DS] SS 7.3.4: this "will interrupt and restart triggered or continuous
    /// conversions that are in progress", which is why every caller is outside
    /// a running capture.
    ///
    /// @param[in] word ADC_CONFIG word; adcConfig() reports it on success.
    /// @return As write16().
    esp_err_t applyAdcConfig( std::uint16_t word ) noexcept;

    /// @brief Sets ADCRANGE, leaving TEMPCOMP and CONVDLY at 0.
    ///
    /// **A real change also clears the accumulators** (`RSTACC`), because
    /// `ENERGY` and `CHARGE` count in units of `CURRENT_LSB` and would
    /// otherwise hold counts accumulated under two different scales in one
    /// number (`docs/ina228-reference.md` SS 8.2 item 4). The
    /// discarded conversion period the same section asks for belongs to the
    /// caller, which knows the live profile's period; the range commands pay
    /// it.
    ///
    /// @param[in] range Shunt range to select.
    /// @return `ESP_OK`, or the error of the first write that failed.
    esp_err_t setRange( ina228::ShuntRange range ) noexcept;

    /// @brief Reads DIAG_ALRT.
    ///
    /// Note that this clears CNVRF ([DS] SS 7.3.4), so a conversion-ready poll
    /// and a flag read are the same transaction.
    ///
    /// @param[out] value Receives the DIAG_ALRT word; untouched on failure.
    /// @return As readRegister().
    esp_err_t readDiagAlrt( std::uint16_t& value ) noexcept;

    /// @brief Configures DIAG_ALRT with ina228::kDiagAlrtConversionReady.
    ///
    /// `CNVR = 1` for conversion-ready on the ALERT pin, `ALATCH = 1` so the
    /// clip detector's flag holds until read, `APOL = 0` (active-low,
    /// open-drain).
    ///
    /// @return As write16().
    esp_err_t enableConversionReadyAlert() noexcept;

    /// @brief Writes `SOVL` and `SUVL` as the clip detector
    /// (ina228::kShuntOverLimitClip, ina228::kShuntUnderLimitClip).
    ///
    /// They reset with the device, so every capture and null window writes them
    /// again.
    ///
    /// @return `ESP_OK`, or the error of the first write that failed (`SOVL`
    ///         is written first).
    esp_err_t armShuntClipLimits() noexcept;

    /// @brief Clears ENERGY and CHARGE through CONFIG RSTACC, then writes
    /// CONFIG back without the bit.
    ///
    /// [DS] Table 7-5 calls RST (bit 15) self-clearing and says no such thing
    /// about RSTACC (bit 14). Leaving an undocumented bit set would hold the
    /// accumulators at zero and make the one open question `CHARGE?` exists to
    /// answer -- whether CHARGE accumulates at 10 uA -- unanswerable. So the
    /// firmware clears it explicitly rather than depending on a behavior the
    /// datasheet does not state.
    ///
    /// @return `ESP_OK`, or the error of the first write that failed.
    esp_err_t resetAccumulators() noexcept;

    /// @brief Takes one triggered temperature conversion and restores
    /// @p restoreWord.
    ///
    /// TRACE cannot afford VTCT inside its 280 us period (50 us is +18 %), so
    /// it gets its die temperature at the capture boundaries instead, with
    /// `MODE = 4h` ([DS] SS 7.6.1.2) -- outside the capture, never inside it
    /// (`docs/architecture.md` SS 3.2).
    ///
    /// @param[in]  restoreWord ADC_CONFIG word written back afterwards, whatever
    ///                         happened.
    /// @param[out] raw         Receives the raw DIETEMP count; 0 unless the
    ///                         conversion completed and was read.
    /// @return `ESP_OK`; `ESP_ERR_TIMEOUT` if CNVRF did not set within the poll
    ///         budget; the first I2C error met; or, when all else succeeded,
    ///         the error of restoring @p restoreWord.
    esp_err_t triggeredDieTemperature( std::uint16_t restoreWord, std::int16_t& raw ) noexcept;

   private:
    /// The calls into the own I2C master the driver makes, each one function
    /// of i2c_bus.h.
    enum class BusCall : std::uint8_t {
        Probe,            ///< i2c_bus::probe() of BusRequest::address.
        Transmit,         ///< i2c_bus::write() of the out bytes to address().
        Receive,          ///< i2c_bus::read() into the in bytes from address().
        TransmitReceive,  ///< i2c_bus::writeRead(): out, then in.
        SetClock,         ///< i2c_bus::setClock() to BusRequest::hz.
    };

    /// One call, as onBusCore() hands it to the bus's core.
    struct BusRequest {
        Ina228Driver*       driver    = nullptr;                ///< The driver making it.
        BusCall             call      = BusCall::Probe;         ///< Which call.
        std::uint8_t        address   = 0;                      ///< Probe.
        std::uint32_t       hz        = 0;                      ///< SetClock.
        const std::uint8_t* out       = nullptr;                ///< Bytes to send.
        std::size_t         outLength = 0;                      ///< How many.
        std::uint8_t*       in        = nullptr;                ///< Receives the bytes read.
        std::size_t         inLength  = 0;                      ///< How many.
        esp_err_t           result    = ESP_ERR_INVALID_STATE;  ///< The call's own.
    };

    /// @brief Whether the calling task runs on the bus's core.
    /// @return true there, and before openBus() on Core 0 (where the bus,
    ///         not yet begun, refuses the call).
    bool onBusCoreNow() const noexcept;

    /// @brief Makes @p request on the bus's core: in place when called there,
    /// through `esp_ipc_call_blocking()` from the other core.
    ///
    /// Out of IRAM: the sampler, on the bus's core, never calls it, because
    /// each IRAM transaction makes its call in place there first.
    ///
    /// @param[in,out] request The call; its @c result receives the call's
    ///                        own error.
    /// @return The call's own result; or, with no call made, the error of an
    ///         IPC call the bus's core refused (noted for lastError()).
    esp_err_t onBusCore( BusRequest& request ) noexcept;

    /// @brief Makes the call @p context names, on the core it runs on: the
    /// body of onBusCore(), and its IPC callback.
    /// @param[in,out] context The BusRequest to make and answer.
    static void makeCall( void* context ) noexcept;

    /// @brief Probes every strap address, each on the bus's core.
    /// @param[out] found    Receives the addresses that acknowledged.
    /// @param[in]  capacity Entries @p found can hold.
    /// @return how many answered, at most @p capacity.
    std::size_t probeAll( std::uint8_t* found, std::size_t capacity ) noexcept;

    /// @brief Selects the part's address: the lowest answer of the strap
    /// space's scan, or @p pinned when it answers.
    /// @param[in] pinned The pinned address, or kScanForAddress.
    /// @retval true  address_ is an address that acknowledged a probe.
    /// @retval false None did.
    bool selectAddress( std::uint8_t pinned ) noexcept;

    /// @brief Probes one address on the bus's core.
    /// @param[in] address The 7-bit address.
    /// @return true when it acknowledged.
    bool answers( std::uint8_t address ) noexcept;

    /// @brief A device transfer asked off the bus's core, made on it.
    /// @param[in]  call      BusCall::Transmit, Receive or TransmitReceive.
    /// @param[in]  out       Bytes to send, or null.
    /// @param[in]  outLength How many.
    /// @param[out] in        Receives the bytes read, or null.
    /// @param[in]  inLength  How many.
    /// @return As onBusCore().
    esp_err_t transferOnBusCore( BusCall call, const std::uint8_t* out, std::size_t outLength,
                                 std::uint8_t* in, std::size_t inLength ) noexcept;

    /// @brief Keeps @p err as lastError() when it is a failure.
    /// @param[in] err A transaction's result.
    /// @return @p err, for the caller to return.
    esp_err_t note( esp_err_t err ) noexcept;

    bool open_     = false;  ///< openBus() set the bus up.
    bool selected_ = false;  ///< The bring-up's scan selected address_.

    std::int32_t  busCore_         = 0;  ///< The core openBus() ran on, which owns the bus.
    std::uint8_t  address_         = ina228::kAddressDefault;  ///< The 7-bit address.
    std::uint32_t sclHz_           = 0;                        ///< The bus clock, Hz.
    std::uint16_t manufacturerId_  = 0;                        ///< MANUFACTURER_ID as read.
    std::uint16_t dieId_           = 0;                        ///< DEVICE_ID's die field.
    std::uint8_t  revisionId_      = 0;                        ///< DEVICE_ID's revision field.
    std::uint16_t adcConfig_       = 0;                        ///< ADC_CONFIG as last written.
    std::uint16_t config_          = 0;                        ///< CONFIG as last written.
    std::uint16_t shuntCal_        = 0;                        ///< SHUNT_CAL as last written.
    std::uint16_t diagConfig_      = 0;                        ///< DIAG_ALRT's configuration bits.
    std::uint16_t shuntOverLimit_  = ina228::kShuntOverLimitReset;   ///< SOVL as last written.
    std::uint16_t shuntUnderLimit_ = ina228::kShuntUnderLimitReset;  ///< SUVL as last written.
    esp_err_t     lastError_       = ESP_OK;  ///< The last failed transaction's.
    bool          ready_           = false;   ///< The bring-up passed.

    ina228::ShuntRange range_ = ina228::ShuntRange::Wide163mV;  ///< The live ADCRANGE.
};

/// @}

}  // namespace kleidos::meter

#endif  // KLEIDOS_METER_INA228_H
