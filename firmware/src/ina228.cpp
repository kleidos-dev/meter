// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file ina228.cpp
 * @ingroup ina228_driver
 * @brief Bring-up, identity and register access for the TI INA228.
 */
#include "ina228.h"

#include "bits.h"
#include "i2c_bus.h"
#include "rtos_ticks.h"

#include <esp_attr.h>
#include <esp_ipc.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <array>

namespace kleidos::meter {

namespace {

/// Wait after a software reset. [DS] SS 6.5 `TPOR` gives 300 us for a power-up
/// start and [DS] SS 7.3.6 adds ~300 us for the oscillator to settle below 1 %;
/// one millisecond clears both with room to spare.
constexpr std::uint32_t kResetSettleMs = 1;

/// Poll budget for one triggered temperature conversion. The longest VTCT is
/// 4120 us ([DS] SS 7.6.1.2) and [DS] SS 8.2.3 suggests allowing 1.5x the
/// conversion time for the flag, so ten milliseconds is generous.
constexpr std::uint32_t kTriggeredConversionTimeoutMs = 10;

/// Widest register the device defines, in bytes ([DS] SS 7.6.1.10/11).
constexpr std::size_t kMaxRegisterBytes = 5;

}  // namespace

const char* bringUpReason( BringUpResult result ) noexcept {
    switch ( result ) {
        case BringUpResult::Ok:
            return "ok";
        case BringUpResult::NoDevice:
            return "no device (scan found none)";
        case BringUpResult::NotIna228:
            return "not an ina228";
        case BringUpResult::BadResetDefaults:
            return "bad reset defaults";
        case BringUpResult::TrimChecksum:
            return "trim memory checksum (MEMSTAT=0)";
        case BringUpResult::BusError:
            return "i2c transaction failed";
    }
    return "unknown";
}

IRAM_ATTR esp_err_t Ina228Driver::note( esp_err_t err ) noexcept {
    if ( err != ESP_OK ) {
        lastError_ = err;
    }
    return err;
}

esp_err_t Ina228Driver::openBus( gpio_num_t sda, gpio_num_t scl, std::uint32_t sclHz,
                                 i2c_bus::Pulls pulls ) noexcept {
    if ( open_ ) {
        return ESP_ERR_INVALID_STATE;
    }
    // A pin below zero (GPIO_NUM_NC) wraps far past the last GPIO, which
    // i2c_bus::begin() refuses like any pin it does not have.
    const esp_err_t err = note( i2c_bus::begin( static_cast<std::uint32_t>( sda ),
                                                static_cast<std::uint32_t>( scl ), sclHz, pulls ) );
    if ( err != ESP_OK ) {
        return err;
    }
    open_    = true;
    sclHz_   = sclHz;
    busCore_ = static_cast<std::int32_t>( xPortGetCoreID() );
    return ESP_OK;
}

void Ina228Driver::closeBus() noexcept {
    open_           = false;
    selected_       = false;
    ready_          = false;
    address_        = ina228::kAddressDefault;
    sclHz_          = 0;
    manufacturerId_ = 0;
    dieId_          = 0;
    revisionId_     = 0;
}

IRAM_ATTR bool Ina228Driver::onBusCoreNow() const noexcept {
    return static_cast<std::int32_t>( xPortGetCoreID() ) == busCore_;
}

esp_err_t Ina228Driver::onBusCore( BusRequest& request ) noexcept {
    request.driver         = this;
    const esp_err_t routed = note( runOnBusCore( &Ina228Driver::makeCall, &request ) );
    return ( routed != ESP_OK ) ? routed : request.result;
}

esp_err_t Ina228Driver::runOnBusCore( void ( *work )( void* ), void* context ) noexcept {
    if ( onBusCoreNow() ) {
        work( context );
        return ESP_OK;
    }
    // Waited for, and made at this task's priority on the bus's core: the
    // sampler, above it there, is never preempted by a console transaction.
    // ESP-IDF's IPC call takes static semaphores and allocates nothing; the
    // own master polls its transaction to the end on that core (i2c_bus.h).
    return esp_ipc_call_blocking( static_cast<std::uint32_t>( busCore_ ), work, context );
}

void Ina228Driver::makeCall( void* context ) noexcept {
    auto&               request = *static_cast<BusRequest*>( context );
    const Ina228Driver& driver  = *request.driver;
    if ( request.call == BusCall::Probe ) {
        request.result = i2c_bus::probe( request.address );
    } else if ( request.call == BusCall::Transmit ) {
        request.result = i2c_bus::write( driver.address_, request.out, request.outLength );
    } else if ( request.call == BusCall::Receive ) {
        request.result = i2c_bus::read( driver.address_, request.in, request.inLength );
    } else if ( request.call == BusCall::TransmitReceive ) {
        request.result = i2c_bus::writeRead( driver.address_, request.out, request.outLength,
                                             request.in, request.inLength );
    } else {
        request.result = i2c_bus::setClock( request.hz );  // SetClock, the fifth and last
    }
}

// Never inlined into the IRAM transactions that call it: their in-place call
// is the hot path, this one the console's, and it stays in flash
// (scripts/check_iram_hot_path.py lists it cold).
[[gnu::noinline]] esp_err_t Ina228Driver::transferOnBusCore( BusCall call, const std::uint8_t* out,
                                                             std::size_t   outLength,
                                                             std::uint8_t* in,
                                                             std::size_t   inLength ) noexcept {
    BusRequest request;
    request.call      = call;
    request.out       = out;
    request.outLength = outLength;
    request.in        = in;
    request.inLength  = inLength;
    return onBusCore( request );
}

std::size_t Ina228Driver::scan( std::uint8_t* found, std::size_t capacity ) noexcept {
    if ( !open_ || found == nullptr ) {
        return 0;
    }
    return probeAll( found, capacity );
}

std::size_t Ina228Driver::probeAll( std::uint8_t* found, std::size_t capacity ) noexcept {
    // Each on the bus's core, which the own master requires; a probe that
    // meets no device returns once the bus is idle again, its statuses
    // cleared (i2c_bus.h).
    std::size_t count = 0;
    for ( std::uint32_t address = ina228::kAddressFirst; address <= ina228::kAddressLast;
          ++address ) {
        if ( answers( static_cast<std::uint8_t>( address ) ) && count < capacity ) {
            found[count++] = static_cast<std::uint8_t>( address );
        }
    }
    return count;
}

bool Ina228Driver::answers( std::uint8_t address ) noexcept {
    BusRequest request;
    request.call    = BusCall::Probe;
    request.address = address;
    return onBusCore( request ) == ESP_OK;
}

bool Ina228Driver::selectAddress( std::uint8_t pinned ) noexcept {
    if ( std::uint32_t{ pinned } != std::uint32_t{ kScanForAddress } ) {
        address_ = pinned;
        return answers( pinned );
    }
    // The whole strap address space ([DS] Table 7-2), and what answered,
    // rather than trusting the soldered jumpers read off a photograph.
    std::array<std::uint8_t, ina228::kAddressCount> found = {};
    if ( scan( found.data(), found.size() ) == 0U ) {
        return false;
    }
    address_ = found[0];
    return true;
}

esp_err_t Ina228Driver::setBusSpeed( std::uint32_t hz ) noexcept {
    if ( !open_ ) {
        return ESP_ERR_INVALID_STATE;
    }
    // A rewrite of the timing registers on the bus's core: nothing is freed
    // or allocated (i2c_bus.h, FW-14).
    BusRequest request;
    request.call        = BusCall::SetClock;
    request.hz          = hz;
    const esp_err_t err = note( onBusCore( request ) );
    if ( err == ESP_OK ) {
        sclHz_ = hz;
    }
    return err;
}

BringUpResult Ina228Driver::bringUp( std::uint8_t pinned ) noexcept {
    ready_ = false;
    if ( !open_ ) {
        return BringUpResult::BusError;
    }

    // Step 1: the address, scanned for or the one the pin map pins.
    if ( !selectAddress( pinned ) ) {
        return BringUpResult::NoDevice;
    }
    selected_ = true;

    // Step 2: software reset, then wait out TPOR.
    if ( write16( ina228::kRegConfig, ina228::kConfigRst ) != ESP_OK ) {
        return BringUpResult::BusError;
    }
    vTaskDelay( rtos::ticksFromMs( kResetSettleMs ) );
    // The reset puts DIAG_ALRT, SOVL and SUVL back to their reset values
    // ([DS] SS 7.6): what the driver reports of them follows.
    diagConfig_      = 0;
    shuntOverLimit_  = ina228::kShuntOverLimitReset;
    shuntUnderLimit_ = ina228::kShuntUnderLimitReset;

    // Step 3: identity. REV_ID is masked off, because it is a silicon revision
    // field and a future part may ship a different value.
    std::uint16_t deviceId = 0;
    esp_err_t     status   = read16( ina228::kRegManufacturerId, manufacturerId_ );
    if ( status == ESP_OK ) {
        status = read16( ina228::kRegDeviceId, deviceId );
    }
    if ( status != ESP_OK ) {
        return BringUpResult::BusError;
    }
    dieId_ = static_cast<std::uint16_t>(
        bits::field( deviceId, ina228::kDieIdMask, ina228::kDieIdShift ) );
    revisionId_ = static_cast<std::uint8_t>( deviceId & ina228::kRevIdMask );
    if ( manufacturerId_ != ina228::kManufacturerId || dieId_ != ina228::kDieId ) {
        return BringUpResult::NotIna228;
    }

    // Step 4: the documented reset values. This is a byte-order self-check as
    // much as an identity check -- getting 0xFB68 back proves MSB-first framing
    // ([DS] SS 7.5.1.1).
    status = read16( ina228::kRegAdcConfig, adcConfig_ );
    if ( status == ESP_OK ) {
        status = read16( ina228::kRegShuntCal, shuntCal_ );
    }
    if ( status != ESP_OK ) {
        return BringUpResult::BusError;
    }
    if ( static_cast<std::uint32_t>( adcConfig_ ) != ina228::kAdcConfigReset
         || static_cast<std::uint32_t>( shuntCal_ ) != ina228::kShuntCalValue ) {
        return BringUpResult::BadResetDefaults;
    }

    // Step 5: a hard stop, not a warning. MEMSTAT = 0 means "a checksum error
    // is detected in the device trim memory space" ([DS] Table 7-16): the
    // calibration constants are suspect and every number derived from them
    // would be suspect with them.
    std::uint16_t diag = 0;
    if ( readDiagAlrt( diag ) != ESP_OK ) {
        return BringUpResult::BusError;
    }
    if ( !bits::hasBit( diag, ina228::kDiagMemStat ) ) {
        return BringUpResult::TrimChecksum;
    }

    // Steps 6 and 7: program SHUNT_CAL explicitly -- required at every VS
    // power-up ([DS] SS 8.2.2.3), and the one register whose omission silently
    // reports current, power, energy and charge as zero ([DS] SS 7.3.2) -- then
    // put CONFIG in the state the design specifies.
    if ( write16( ina228::kRegShuntCal, ina228::kShuntCalValue ) != ESP_OK ) {
        return BringUpResult::BusError;
    }
    shuntCal_ = ina228::kShuntCalValue;
    if ( setRange( ina228::ShuntRange::Wide163mV ) != ESP_OK ) {
        return BringUpResult::BusError;
    }

    ready_ = true;
    return BringUpResult::Ok;
}

esp_err_t Ina228Driver::write16( std::uint8_t reg, std::uint16_t value ) noexcept {
    if ( !selected_ ) {
        return ESP_ERR_INVALID_STATE;
    }
    // Every write carries the pointer, then the data MSB first ([DS] SS 7.5.1.1).
    const std::array<std::uint8_t, 3> frame = {
        reg, static_cast<std::uint8_t>( bits::field( value, 0xFF00U, 8U ) ),
        static_cast<std::uint8_t>( value & 0xFFU ) };
    return note( transferOnBusCore( BusCall::Transmit, frame.data(), frame.size(), nullptr, 0 ) );
}

// The register reads the sampler makes every sample, with note() and the
// TRACE pointer write, are in IRAM (0.16.1, `docs/architecture.md`
// SS 5.2); the rest of the driver runs at bring-up or between captures.
// Each makes its call in place on the bus's core, the sampler's, and leaves
// it to transferOnBusCore() from the other one (0.16.5).
IRAM_ATTR esp_err_t Ina228Driver::readRegister( std::uint8_t reg, std::size_t width,
                                                std::uint64_t& value ) noexcept {
    value = 0;
    if ( !selected_ ) {
        return ESP_ERR_INVALID_STATE;
    }
    if ( width == 0U || width > kMaxRegisterBytes ) {
        return ESP_ERR_INVALID_ARG;
    }
    std::array<std::uint8_t, kMaxRegisterBytes> buffer = {};
    const esp_err_t                             err =
        note( onBusCoreNow()
                  ? i2c_bus::writeRead( address_, &reg, 1, buffer.data(), width )
                  : transferOnBusCore( BusCall::TransmitReceive, &reg, 1, buffer.data(), width ) );
    if ( err != ESP_OK ) {
        return err;
    }
    for ( std::size_t i = 0; i < width; ++i ) {
        value = ( value << 8 ) | buffer[i];
    }
    return ESP_OK;
}

IRAM_ATTR esp_err_t Ina228Driver::read16( std::uint8_t reg, std::uint16_t& value ) noexcept {
    std::uint64_t   raw = 0;
    const esp_err_t err = readRegister( reg, ina228::kWidth16Bytes, raw );
    if ( err == ESP_OK ) {
        value = static_cast<std::uint16_t>( raw );
    }
    return err;
}

IRAM_ATTR esp_err_t Ina228Driver::read24( std::uint8_t reg, std::uint32_t& value ) noexcept {
    std::uint64_t   raw = 0;
    const esp_err_t err = readRegister( reg, ina228::kWidth24Bytes, raw );
    if ( err == ESP_OK ) {
        value = static_cast<std::uint32_t>( raw );
    }
    return err;
}

esp_err_t Ina228Driver::read40( std::uint8_t reg, std::uint64_t& value ) noexcept {
    return readRegister( reg, ina228::kWidth40Bytes, value );
}

IRAM_ATTR esp_err_t Ina228Driver::setPointer( std::uint8_t reg ) noexcept {
    if ( !selected_ ) {
        return ESP_ERR_INVALID_STATE;
    }
    return note( onBusCoreNow() ? i2c_bus::write( address_, &reg, 1 )
                                : transferOnBusCore( BusCall::Transmit, &reg, 1, nullptr, 0 ) );
}

IRAM_ATTR esp_err_t Ina228Driver::readRetained( std::uint8_t* buffer,
                                                std::size_t   length ) noexcept {
    if ( !selected_ || buffer == nullptr ) {
        return ESP_ERR_INVALID_STATE;
    }
    return note( onBusCoreNow()
                     ? i2c_bus::read( address_, buffer, length )
                     : transferOnBusCore( BusCall::Receive, nullptr, 0, buffer, length ) );
}

esp_err_t Ina228Driver::applyAdcConfig( std::uint16_t word ) noexcept {
    const esp_err_t err = write16( ina228::kRegAdcConfig, word );
    if ( err == ESP_OK ) {
        adcConfig_ = word;
    }
    return err;
}

esp_err_t Ina228Driver::setRange( ina228::ShuntRange range ) noexcept {
    const bool          changed = range != range_;
    const std::uint16_t word =
        ( range == ina228::ShuntRange::Narrow41mV ) ? ina228::kConfigAdcRange : 0U;
    const esp_err_t err = write16( ina228::kRegConfig, word );
    if ( err != ESP_OK ) {
        return err;
    }
    config_ = word;
    range_  = range;

    // RSTACC on every switch, and here rather than at the call site so a future
    // caller inherits it. CHARGE counts are in units of CURRENT_LSB, which is
    // 3.125 uA in range 0 and 0.78125 uA in range 1, so without this one 40-bit
    // number would hold counts accumulated under two scales and `SENSe:CHARge?`
    // would convert all of them with whichever scale was in force when it read
    // (`docs/ina228-reference.md` SS 8.2 item 4; `docs/architecture.md`
    // SS 3.1).
    //
    // Only on a real change: bring-up calls this with the range the device
    // already reports after its reset, and zeroing accumulators that are
    // already zero would be two writes for nothing.
    if ( !changed ) {
        return ESP_OK;
    }
    return resetAccumulators();
}

IRAM_ATTR esp_err_t Ina228Driver::readDiagAlrt( std::uint16_t& value ) noexcept {
    return read16( ina228::kRegDiagAlrt, value );
}

esp_err_t Ina228Driver::enableConversionReadyAlert() noexcept {
    const esp_err_t err = write16( ina228::kRegDiagAlrt, ina228::kDiagAlrtConversionReady );
    if ( err == ESP_OK ) {
        diagConfig_ = ina228::kDiagAlrtConversionReady;
    }
    return err;
}

esp_err_t Ina228Driver::armShuntClipLimits() noexcept {
    esp_err_t err = write16( ina228::kRegShuntOvl, ina228::kShuntOverLimitClip );
    if ( err != ESP_OK ) {
        return err;
    }
    shuntOverLimit_ = ina228::kShuntOverLimitClip;
    err             = write16( ina228::kRegShuntUvl, ina228::kShuntUnderLimitClip );
    if ( err != ESP_OK ) {
        return err;
    }
    shuntUnderLimit_ = ina228::kShuntUnderLimitClip;
    return ESP_OK;
}

esp_err_t Ina228Driver::resetAccumulators() noexcept {
    const esp_err_t err = write16( ina228::kRegConfig,
                                   static_cast<std::uint16_t>( config_ | ina228::kConfigRstAcc ) );
    if ( err != ESP_OK ) {
        return err;
    }
    return write16( ina228::kRegConfig, config_ );
}

esp_err_t Ina228Driver::triggeredDieTemperature( std::uint16_t restoreWord,
                                                 std::int16_t& raw ) noexcept {
    raw = 0;
    // MODE = 4h, triggered temperature, with the shortest conversion time: the
    // quantity does not move during a capture boundary, and the wait is paid
    // once per capture.
    const std::uint16_t triggerWord =
        ina228::adcConfigWord( ina228::kModeTriggeredTemp, ina228::kCt50us, ina228::kCt50us,
                               ina228::kCt50us, ina228::kAvg1 );
    esp_err_t err = applyAdcConfig( triggerWord );
    if ( err != ESP_OK ) {
        return err;
    }

    // Writing ADC_CONFIG clears CNVRF ([DS] SS 7.3.4), so the flag seen below
    // belongs to the conversion this call started.
    bool          converted = false;
    std::uint16_t diag      = 0;
    for ( std::uint32_t waited = 0; waited < kTriggeredConversionTimeoutMs; ++waited ) {
        vTaskDelay( rtos::ticksFromMs( 1 ) );
        err = readDiagAlrt( diag );
        if ( err != ESP_OK ) {
            break;
        }
        if ( bits::hasBit( diag, ina228::kDiagCnvrf ) ) {
            converted = true;
            break;
        }
    }

    if ( err == ESP_OK && converted ) {
        std::uint16_t temperature = 0;
        err                       = read16( ina228::kRegDieTemp, temperature );
        if ( err == ESP_OK ) {
            raw = static_cast<std::int16_t>( temperature );
        }
    } else if ( err == ESP_OK ) {
        err = ESP_ERR_TIMEOUT;
    } else {
        // The read failed: err already says how.
    }

    // Restore the profile whatever happened: a capture must never run under the
    // trigger word.
    const esp_err_t restored = applyAdcConfig( restoreWord );
    return ( err != ESP_OK ) ? err : restored;
}

}  // namespace kleidos::meter
