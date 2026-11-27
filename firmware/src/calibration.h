// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file calibration.h
 * @ingroup calibration_store
 * @brief The persisted zero-offset null, the measured shunt, and the quality
 *        token every reading carries.
 *
 * At 0.100 Ohm, **10 uA is 1 uV** -- exactly the part's maximum offset spec
 * (`Vos` +/-1 uV max, [DS] SS 6.5). The null is therefore not an optional
 * refinement; it is the measurement
 * (`docs/architecture.md` SS 3.7).
 *
 * **There is no wall clock on this instrument** -- no RTC chip, no Wi-Fi, no
 * NTP. So `cal_tag` is an opaque string the host writes and the instrument
 * only ever echoes, and the calibration date is the host's too
 * (`CALibration:DATE`): checked to be a calendar date, stored, echoed, never
 * compared with anything, because the instrument has nothing to compare it
 * with.
 *
 * **This blob is a working copy.** The calibration is the registry in this
 * repository (`calibration/README.md`): a full flash erase loses it, and
 * `kleidos-meter calibration restore` writes the registry's values back. It
 * is kept in the flash as the newest record of the calibration record log
 * (`flash_log.h`), in the `calib` partition (`flash_port.h`), from FW-16; it
 * was an NVS blob before, and an image that keeps it in the log starts from
 * the defaults, `q=uncal`, until the restore (no migration: reading NVS would
 * need NVS).
 * Everything a restore needs to write is therefore settable here: the shunt
 * of each range (`SENSe:CURRent:SHUNt:RANGe<n>`), the tag, the date and the
 * VBUS side. The nulls are not: they are session state, taken before every
 * measurement session.
 */
#ifndef KLEIDOS_METER_CALIBRATION_H
#define KLEIDOS_METER_CALIBRATION_H

#include "ina228_regs.h"

#include <array>
#include <climits>
#include <cstddef>
#include <cstdint>

namespace kleidos::meter {

/// @addtogroup calibration_store
/// @{

/// @brief Which side of the shunt the VBUS terminal is wired to.
///
/// It matters because the VBUS pin draws 3.8 uA at 3.8 V through its 1 MOhm
/// input impedance ([DS] SS 6.5 `ZVBUS`); wired downstream, that current is
/// counted as DUT current and spoils a 10 uA reading by 38 %. On this breakout
/// VBUS is its own screw terminal, so the choice is the wiring's -- and the
/// header records which one a capture was taken with rather than assuming.
enum class VbusSide : std::uint8_t {
    Unknown = 0,  ///< Not recorded (`CALibration:VBUSside UNKNown`); the default.
    Source  = 1,  ///< On the source side of the shunt (`CALibration:VBUSside SOURce`).
};

/// @brief How far a reading can be trusted (`docs/architecture.md` SS 3.7).
///
/// Carried as `q=` by `SYSTem:STATus?`, `FETCh:HEADer?`, `FETCh:STATistics?`
/// and the other reports `docs/scpi.md` lists it in.
enum class Quality : std::uint8_t {
    Uncal,  ///< No null for the live range, or the shunt has never been measured.
    Stale,  ///< Nulled, but the die temperature has moved more than +/-5 C since.
    Cal,    ///< Nulled for the live range, shunt measured, within +/-5 C.
    /// MATHOF was set during the window, or the shunt was off the DUT's
    /// current (relay.h).
    Invalid,
};

/// @brief The token's spelling on the wire.
/// @param[in] quality The token to spell.
/// @return `uncal`, `stale`, `cal` or `invalid`, a static string; `uncal` for
///         a value outside the enumeration.
const char* qualityName( Quality quality ) noexcept;

/// @brief Nominal R100 shunt, in micro-ohms.
///
/// `R100` is the EIA code for 0.100 Ohm, the `R` being the
/// decimal point. Its tolerance is unmarked on this part, so it is treated as
/// unknown until measured.
///
/// It is **only** the default, never a sentinel for "unmeasured": the single
/// most likely outcome of the two-point calibration on an R100 part is exactly
/// 100.000 mOhm, and a shunt that genuinely measures that must still be able
/// to reach `q=cal`. What "unmeasured" means is carried by
/// `Calibration::shuntMeasured`.
constexpr std::uint32_t kNominalShuntMicroOhm = 100000;

/// @brief The die-temperature window within which a null still stands, in
///        milli-degrees.
///
/// Not arbitrary: `dVos/dT` is +/-10 nV/C max = **+/-0.1 uA/C**
/// at 0.100 Ohm, so +/-5 C is the +/-0.5 uA that is the instrument's floor
/// ([DS] SS 6.5; `docs/ina228-reference.md` SS 8.5). Beyond it the
/// null is no longer the dominant correction it was measured to be.
constexpr std::int32_t kStaleDieMilliCelsius = 5000;

/// @brief The lowest shunt resistance the instrument takes, in micro-ohms:
///        half the nominal.
///
/// The window a measured shunt may fall in, for the R100 part this rig
/// carries: half the nominal to twice it. It is the one scalar every nanoamp
/// is divided by (scale.h), so a typo in it rescales every reading.
/// `SENSe:CURRent:SHUNt` and `SENSe:CURRent:SHUNt:RANGe<n>` refuse a value
/// outside it, and a stored record that holds one is refused when it is read
/// (CalibrationStore::load()), never clamped into it.
constexpr std::uint32_t kMinShuntMicroOhm = kNominalShuntMicroOhm / 2U;
constexpr std::uint32_t kMaxShuntMicroOhm = kNominalShuntMicroOhm * 2U;  ///< The window's top.

/// @brief Whether @p microOhm lies in kMinShuntMicroOhm..kMaxShuntMicroOhm.
/// @param[in] microOhm A shunt resistance, in micro-ohms.
/// @return true inside the window, the bounds included.
constexpr bool shuntInWindow( std::uint32_t microOhm ) noexcept {
    return microOhm >= kMinShuntMicroOhm && microOhm <= kMaxShuntMicroOhm;
}

/// @brief Capacity of the opaque host-written calibration tag, excluding the NUL.
constexpr std::size_t kCalTagCapacity = 24;

/// @brief The earliest year a calibration date may name.
///
/// A calibration date, as `CALibration:DATE` stores it: `yyyymmdd` in one
/// integer, 0 for none. Only the years an instrument of this firmware can
/// have been calibrated in are accepted, kMinCalYear..kMaxCalYear.
constexpr std::uint32_t kMinCalYear = 2026;
constexpr std::uint32_t kMaxCalYear = 2099;  ///< The latest year a calibration date may name.

/// @brief Whether @p year, @p month and @p day name a day of the Gregorian
///        calendar within kMinCalYear..kMaxCalYear.
/// @param[in] year  The year, in full (2026, not 26).
/// @param[in] month The month, 1 to 12.
/// @param[in] day   The day of the month, from 1.
/// @retval true  The date exists and its year is in the accepted window.
/// @retval false The year is outside the window, or the month or day does not
///               exist.
constexpr bool isCalendarDate( std::uint32_t year, std::uint32_t month,
                               std::uint32_t day ) noexcept {
    if ( year < kMinCalYear || year > kMaxCalYear || month < 1U || month > 12U || day < 1U ) {
        return false;
    }
    constexpr std::array<std::uint8_t, 12> kDays = { 31, 28, 31, 30, 31, 30,
                                                     31, 31, 30, 31, 30, 31 };
    // Every fourth year in this window is a leap year: 2100 is outside it.
    const std::uint32_t last = ( month == 2U && year % 4U == 0U ) ? 29U : kDays[month - 1U];
    return day <= last;
}

/// @brief Passed as the live die temperature when none has been read yet.
///
/// TRACE does not convert temperature continuously -- it takes one at each
/// capture boundary -- so before the first boundary there is simply no
/// evidence either way, and a null is not called stale on no evidence.
constexpr std::int32_t kDieMilliCelsiusUnknown = INT32_MIN;

/// @brief Everything the instrument persists, as one record of the
///        calibration record log.
struct Calibration {
    /// The shunt both ranges use unless one has a value of its own.
    std::uint32_t shuntMicroOhm = kNominalShuntMicroOhm;
    /// A range's own shunt value, 0 when it uses shuntMicroOhm. Indexed by
    /// ShuntRange.
    std::uint32_t shuntRangeMicroOhm[2] = { 0, 0 };
    /// The zero offset `CALibration:ZERO` measured, in picovolts. Indexed by
    /// ShuntRange.
    std::int64_t offsetPicovolts[2] = { 0, 0 };
    /// The die temperature the null was taken at, in milli-degrees C, or
    /// kDieMilliCelsiusUnknown when that read failed. Indexed by ShuntRange.
    std::int32_t offsetDieMilliCelsius[2] = { 0, 0 };
    /// True once the range has a null. Indexed by ShuntRange.
    bool nulled[2] = { false, false };

    /// True once `SENSe:CURRent:SHUNt` or `CALibration:SHUNt` has written a
    /// measured resistance. Separate from
    /// the value, so that a shunt measuring exactly the nominal 100000 uohm is
    /// still a measurement.
    bool shuntMeasured = false;

    VbusSide vbusSide = VbusSide::Unknown;  ///< Where the VBUS terminal is wired.
    /// The opaque host-written calibration tag, NUL-terminated.
    char          tag[kCalTagCapacity + 1] = {};
    std::uint32_t date                     = 0;  ///< yyyymmdd, 0 for none.
};

/// @brief Loads, holds and persists the Calibration blob.
///
/// A failed load is not an error: the defaults are a perfectly honest
/// uncalibrated instrument, and `q=uncal` says so on every line.
///
/// The blob is the newest record of the calibration record log
/// (`flash_log.h`) over the `calib` partition (`flash_port.h`) that the
/// store accepts: a record of the current layout (its magic, version and
/// length) whose shunts are measurements this firmware could have taken, the
/// common one in the shunt window and each range's 0 or in it
/// (shuntInWindow()). A record the store refuses is to the log as one whose
/// CRC does not hold: the newest record before it wins, else the defaults
/// (FW-16). One store at a time uses the log (it is one per instrument, the
/// console's).
class CalibrationStore {
   public:
    /// @brief Reads the log and takes its newest accepted record, or leaves
    ///        the defaults in place.
    ///
    /// Mounts the log over the `calib` partition, which reads both sectors
    /// again, as a boot does.
    ///
    /// @return true if a record was found and taken; false (the calibration in
    ///         memory unchanged) when the log holds none the store accepts or
    ///         a read failed.
    bool load() noexcept;

    /// @brief Whether the stored calibration can be read back, without
    ///        touching the one in memory: the self-test's look at the store.
    ///
    /// Mounts the log again, so it reads both sectors as a boot would.
    ///
    /// @return true when the newest accepted record reads back, or when none
    ///         was ever stored (a fresh instrument, or one whose `calib`
    ///         partition holds no record at all); false when a read fails or
    ///         the scan met a whole record (header and CRC) the store refuses
    ///         that is not older than the newest it takes.
    bool storedReadable() noexcept;

    /// @brief Writes the calibration as a new record of the log.
    /// @return true if the log took it and read it back; false if it was
    ///         refused (a shunt outside the window: nothing was written) or
    ///         the flash failed.
    bool save() noexcept;

    /// @brief The whole calibration as it stands in memory.
    /// @return The held Calibration.
    const Calibration& value() const noexcept { return value_; }

    /// @brief The zero offset of @p range.
    /// @param[in] range The ADC range.
    /// @return The offset in picovolts; 0 when the range was never nulled.
    std::int64_t offsetPicovolts( ina228::ShuntRange range ) const noexcept {
        return value_.offsetPicovolts[static_cast<std::size_t>( range )];
    }

    /// @brief Whether @p range has a null.
    /// @param[in] range The ADC range.
    /// @return true once `CALibration:ZERO` has stored an offset for it.
    bool nulled( ina228::ShuntRange range ) const noexcept {
        return value_.nulled[static_cast<std::size_t>( range )];
    }

    /// @brief The die temperature @p range's null was taken at.
    /// @param[in] range The ADC range.
    /// @return Milli-degrees C, or kDieMilliCelsiusUnknown when that read
    ///         failed.
    std::int32_t offsetDieMilliCelsius( ina228::ShuntRange range ) const noexcept {
        return value_.offsetDieMilliCelsius[static_cast<std::size_t>( range )];
    }

    /// @brief Whether a measured shunt resistance has been written.
    /// @return Calibration::shuntMeasured.
    bool shuntMeasured() const noexcept { return value_.shuntMeasured; }

    /// @brief The shunt @p range converts with: its own value, or the common
    ///        one.
    /// @param[in] range The ADC range.
    /// @return The shunt resistance in micro-ohms.
    std::uint32_t shuntMicroOhm( ina228::ShuntRange range ) const noexcept {
        const std::uint32_t own = value_.shuntRangeMicroOhm[static_cast<std::size_t>( range )];
        return ( own != 0U ) ? own : value_.shuntMicroOhm;
    }

    /// @brief Stores a measured shunt resistance for both ranges.
    ///
    /// Drops any value of a range's own (a new calibration replaces the whole
    /// result), and marks the shunt measured.
    /// @param[in] microOhm The measured resistance, in micro-ohms.
    void setShuntMicroOhm( std::uint32_t microOhm ) noexcept;

    /// @brief Stores @p range's own measured shunt and marks the shunt
    ///        measured.
    /// @param[in] range    The ADC range.
    /// @param[in] microOhm The range's resistance, in micro-ohms; 0 returns the
    ///                     range to the common value.
    void setShuntRangeMicroOhm( ina228::ShuntRange range, std::uint32_t microOhm ) noexcept;

    /// @brief Stores the calibration date.
    ///
    /// The caller has checked it with isCalendarDate().
    /// @param[in] yyyymmdd The date as `yyyymmdd` in one integer, 0 for none.
    void setDate( std::uint32_t yyyymmdd ) noexcept;
    /// @brief Stores @p range's null and marks the range nulled.
    /// @param[in] range           The ADC range.
    /// @param[in] picovolts       The measured zero offset, in picovolts.
    /// @param[in] dieMilliCelsius The die temperature it was taken at, in
    ///                            milli-degrees C, or kDieMilliCelsiusUnknown.
    void setOffset( ina228::ShuntRange range, std::int64_t picovolts,
                    std::int32_t dieMilliCelsius ) noexcept;
    /// @brief Records which side of the shunt the VBUS terminal is wired to.
    /// @param[in] side The wiring.
    void setVbusSide( VbusSide side ) noexcept;

    /// @brief Copies at most kCalTagCapacity bytes of @p tag, NUL-terminated.
    /// @param[in] tag The host's tag; nullptr clears it.
    void setTag( const char* tag ) noexcept;

    /// @brief The quality token for @p range at @p dieMilliCelsius.
    ///
    /// The staleness test needs **two** temperatures, the live one and the one
    /// the null was taken at, and either may be kDieMilliCelsiusUnknown -- the
    /// stored one whenever `CALibration:ZERO` ran on a profile whose temperature read
    /// failed. A null is not called stale on no evidence, so the test is
    /// skipped unless both are real; the spread is formed in int64 so that
    /// neither the subtraction nor its negation can overflow.
    ///
    /// @param[in] range           The live ADC range.
    /// @param[in] dieMilliCelsius The live die temperature, in milli-degrees C,
    ///                            or kDieMilliCelsiusUnknown.
    /// @param[in] mathOverflow    true if DIAG_ALRT MATHOF was seen during the
    ///                            window, which outranks everything else.
    /// @return Quality::Invalid on @p mathOverflow; Quality::Uncal without a
    ///         null for @p range or a measured shunt; Quality::Stale when both
    ///         temperatures are known and more than kStaleDieMilliCelsius
    ///         apart; Quality::Cal otherwise.
    Quality quality( ina228::ShuntRange range, std::int32_t dieMilliCelsius,
                     bool mathOverflow ) const noexcept;

   private:
    /// @brief Mounts the log over the `calib` partition with the store's
    ///        judgment of a record, which reads both sectors.
    /// @return true if both sectors were read.
    static bool mountLog() noexcept;

    /// @brief Mounts the log unless this store already did: a save on a
    ///        store that never loaded.
    void ensureMounted() noexcept;

    Calibration value_ = {};  ///< The live calibration.

    /// This store has mounted the log (load(), storedReadable() or a first
    /// save()): every later save appends to what that mount read.
    bool mounted_ = false;
};

/// @}

}  // namespace kleidos::meter

#endif  // KLEIDOS_METER_CALIBRATION_H
