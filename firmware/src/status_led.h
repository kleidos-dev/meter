// SPDX-FileCopyrightText: 2026 José M. Requena Plens
// SPDX-License-Identifier: GPL-3.0-or-later
/**
 * @file status_led.h
 * @ingroup status_led
 * @brief Blanks the carrier's addressable RGB LED and leaves it dark.
 *
 * This was the whole of the 0.1.0 design's first milestone, and it is the
 * first thing the image does after releasing the relays
 * (`docs/architecture.md` SS 8).
 *
 * A WS2812-class LED **latches**: "the first pixel collect initial 24bit data
 * then sent to the internal data latch" (Worldsemi WS2812B datasheet). The
 * latch is powered from the LED's own VDD, which on this board is the 3.3 V
 * rail, so a `RST` press, an `esp_restart()`, an esptool auto-reset or a flash
 * upload all leave the previous firmware's last color lit -- only unplugging
 * the cable clears it. Flashing over the factory demo is exactly that case, so
 * writing nothing would leave the LED lit forever. An explicit all-zero frame
 * is mandatory, and a plain level write will not do: the part is addressable,
 * and holding the data line low is a *reset code*, not a color.
 *
 * **The LED is never lit again afterwards.** The instrument's job is a clean
 * 3.3 V rail shared with the INA228's analog front end, and a WS2812 refresh is
 * a current burst on that rail for no measurement benefit
 * (`docs/carrier-board.md` SS 4). Status goes over the serial line,
 * where it can be parsed.
 */
#ifndef KLEIDOS_METER_STATUS_LED_H
#define KLEIDOS_METER_STATUS_LED_H

#include "text.h"

#include <driver/gpio.h>

#include <array>
#include <cstddef>
#include <cstdint>

namespace kleidos::meter::led {

/// @addtogroup status_led
/// @{

/// Addressable-LED data lines this image blanks at boot, highest confidence
/// first.
///
/// YD-ESP32-S3 (this board): **GPIO48**, per the vendor's own header table
/// ("GPIO48 RGB LED") and its spec text, and per a second board database.
/// ESP32-S3-DevKitC-1 v1.1: **GPIO38**, where Espressif moved it for the 1.8 V
/// VDD_SPI of the ESP32-S3R8V part -- a different board, but the shield marking
/// `S3-N16R8` is a clone's, so the R8 / R8V question is open
/// (`docs/carrier-board.md` SS 4, SS 9 q4).
///
/// Writing both is free: each is a plain output here, neither is strapping
/// (GPIO0/3/45/46), in-package flash (GPIO26-32), octal PSRAM (GPIO33-37),
/// USB-JTAG (GPIO19/20) nor UART0 (GPIO43/44), and nothing is wired to either
/// terminal. It costs about 2.1 ms per line once, at boot: a 1 ms reset code, the
/// 120 us frame and another 1 ms reset code. **Narrow this to the single
/// confirmed pin once the R8 / R8V question is settled**, so that a wire landing on
/// right-rail terminal 10 later is never fought by our boot.
constexpr std::array<gpio_num_t, 2> kLedDataGpios = { GPIO_NUM_48, GPIO_NUM_38 };

/// Pixels blanked per line. The board carries one; the extra three cost 45 us
/// and clear a chain if one is ever plugged in.
constexpr std::size_t kLedBlankPixels = 4;

/// Bytes per pixel: green, red, blue, MSB first (both WS2812B revisions).
constexpr std::size_t kLedBytesPerPixel = 3;

// --- The waveform -----------------------------------------------------------
// The fitted part's revision is not identifiable from the photograph, and the
// two WS2812B revisions in circulation are backwards incompatible on timing:
// they barely overlap on a `1` bit (T1L 300-600 ns against 580-1000 ns), but
// they overlap generously on a `0` bit. **That is the structural reason an off
// frame is robust where a color feature would not be: it contains only zero
// bits.** The values below sit inside both windows:
//
//   T0H 300 ns -- old 250-550 ns, V5 220-380 ns
//   T0L 950 ns -- old 700-1000 ns, V5 580-1000 ns
//   bit period 1250 ns -- old `TH+TL = 1.25 us +/-600 ns`
//   RES >= 300 us -- old > 50 us, V5 > 280 us; implemented as a 1 ms
//                   busy-wait, because a one-tick `vTaskDelay` runs only to
//                   the next tick boundary and can be microseconds long

constexpr std::uint32_t kT0HighNanoseconds = 300;   ///< `0` bit high time (T0H), in ns.
constexpr std::uint32_t kT0LowNanoseconds  = 950;   ///< `0` bit low time (T0L), in ns.
constexpr std::uint32_t kT1HighNanoseconds = 800;   ///< `1` bit high time (T1H), in ns; not sent.
constexpr std::uint32_t kT1LowNanoseconds  = 600;   ///< `1` bit low time (T1L), in ns; not sent.
constexpr std::uint32_t kResetMicroseconds = 1000;  ///< Reset code (RES) busy-wait, in us.

/// RMT tick rate: the 80 MHz APB clock divided by 4 in transmit channel 0's
/// divider (status_led.cpp). 20 MHz gives 50 ns per tick, so both `0`-bit
/// durations are exact integers rather than rounded ones.
constexpr std::uint32_t kRmtResolutionHz = 20000000;
/// Length of one RMT tick at kRmtResolutionHz, in ns.
constexpr std::uint32_t kNanosecondsPerTick = 1000000000U / kRmtResolutionHz;

static_assert( kNanosecondsPerTick == 50U, "50 ns per tick is what makes the durations exact" );
static_assert( kT0HighNanoseconds % kNanosecondsPerTick == 0U, "T0H must be a whole tick count" );
static_assert( kT0LowNanoseconds % kNanosecondsPerTick == 0U, "T0L must be a whole tick count" );
static_assert( kT0HighNanoseconds + kT0LowNanoseconds == 1250U, "A WS2812B bit period is 1.25 us" );

/// @brief Blanks every line in kLedDataGpios and leaves each driven low.
///
/// Low is the WS2812's idle level, so a driven-low line cannot be misread as
/// data; a floating one can, in principle, pick up enough noise to form a
/// frame. Low costs nothing but leakage.
void blankAll() noexcept;

/// @brief Appends the comma-separated list of blanked pins, e.g. `48,38`, to
/// @p w.
///
/// @param[in,out] w The writer the list is appended to.
void writeBlankedList( text::Writer& w ) noexcept;

/// @brief Appends the `blanked=... pixels=... t0h_ns=... t0l_ns=... res_us=...
/// held=low` report to @p w.
///
/// @param[in,out] w The writer the report is appended to.
void writeReport( text::Writer& w ) noexcept;

/// @}

}  // namespace kleidos::meter::led

#endif  // KLEIDOS_METER_STATUS_LED_H
