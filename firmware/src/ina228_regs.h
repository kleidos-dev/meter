// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file ina228_regs.h
 * @ingroup ina228_driver
 * @brief TI INA228 register map, field encodings and measurement LSBs.
 *
 * Every address, field position, encoding and LSB in this file is quoted from
 * the TI INA228 datasheet, literature number **SLYS021A** (January 2021,
 * revised May 2022), <https://www.ti.com/lit/ds/symlink/ina228.pdf>, cited
 * below as **[DS]** with the section, table or equation number. Nothing here is
 * from memory: the reading that produced these values is recorded, claim by
 * claim, in `docs/ina228-reference.md`.
 *
 * The file is the device's own map and nothing else. What this instrument
 * chooses to do with it -- the four sampling profiles, the range decision, the
 * calibration policy -- lives in sampler.h and calibration.h.
 *
 * Scope note: only the registers the firmware uses are given behavior here. `ENERGY`
 * (`0x09`) and four of the six threshold registers (`0x0E`-`0x11`) are listed
 * for the register-width table that `DIAGnostic:REGister?` needs, and are
 * deliberately not used (`docs/architecture.md` SS 11). `SOVL` and `SUVL` (`0x0C`, `0x0D`)
 * are programmed as a clip detector and nothing else: see
 * kShuntOverLimitClip below.
 */
#ifndef KLEIDOS_METER_INA228_REGS_H
#define KLEIDOS_METER_INA228_REGS_H

#include "bits.h"

#include <array>
#include <cstddef>
#include <cstdint>

namespace kleidos::meter::ina228 {

/// @addtogroup ina228_driver
/// @{

// --- I2C addressing ---------------------------------------------------------

/// Lowest address the A0/A1 straps can select: `1000000b`, both pins at GND.
/// [DS] SS 7.5.1, Table 7-2.
constexpr std::uint8_t kAddressFirst = 0x40;

/// Highest address the straps can select: `1001111b`, both pins on SCL.
/// [DS] SS 7.5.1, Table 7-2.
constexpr std::uint8_t kAddressLast = 0x4F;

/// Addresses in the strap space, i.e. the whole range a boot scan walks.
constexpr std::size_t kAddressCount = static_cast<std::size_t>( kAddressLast ) - kAddressFirst + 1U;

/// The address this breakout's straps select. A photograph of the breakout
/// (not kept in this repository) shows a 2x4 strap field, rows
/// silkscreened `A0` / `A1` and columns `G . V . S D`, with the **leftmost pad
/// of each row carrying solder**: `A0 = GND`, `A1 = GND` -> `1000000b` by [DS]
/// Table 7-2, which is also the address TI annotates on its own application
/// diagram ([DS] Figure 8-2). The firmware does not trust it -- `begin()` scans
/// the whole strap space and reports what answered.
constexpr std::uint8_t kAddressDefault = 0x40;

// --- Register addresses ([DS] SS 7.6.1, Table 7-3) --------------------------
// Addresses not listed here are reserved and must not be written.

constexpr std::uint8_t kRegConfig = 0x00;  ///< CONFIG, 16-bit, reset `0000h`. [DS] SS 7.6.1.1.
constexpr std::uint8_t kRegAdcConfig =
    0x01;  ///< ADC_CONFIG, 16-bit, reset `FB68h`. [DS] SS 7.6.1.2.
constexpr std::uint8_t kRegShuntCal = 0x02;  ///< SHUNT_CAL, 16-bit, reset `1000h`. [DS] SS 7.6.1.3.
constexpr std::uint8_t kRegShuntTempco =
    0x03;  ///< SHUNT_TEMPCO, 16-bit, reset `0000h`. [DS] SS 7.6.1.4.
constexpr std::uint8_t kRegVShunt  = 0x04;  ///< VSHUNT, 24-bit, read-only. [DS] SS 7.6.1.5.
constexpr std::uint8_t kRegVBus    = 0x05;  ///< VBUS, 24-bit, read-only. [DS] SS 7.6.1.6.
constexpr std::uint8_t kRegDieTemp = 0x06;  ///< DIETEMP, 16-bit, read-only. [DS] SS 7.6.1.7.
constexpr std::uint8_t kRegCurrent = 0x07;  ///< CURRENT, 24-bit, read-only. [DS] SS 7.6.1.8.
constexpr std::uint8_t kRegPower =
    0x08;  ///< POWER, 24-bit, read-only, **unsigned, no reserved nibble**. [DS] SS 7.6.1.9.
constexpr std::uint8_t kRegEnergy =
    0x09;  ///< ENERGY, 40-bit, read-only, unsigned. Unused. [DS] SS 7.6.1.10.
constexpr std::uint8_t kRegCharge =
    0x0A;  ///< CHARGE, 40-bit, read-only, two's complement. [DS] SS 7.6.1.11.
constexpr std::uint8_t kRegDiagAlrt =
    0x0B;  ///< DIAG_ALRT, 16-bit, reset `0001h`. [DS] SS 7.6.1.12.
constexpr std::uint8_t kRegShuntOvl =
    0x0C;  ///< SOVL, 16-bit, reset `7FFFh`. The clip detector's top. [DS] SS 7.6.1.13.
constexpr std::uint8_t kRegShuntUvl =
    0x0D;  ///< SUVL, 16-bit, reset `8000h`. The clip detector's bottom. [DS] SS 7.6.1.14.
constexpr std::uint8_t kRegBusOvl =
    0x0E;  ///< BOVL, 16-bit, reset `7FFFh`. Unused. [DS] SS 7.6.1.15.
constexpr std::uint8_t kRegBusUvl =
    0x0F;  ///< BUVL, 16-bit, reset `0000h`. Unused. [DS] SS 7.6.1.16.
constexpr std::uint8_t kRegTempLimit =
    0x10;  ///< TEMP_LIMIT, 16-bit, reset `7FFFh`. Unused. [DS] SS 7.6.1.17.
constexpr std::uint8_t kRegPowerLimit =
    0x11;  ///< PWR_LIMIT, 16-bit, reset `FFFFh`. Unused. [DS] SS 7.6.1.18.
constexpr std::uint8_t kRegManufacturerId =
    0x3E;  ///< MANUFACTURER_ID, 16-bit. [DS] SS 7.6.1.19, Table 7-23.
constexpr std::uint8_t kRegDeviceId = 0x3F;  ///< DEVICE_ID, 16-bit. [DS] SS 7.6.1.20, Table 7-24.

// --- Register widths --------------------------------------------------------
// The read and write diagrams are drawn for 16-bit registers and "registers
// with a higher number of bytes will behave similarly" ([DS] SS 7.5.1.1):
// 16-bit -> 2 bytes, 24-bit -> 3, 40-bit -> 5, always MSB first.

constexpr std::size_t kWidth16Bytes = 2;  ///< Bytes on the wire for a 16-bit register.
constexpr std::size_t kWidth24Bytes = 3;  ///< Bytes on the wire for a 24-bit register.
constexpr std::size_t kWidth40Bytes = 5;  ///< Bytes on the wire for a 40-bit register.

/// @brief Width of @p reg in bytes, or 0 for a reserved address.
///
/// This is what `DIAGnostic:REGister?` uses to decide how many bytes to clock out, so a
/// reserved address is refused rather than read with a guessed width.
///
/// @param[in] reg Register address.
/// @return kWidth16Bytes, kWidth24Bytes or kWidth40Bytes for a listed register;
///         0 for a reserved address.
constexpr std::size_t registerWidthBytes( std::uint8_t reg ) noexcept {
    switch ( reg ) {
        case kRegVShunt:
        case kRegVBus:
        case kRegCurrent:
        case kRegPower:
            return kWidth24Bytes;
        case kRegEnergy:
        case kRegCharge:
            return kWidth40Bytes;
        case kRegConfig:
        case kRegAdcConfig:
        case kRegShuntCal:
        case kRegShuntTempco:
        case kRegDieTemp:
        case kRegDiagAlrt:
        case kRegShuntOvl:
        case kRegShuntUvl:
        case kRegBusOvl:
        case kRegBusUvl:
        case kRegTempLimit:
        case kRegPowerLimit:
        case kRegManufacturerId:
        case kRegDeviceId:
            return kWidth16Bytes;
        default:
            return 0;
    }
}

// --- CONFIG (`0x00`) fields ([DS] SS 7.6.1.1, Table 7-5) --------------------

constexpr std::uint32_t kConfigRst    = 1U << 15;  ///< Bit 15 RST: system reset, self-clearing.
constexpr std::uint32_t kConfigRstAcc = 1U
                                        << 14;  ///< Bit 14 RSTACC: clears ENERGY and CHARGE to 0.
constexpr std::uint32_t kConfigTempComp =
    1U << 5;  ///< Bit 5 TEMPCOMP: shunt temperature compensation from SHUNT_TEMPCO.
constexpr std::uint32_t kConfigAdcRange =
    1U << 4;  ///< Bit 4 ADCRANGE: 0 = +/-163.84 mV, 1 = +/-40.96 mV.

/// Position of bits 13-6 CONVDLY: delay before the initial conversion, 2 ms
/// per step. Left at 0 -- it exists to time-align several monitors
/// ([DS] SS 7.3.4), and there is one.
constexpr std::uint32_t kConfigConvDlyShift = 6;
constexpr std::uint32_t kConfigConvDlyMask  = 0x00FFU << kConfigConvDlyShift;  ///< CONVDLY mask.

// --- ADC_CONFIG (`0x01`) fields ([DS] SS 7.6.1.2, Table 7-6) ----------------

constexpr std::uint32_t kAdcModeShift   = 12;  ///< Bits 15-12 MODE.
constexpr std::uint32_t kAdcVBusCtShift = 9;   ///< Bits 11-9 VBUSCT, bus-voltage conversion time.
constexpr std::uint32_t kAdcVShCtShift  = 6;   ///< Bits 8-6 VSHCT, shunt-voltage conversion time.
constexpr std::uint32_t kAdcVtCtShift   = 3;   ///< Bits 5-3 VTCT, temperature conversion time.
constexpr std::uint32_t kAdcAvgShift    = 0;   ///< Bits 2-0 AVG, averaging count.

constexpr std::uint32_t kAdcModeMask   = 0xFU << kAdcModeShift;    ///< MODE mask, bits 15-12.
constexpr std::uint32_t kAdcVBusCtMask = 0x7U << kAdcVBusCtShift;  ///< VBUSCT mask, bits 11-9.
constexpr std::uint32_t kAdcVShCtMask  = 0x7U << kAdcVShCtShift;   ///< VSHCT mask, bits 8-6.
constexpr std::uint32_t kAdcVtCtMask   = 0x7U << kAdcVtCtShift;    ///< VTCT mask, bits 5-3.
constexpr std::uint32_t kAdcAvgMask    = 0x7U << kAdcAvgShift;     ///< AVG mask, bits 2-0.

/// MODE encodings this firmware uses, from the 16-entry table in [DS] SS 7.6.1.2.
/// The triggered modes are otherwise left alone: in triggered mode the
/// accumulators are invalid, "as the device does not keep track of elapsed
/// time" ([DS] SS 7.3.4).
constexpr std::uint8_t kModeShutdown        = 0x0;  ///< `0h` shutdown.
constexpr std::uint8_t kModeTriggeredTemp   = 0x4;  ///< `4h` triggered temperature, one conversion.
constexpr std::uint8_t kModeContinuousShunt = 0xA;  ///< `Ah` continuous shunt only.
constexpr std::uint8_t kModeContinuousShuntBus = 0xB;  ///< `Bh` continuous shunt + bus.
constexpr std::uint8_t kModeContinuousAll = 0xF;  ///< `Fh` continuous bus + shunt + temperature.

/// Conversion-time encodings, shared by VBUSCT, VSHCT and VTCT
/// ([DS] SS 7.6.1.2; also [DS] SS 6.5 `TCT`, "subject to oscillator accuracy
/// and drift").
/// `0h` is 50 us.
constexpr std::uint8_t kCt50us   = 0x0;
constexpr std::uint8_t kCt84us   = 0x1;  ///< `1h` 84 us.
constexpr std::uint8_t kCt150us  = 0x2;  ///< `2h` 150 us.
constexpr std::uint8_t kCt280us  = 0x3;  ///< `3h` 280 us.
constexpr std::uint8_t kCt540us  = 0x4;  ///< `4h` 540 us.
constexpr std::uint8_t kCt1052us = 0x5;  ///< `5h` 1052 us.
constexpr std::uint8_t kCt2074us = 0x6;  ///< `6h` 2074 us.
constexpr std::uint8_t kCt4120us = 0x7;  ///< `7h` 4120 us.

/// Microseconds per conversion-time code, indexed by the encodings above.
constexpr std::array<std::uint16_t, 8> kConversionTimeUs = { 50,  84,   150,  280,
                                                             540, 1052, 2074, 4120 };

/// Which inputs a MODE value converts.
///
/// Read straight off the 16-entry MODE table ([DS] SS 7.6.1.2): bit 3 selects
/// continuous over triggered, and the low three bits name the inputs
/// independently of it -- `9h`/`1h` bus, `Ah`/`2h` shunt, `Bh`/`3h` shunt+bus,
/// `Ch`/`4h` temperature, `Dh`/`5h` temperature+bus, `Eh`/`6h`
/// temperature+shunt, `Fh`/`7h` all three. The static_asserts below re-derive
/// the named modes from these bits rather than taking the decomposition on
/// trust. This one, bit 0, selects the bus voltage.
constexpr std::uint32_t kModeBusBit         = 1U << 0;
constexpr std::uint32_t kModeShuntBit       = 1U << 1;  ///< Bit 1: shunt voltage.
constexpr std::uint32_t kModeTemperatureBit = 1U << 2;  ///< Bit 2: temperature.
constexpr std::uint32_t kModeContinuousBit  = 1U << 3;  ///< Bit 3: continuous, not triggered.

static_assert( kModeContinuousShunt == ( kModeContinuousBit | kModeShuntBit ) );
static_assert( kModeContinuousShuntBus == ( kModeContinuousBit | kModeShuntBit | kModeBusBit ) );
static_assert( kModeContinuousAll
               == ( kModeContinuousBit | kModeShuntBit | kModeBusBit | kModeTemperatureBit ) );
static_assert( kModeTriggeredTemp == kModeTemperatureBit );

/// @brief Whether @p mode converts the bus voltage.
/// @param[in] mode A MODE encoding.
/// @retval true  kModeBusBit is set.
/// @retval false It is not.
constexpr bool modeConvertsBus( std::uint8_t mode ) noexcept {
    return bits::hasBit( mode, kModeBusBit );
}

/// @brief Whether @p mode converts the shunt voltage.
/// @param[in] mode A MODE encoding.
/// @retval true  kModeShuntBit is set.
/// @retval false It is not.
constexpr bool modeConvertsShunt( std::uint8_t mode ) noexcept {
    return bits::hasBit( mode, kModeShuntBit );
}

/// @brief Whether @p mode converts the die temperature.
/// @param[in] mode A MODE encoding.
/// @retval true  kModeTemperatureBit is set.
/// @retval false It is not.
constexpr bool modeConvertsTemperature( std::uint8_t mode ) noexcept {
    return bits::hasBit( mode, kModeTemperatureBit );
}

/// AVG encodings ([DS] SS 7.6.1.2). "The averaging setting applies to all
/// active inputs. When >0h, the output registers are updated after the
/// averaging has completed." `0h` is 1 sample, no averaging.
constexpr std::uint8_t kAvg1    = 0x0;
constexpr std::uint8_t kAvg4    = 0x1;  ///< `1h` 4 samples.
constexpr std::uint8_t kAvg16   = 0x2;  ///< `2h` 16 samples.
constexpr std::uint8_t kAvg64   = 0x3;  ///< `3h` 64 samples.
constexpr std::uint8_t kAvg128  = 0x4;  ///< `4h` 128 samples.
constexpr std::uint8_t kAvg256  = 0x5;  ///< `5h` 256 samples.
constexpr std::uint8_t kAvg512  = 0x6;  ///< `6h` 512 samples.
constexpr std::uint8_t kAvg1024 = 0x7;  ///< `7h` 1024 samples.

/// Samples averaged per output, indexed by the AVG encodings above.
constexpr std::array<std::uint16_t, 8> kAverageCount = { 1, 4, 16, 64, 128, 256, 512, 1024 };

/// @brief Composes an ADC_CONFIG word from its five fields.
///
/// Profile words are **derived** through this function and checked with a
/// `static_assert` against the value the design states, never copied as a
/// literal (`docs/architecture.md` SS 3.2). Each field is masked to its
/// width before it is shifted into place.
///
/// @param[in] mode   MODE encoding, 4 bits.
/// @param[in] vbusCt VBUSCT conversion-time code, 3 bits.
/// @param[in] vShCt  VSHCT conversion-time code, 3 bits.
/// @param[in] vtCt   VTCT conversion-time code, 3 bits.
/// @param[in] avg    AVG encoding, 3 bits.
/// @return The 16-bit ADC_CONFIG word.
constexpr std::uint16_t adcConfigWord( std::uint8_t mode, std::uint8_t vbusCt, std::uint8_t vShCt,
                                       std::uint8_t vtCt, std::uint8_t avg ) noexcept {
    return static_cast<std::uint16_t>(
        ( ( mode & 0xFU ) << kAdcModeShift ) | ( ( vbusCt & 0x7U ) << kAdcVBusCtShift )
        | ( ( vShCt & 0x7U ) << kAdcVShCtShift ) | ( ( vtCt & 0x7U ) << kAdcVtCtShift )
        | ( ( avg & 0x7U ) << kAdcAvgShift ) );
}

/// @brief The output update period, in microseconds, of an ADC_CONFIG field
/// set.
///
/// "Enabled measurement inputs are converted sequentially so the total time to
/// convert all inputs depends on the conversion time for each input and the
/// number of inputs enabled ... the conversion sequence repeats until the
/// number of averages is reached" ([DS] SS 7.3.4), i.e.
/// `( VBUSCT? + VSHCT? + VTCT? ) x AVG`. Confirmed against the "output sample
/// period" column of [DS] Table 8-2.
///
/// @param[in] mode   MODE encoding; only the inputs it converts count.
/// @param[in] vbusCt VBUSCT conversion-time code.
/// @param[in] vShCt  VSHCT conversion-time code.
/// @param[in] vtCt   VTCT conversion-time code.
/// @param[in] avg    AVG encoding.
/// @return The update period in microseconds.
constexpr std::uint32_t periodMicroseconds( std::uint8_t mode, std::uint8_t vbusCt,
                                            std::uint8_t vShCt, std::uint8_t vtCt,
                                            std::uint8_t avg ) noexcept {
    std::uint32_t sequence = 0;
    if ( modeConvertsBus( mode ) ) {
        sequence += kConversionTimeUs[vbusCt & 0x7U];
    }
    if ( modeConvertsShunt( mode ) ) {
        sequence += kConversionTimeUs[vShCt & 0x7U];
    }
    if ( modeConvertsTemperature( mode ) ) {
        sequence += kConversionTimeUs[vtCt & 0x7U];
    }
    return sequence * kAverageCount[avg & 0x7U];
}

/// @brief The digital filter's first notch, `1 / (2 x TCT)` ([DS] SS 7.3.4.1),
/// in hertz, for a shunt conversion time code.
///
/// @param[in] vShCt VSHCT conversion-time code.
/// @return The notch frequency in Hz, rounded down.
constexpr std::uint32_t shuntNotchHz( std::uint8_t vShCt ) noexcept {
    return 1000000U / ( 2U * kConversionTimeUs[vShCt & 0x7U] );
}

/// @brief Bit times one retained-pointer read of @p bytes costs on the wire.
///
/// The count covers the START and STOP, the address byte with its acknowledge,
/// and each data byte with its own. Leaving the pointer where it is halves this
/// -- a full read re-sends the register byte behind a second address
/// ([DS] SS 7.5.1.1).
///
/// @param[in] bytes Data bytes read.
/// @return Bit times on the bus.
constexpr std::uint32_t retainedReadBitTimes( std::uint32_t bytes ) noexcept {
    return 2U + 9U + 9U * bytes;
}

/// @brief Bit times one read of a register costs with its pointer written
/// first: a retained read, and before it the address and the register byte
/// and the repeated START ([DS] SS 7.5.1.1).
///
/// @param[in] bytes Data bytes read.
/// @return Bit times on the bus.
constexpr std::uint32_t registerReadBitTimes( std::uint32_t bytes ) noexcept {
    return retainedReadBitTimes( bytes ) + 9U + 9U + 1U;
}

/// @brief What @p bitTimes cost in microseconds at @p sclHz, rounded up.
///
/// @param[in] bitTimes Bit times on the bus.
/// @param[in] sclHz    Bus clock in Hz.
/// @return The time in microseconds, rounded up.
constexpr std::uint32_t busMicroseconds( std::uint32_t bitTimes, std::uint32_t sclHz ) noexcept {
    return ( bitTimes * 1000000U + sclHz - 1U ) / sclHz;
}

/// @brief What a retained read of @p bytes costs in microseconds at @p sclHz,
/// rounded up.
///
/// This is the floor a sampling period has to clear, because the ADC converts
/// faster than the bus carries the result away.
///
/// @param[in] bytes Data bytes read.
/// @param[in] sclHz Bus clock in Hz.
/// @return The read time in microseconds, rounded up.
constexpr std::uint32_t retainedReadMicroseconds( std::uint32_t bytes,
                                                  std::uint32_t sclHz ) noexcept {
    return busMicroseconds( retainedReadBitTimes( bytes ), sclHz );
}

/// The 95 us the design states for a 3-byte retained read at 400 kHz, which
/// the derivation above has to reproduce rather than be told.
static_assert( retainedReadMicroseconds( kWidth24Bytes, 400000 ) == 95U );
static_assert( retainedReadMicroseconds( kWidth24Bytes, 1000000 ) == 38U );

/// @brief Extracts a field from an ADC_CONFIG word.
/// @param[in] word  ADC_CONFIG word.
/// @param[in] mask  The field's mask, e.g. kAdcModeMask.
/// @param[in] shift The field's position, e.g. kAdcModeShift.
/// @return The field value, right-aligned.
constexpr std::uint8_t adcConfigField( std::uint16_t word, std::uint32_t mask,
                                       std::uint32_t shift ) noexcept {
    return static_cast<std::uint8_t>( bits::field( word, mask, shift ) );
}

/// @brief The conversion period an ADC_CONFIG word runs at, in microseconds.
///
/// The word the device holds, not a profile's: until a capture writes its
/// profile the device converts at the reset word's 3,156 us.
///
/// @param[in] word ADC_CONFIG word.
/// @return The time one result takes, averaging included.
constexpr std::uint32_t adcConfigPeriodMicroseconds( std::uint16_t word ) noexcept {
    return periodMicroseconds( adcConfigField( word, kAdcModeMask, kAdcModeShift ),
                               adcConfigField( word, kAdcVBusCtMask, kAdcVBusCtShift ),
                               adcConfigField( word, kAdcVShCtMask, kAdcVShCtShift ),
                               adcConfigField( word, kAdcVtCtMask, kAdcVtCtShift ),
                               adcConfigField( word, kAdcAvgMask, kAdcAvgShift ) );
}

// --- SHUNT_CAL (`0x02`) ([DS] SS 7.6.1.3, Table 7-7) ------------------------

/// Bit 15 is reserved, so SHUNT_CAL is 15 bits with a maximum of 32767.
constexpr std::uint16_t kShuntCalMax = 0x7FFF;

/// The calibration constant this rig uses, in **both** ADCRANGE settings.
///
/// By [DS] SS 8.1.2 Equation 2, `SHUNT_CAL = 13107.2e6 x CURRENT_LSB x R_SHUNT`,
/// multiplied by 4 for `ADCRANGE = 1`; with the R100 shunt and each range's own
/// full-scale current that is `13107.2e6 x 3.125e-6 x 0.1 = 4096` in range 0 and
/// `13107.2e6 x 0.78125e-6 x 0.1 x 4 = 4096` in range 1. The x4 exactly cancels
/// the 4x smaller `CURRENT_LSB`, so a range change is one bit in CONFIG plus a
/// host-side factor of four and nothing else. Both values are exact, so no gain
/// error enters through the calibration constant. It is also the device's reset
/// value, and it is programmed anyway because [DS] SS 8.2.2.3 requires it at
/// every VS power-up and [DS] SS 7.3.2 warns that a zero here silently reports
/// power, energy and charge as zero.
constexpr std::uint16_t kShuntCalValue = 0x1000;

// --- DIAG_ALRT (`0x0B`) bits ([DS] SS 7.6.1.12, Table 7-16) -----------------

constexpr std::uint32_t kDiagAlatchShift = 15U;  ///< ALATCH's bit.
constexpr std::uint32_t kDiagAlatch =
    1U << kDiagAlatchShift;                    ///< ALATCH: 0 transparent, 1 latched until read.
constexpr std::uint32_t kDiagCnvr = 1U << 14;  ///< CNVR: assert ALERT on conversion-ready.
constexpr std::uint32_t kDiagSlowAlert =
    1U << 13;  ///< SLOWALERT: compare the averaged value instead of the raw one.
constexpr std::uint32_t kDiagApol = 1U << 12;  ///< APOL: 0 active-low open-drain, 1 active-high.
constexpr std::uint32_t kDiagEnergyOf =
    1U << 11;  ///< ENERGYOF: ENERGY overflowed. Clears when ENERGY is read.
constexpr std::uint32_t kDiagChargeOf =
    1U << 10;  ///< CHARGEOF: CHARGE overflowed. **Clears when CHARGE is read.**
constexpr std::uint32_t kDiagMathOf =
    1U << 9;  ///< MATHOF: arithmetic overflow, "current and power data may be invalid".
constexpr std::uint32_t kDiagTmpOl  = 1U << 7;  ///< TMPOL: over-temperature versus TEMP_LIMIT.
constexpr std::uint32_t kDiagShntOl = 1U << 6;  ///< SHNTOL: shunt over-voltage versus SOVL.
constexpr std::uint32_t kDiagShntUl = 1U << 5;  ///< SHNTUL: shunt under-voltage versus SUVL.
constexpr std::uint32_t kDiagBusOl  = 1U << 4;  ///< BUSOL: bus over-voltage versus BOVL.
constexpr std::uint32_t kDiagBusUl  = 1U << 3;  ///< BUSUL: bus under-voltage versus BUVL.
constexpr std::uint32_t kDiagPol    = 1U << 2;  ///< POL: power over-limit versus PWR_LIMIT.
constexpr std::uint32_t kDiagCnvrf =
    1U << 1;  ///< CNVRF: conversion complete. Clears on reading DIAG_ALRT or writing ADC_CONFIG.
constexpr std::uint32_t kDiagMemStat =
    1U << 0;  ///< MEMSTAT: 0 = trim-memory checksum error, 1 = normal.

/// DIAG_ALRT word for conversion-ready pacing on the open-drain ALERT pin and
/// a latched clip flag: `CNVR = 1` to assert on conversion-ready, `ALATCH = 1`,
/// `APOL = 0` active-low, `SLOWALERT = 0`.
///
/// **`ALATCH = 1` is what makes the clip detector a detector.** The shunt
/// comparator judges every conversion against `SOVL` / `SUVL` before the
/// averaging when `SLOWALERT = 0` ("ALERT comparison on non-averaged (ADC)
/// value", [DS] Table 7-16), and with the limits of kShuntOverLimitClip a
/// conversion that reached either end of the field sets `SHNTOL` or `SHNTUL`.
/// Transparent, the bit clears again as soon as a later conversion is back
/// inside, so on SLEEP, which averages 16 conversions a sample, a clipped one
/// in the middle of an average would be gone by the next read; latched, bits
/// 7-2 hold until `DIAG_ALRT` is read ([DS] SS 7.6.1.12), and every read the
/// sampler makes during a capture ORs into the `flags=` word
/// `FETCh:STATistics?` reports. The bit reads back, so a healthy capture's
/// `flags=` carries `0x8000` too.
///
/// **The pin is held until this register is read, in either mode** (bench,
/// 2026-11-20, `docs/architecture.md` SS 3.3): with `CNVR = 1` the `ALE`
/// pin goes low with `CNVRF` and is released only by a `DIAG_ALRT` read, so a
/// conversion that ends while it is still low makes no edge. `ALATCH = 0` does
/// not make it pulse; it only adds sub-microsecond glitches. So `ALATCH = 1`
/// costs the pacing nothing: `STATE` and `SLEEP` are paced on the pin with a
/// `DIAG_ALRT` read after every edge, which releases it and carries the
/// latched clip bits into `flags=`, and `measurePace()` reads it after every
/// edge it counts. `TRACE` and `BURST` cannot afford that read (it would move
/// the retained pointer) and stay on the host timer.
///
/// **`MEMSTAT` is written back as 1**, and that is not cosmetic: bit 0 is
/// listed **R/W with reset `1h`**, where `0h` means "a checksum error is
/// detected in the device trim memory space" ([DS] Table 7-16;
/// `docs/ina228-reference.md` SS 7.6.1.12 row 0). Writing the word without it
/// puts a zero there, so the instrument would publish its own "trim memory bad"
/// status on the first `INITiate` and `DIAGnostic:ALERt?` would report
/// `memstat=0` for the rest of the session. Writing 1 cannot mask a real fault
/// either: `bringUp()` has already refused to measure unless the bit read back
/// 1 (ina228.cpp step 5), so the only state this write asserts is the one the
/// device itself just reported.
///
/// **Owed to the bench:** whether bit 0 latches what is written or ignores it
/// is not stated in [DS]. Confirm with `DIAGnostic:ALERt?` after the first
/// `INITiate` and record the answer beside the register table in
/// `docs/ina228-reference.md`. Either way this word is correct;
/// the bench run only settles whether the old one was harmful or merely wrong.
///
/// A consequence worth knowing when reading a capture: `MEMSTAT = 1` is the
/// healthy value, so bit 0 set in a `flags=` word is the normal state of a
/// working device, not an alert that fired.
constexpr std::uint16_t kDiagAlrtConversionReady =
    static_cast<std::uint16_t>( kDiagAlatch | kDiagCnvr | kDiagMemStat );

static_assert( kDiagAlrtConversionReady == 0xC001U );
static_assert( !bits::hasBit( kDiagAlrtConversionReady, kDiagSlowAlert ),
               "the clip detector must compare every conversion, not the averaged value" );

// --- SOVL / SUVL (`0x0C`, `0x0D`) as a clip detector ([DS] SS 7.6.1.13-14) ---

/// `SOVL` and `SUVL` after reset ([DS] SS 7.6, reset columns).
constexpr std::uint16_t kShuntOverLimitReset  = 0x7FFF;
constexpr std::uint16_t kShuntUnderLimitReset = 0x8000;  ///< `SUVL` after reset.

/// `SOVL` for the clip detector: eight limit counts under the top of the field.
///
/// The two limits are 16-bit two's complement at 5 uV per count on range 0 and
/// 1.25 uV on range 1 ([DS] SS 7.6.1.13), which is 16 `VSHUNT` counts in either
/// range (312.5 nV and 78.125 nV). So one word sits at the same place in both
/// ranges: 32760 x 5 uV = 163.80 mV of 163.84 mV, and 32760 x 1.25 uV =
/// 40.95 mV of 40.96 mV, 128 counts (0.024 %) inside the end. The reset value,
/// `7FFFh`, is 163.835 mV, which a saturated reading (163.8399 mV) equals in the
/// limit's resolution and so never exceeds: at reset the comparator cannot see
/// a clip. A threshold this close to full scale is not a measurement limit --
/// the limit registers are too coarse for one (`docs/architecture.md`
/// SS 11) -- only the flag that a conversion reached the end of the field.
constexpr std::uint16_t kShuntOverLimitClip = 0x7FF8;

/// `SUVL` for the clip detector: eight limit counts above the bottom of the
/// field, -32760 counts, -163.80 mV on range 0 and -40.95 mV on range 1. The
/// reset value, `8000h`, is the bottom itself, which nothing reads below.
constexpr std::uint16_t kShuntUnderLimitClip = 0x8008;

static_assert( static_cast<std::int16_t>( kShuntOverLimitClip ) == 32760 );
static_assert( static_cast<std::int16_t>( kShuntUnderLimitClip ) == -32760 );

// --- Identity and reset defaults --------------------------------------------

/// MANUFACTURER_ID "reads back TI in ASCII" ([DS] SS 7.6.1.19, Table 7-23).
constexpr std::uint32_t kManufacturerId = 0x5449U;

/// DEVICE_ID bits 15-4 DIEID ([DS] SS 7.6.1.20, Table 7-24). Bits 3-0 are
/// REV_ID, a silicon revision field, and are masked off when identifying --
/// which is what TI's own ecosystem does.
constexpr std::uint32_t kDieId      = 0x228U;
constexpr std::uint32_t kDieIdShift = 4U;       ///< Position of DIEID in DEVICE_ID.
constexpr std::uint32_t kDieIdMask  = 0xFFF0U;  ///< DIEID mask, bits 15-4.
constexpr std::uint32_t kRevIdMask  = 0x000F;   ///< REV_ID mask, bits 3-0.

/// ADC_CONFIG after reset ([DS] SS 7.6, reset columns). It decodes to exactly
/// the documented field resets (`MODE=Fh`, `VBUSCT=VSHCT=VTCT=5h`, `AVG=0h`),
/// so reading it back is a byte-order self-check as much as an identity check:
/// it proves MSB-first framing ([DS] SS 7.5.1.1).
constexpr std::uint16_t kAdcConfigReset = 0xFB68;

static_assert( static_cast<std::uint32_t>( kAdcConfigReset )
                   == adcConfigWord( kModeContinuousAll, kCt1052us, kCt1052us, kCt1052us, kAvg1 ),
               "The documented ADC_CONFIG reset value must decode to the documented field resets" );
static_assert( adcConfigPeriodMicroseconds( kAdcConfigReset ) == 3156U,
               "the reset word converts all three inputs at 1052 us each" );
static_assert( modeConvertsShunt( adcConfigField( kAdcConfigReset, kAdcModeMask, kAdcModeShift ) ),
               "the reset word converts the shunt, as every profile's does (sampler.cpp): "
               "the ADC_CONFIG a range switch times its discard by always does" );

/// Power-on reset time, [DS] SS 6.5 `TPOR`. The bring-up waits longer than this
/// after writing RST.
constexpr std::uint32_t kPorMicroseconds = 300;

/// Oscillator tolerance, [DS] SS 6.5 `FOSC_TOL`: +/-0.5 % at 25 C, +/-1 % over
/// temperature. This is the bound on a timer-paced capture's skew, reported in
/// every header as `osc_tol_ppm` rather than hidden.
constexpr std::uint32_t kOscillatorTolerancePpm = 10000;

// --- Measurement LSBs, as exact integers ------------------------------------
// Picovolts, picoamps, picowatts, picocoulombs and nano-degrees, chosen so that
// every datasheet LSB is an exact integer and the whole conversion path can be
// integer arithmetic ([DS] SS 7.6.1.5-7.6.1.11 and SS 8.1.2, with
// `SHUNT_CAL = 4096`).

constexpr std::int64_t kShuntLsbPvRange0 = 312500;     ///< VSHUNT 312.5 nV/LSB, ADCRANGE = 0.
constexpr std::int64_t kShuntLsbPvRange1 = 78125;      ///< VSHUNT 78.125 nV/LSB, ADCRANGE = 1.
constexpr std::int64_t kBusLsbPv         = 195312500;  ///< VBUS 195.3125 uV/LSB, both ranges.
constexpr std::int64_t kTempLsbNanoC     = 7812500;    ///< DIETEMP 7.8125 m C/LSB, both ranges.

constexpr std::int64_t kCurrentLsbPaRange0 = 3125000;   ///< CURRENT 3.125 uA/LSB, ADCRANGE = 0.
constexpr std::int64_t kCurrentLsbPaRange1 = 781250;    ///< CURRENT 0.78125 uA/LSB, ADCRANGE = 1.
constexpr std::int64_t kPowerLsbPwRange0   = 10000000;  ///< Power 10 uW/LSB (3.2 x CURRENT_LSB).
constexpr std::int64_t kPowerLsbPwRange1   = 2500000;   ///< Power 2.5 uW/LSB.
constexpr std::int64_t kChargeLsbPcRange0  = 3125000;   ///< Charge 3.125 uC/LSB (= CURRENT_LSB).
constexpr std::int64_t kChargeLsbPcRange1  = 781250;    ///< Charge 0.78125 uC/LSB.

/// The shunt full-scale range, CONFIG bit 4 ADCRANGE ([DS] SS 7.6.1.1).
///
/// `Wide163mV` is the default, the range the device boots on: it covers 10 uA
/// to 1.6384 A in one setting, where `Narrow41mV` clips at 409.6 mA and so
/// cannot see a hard-charging board. The finer quantum buys no noise advantage
/// -- across all 64 entries of [DS] Table 8-2 the noise ratio between the
/// ranges has median 1.000, because at 0.100 Ohm the limit is the front end,
/// not the quantizer (`docs/ina228-reference.md` SS 8.2) -- but it
/// does shrink the two error terms charged on full scale and on the count, the
/// INL and the quantum, by four, so the host captures a low floor on
/// `Narrow41mV` where its own records show it cannot clip
/// (the Kleidos firmware repository's `qa/qa_lib/power/ranges.py`).
enum class ShuntRange : std::uint8_t {
    Wide163mV  = 0,  ///< ADCRANGE = 0, +/-163.84 mV, +/-1.6384 A at 0.100 Ohm.
    Narrow41mV = 1,  ///< ADCRANGE = 1, +/-40.96 mV, +/-409.6 mA at 0.100 Ohm.
};

/// @brief VSHUNT LSB in picovolts for @p range.
/// @param[in] range Shunt range.
/// @return The VSHUNT LSB in picovolts.
constexpr std::int64_t shuntLsbPicovolts( ShuntRange range ) noexcept {
    return ( range == ShuntRange::Wide163mV ) ? kShuntLsbPvRange0 : kShuntLsbPvRange1;
}

/// @brief CURRENT LSB in picoamps for @p range.
/// @param[in] range Shunt range.
/// @return The CURRENT LSB in picoamps.
constexpr std::int64_t currentLsbPicoamps( ShuntRange range ) noexcept {
    return ( range == ShuntRange::Wide163mV ) ? kCurrentLsbPaRange0 : kCurrentLsbPaRange1;
}

/// @brief Power LSB in picowatts for @p range.
/// @param[in] range Shunt range.
/// @return The power LSB in picowatts.
constexpr std::int64_t powerLsbPicowatts( ShuntRange range ) noexcept {
    return ( range == ShuntRange::Wide163mV ) ? kPowerLsbPwRange0 : kPowerLsbPwRange1;
}

/// @brief Charge LSB in picocoulombs for @p range.
/// @param[in] range Shunt range.
/// @return The charge LSB in picocoulombs.
constexpr std::int64_t chargeLsbPicocoulombs( ShuntRange range ) noexcept {
    return ( range == ShuntRange::Wide163mV ) ? kChargeLsbPcRange0 : kChargeLsbPcRange1;
}

/// @brief Current ceiling in milliamps for @p range at the nominal 0.100 Ohm
/// shunt.
///
/// 163.84 mV / 0.1 Ohm and 40.96 mV / 0.1 Ohm (`docs/ina228-reference.md`
/// SS 8.2). Reported by `SENSe:CURRent:RANGe:REPort?`, where it is the number
/// that decides whether a run can see a charging board.
///
/// @param[in] range Shunt range.
/// @return 1638 for ShuntRange::Wide163mV, 409 for ShuntRange::Narrow41mV.
constexpr std::int32_t rangeCeilingMilliamps( ShuntRange range ) noexcept {
    return ( range == ShuntRange::Wide163mV ) ? 1638 : 409;
}

/// @}

}  // namespace kleidos::meter::ina228

#endif  // KLEIDOS_METER_INA228_REGS_H
