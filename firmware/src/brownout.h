// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file brownout.h
 * @ingroup brownout_detector
 * @brief The supply's brownout detector, armed by the firmware to reset the
 *        chip in hardware, with no interrupt and nothing allocated: in place
 *        of ESP-IDF's brownout handling.
 *
 * ESP-IDF's startup arms the detector with an interrupt whose handler asks
 * an XMC flash chip to reset and then restarts the chip in software
 * (`components/esp_hw_support/power_supply/brownout.c`, chosen by the
 * promptless `CONFIG_SPI_FLASH_BROWNOUT_RESET`); registering that handler
 * through `rtc_isr_register()` allocated three blocks before `app_main()`
 * (FW-18, `docs/misra.md` D1). The meter's flash is not an XMC part, so the
 * image turns ESP-IDF's handling off (`CONFIG_ESP_BROWNOUT_DET=n`,
 * `firmware/sdkconfig.defaults`) and arm() gives the detector the
 * configuration ESP-IDF itself writes when it uses no interrupt: enabled at
 * level kThreshold, a hardware reset of the system when the supply falls
 * below it, the flash suspended and the RF powered down. It goes through
 * `brownout_hal_config()` (`hal/brownout_hal.h`, `esp_hal_pmu`), which writes
 * the RTC controller's registers and allocates nothing.
 *
 * Until arm() runs, right after `app_main()` releases the relays, the
 * bootloader's analog brownout reset holds (`bootloader_esp32s3.c`);
 * `brownout_hal_config()` hands the detector to the digital configuration,
 * as ESP-IDF's does. The
 * hardware's reset reason is `RESET_REASON_SYS_BROWN_OUT`, which
 * `esp_reset_reason()` reports as `ESP_RST_BROWNOUT`, so
 * `SYSTem:HEALth?` names a brownout as it did.
 */
#ifndef KLEIDOS_METER_BROWNOUT_H
#define KLEIDOS_METER_BROWNOUT_H

#include <cstdint>

namespace kleidos::meter::brownout {

/// @addtogroup brownout_detector
/// @{

/// @brief The detector's level: 7, about 2.44 V, ESP-IDF's default for the
///        ESP32-S3 (`CONFIG_ESP_BROWNOUT_DET_LVL_SEL_7` in
///        `power_supply/port/esp32s3/Kconfig.power`), the level every image
///        before FW-18 armed.
constexpr std::uint8_t kThreshold = 7U;

/// @brief Arms the detector: enabled at kThreshold, a hardware system reset
///        below it, the flash suspended and the RF powered down. Allocates
///        nothing and touches no other peripheral; safe to call again, which
///        writes the same configuration.
void arm() noexcept;

/// @}

}  // namespace kleidos::meter::brownout

#endif  // KLEIDOS_METER_BROWNOUT_H
