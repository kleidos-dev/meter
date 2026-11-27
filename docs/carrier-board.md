<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# The DAQ carrier board — identification and pin choices

> **Protocol note (2026-11-19).** The instrument speaks SCPI since firmware 0.2.0, and the command names in this document are its SCPI spellings ([`scpi.md`](scpi.md) has the command set and the map from the console protocol of 0.1.0). The replies the 0.1.0 console printed (`METER_OK: …`, `METER_ERR: …`) now come back as report units and queued errors.


| | |
|---|---|
| Status | Settled, except the four open questions in §9. No code, no build, no flashing. |
| Date | 2026-10-26 (revised 2026-10-30: the HW-316 relay module takes left-rail terminals 7–10 and 21, §8.1) |
| Scope | The ESP32-S3 board that hosts the INA228 bench meter and drives its relay module. The INA228 breakout itself is a separate document. |
| Photos | the owner's photographs of this board and of the INA228 breakout, which are not published: what they show is written where it is used |
| Rule | Every GPIO number, strapping claim and pin function below is either read off the photo or quoted from Espressif / the board vendor, and cited at the point of use. Nothing here is from memory. |

---

## 1. What the board is

Two boards stacked:

**The module board** is a **VCC-GND Studio YD-ESP32-S3**, an ESP32-S3-DevKitC-1 clone. Read off the photo: the shield is marked `S3-N16R8 / WIFI+BT / ISM2.4G 802.11 b/g/n / FC CE SRRC RoHS`; there are two USB-C receptacles side by side; a `CH343P` QFN (marking `CH343P 3F619`) sits above the right-hand receptacle; a SOT-223 regulator is marked `1117C 33` (an AMS1117-3.3 class LDO); there are `RST` and `BOOT` tactile buttons, a 5050 WS2812-style LED beside a silkscreened `RGB`, three `S4` SOD-123 diodes and two `J3Y` SOT-23 transistors (the classic DTR/RTS auto-reset pair).

That identification is confirmed by the vendor's own page, which describes the same board: 16 MB flash, 8 MB PSRAM, "two USBs, one is a hardware USB-to-serial port (CH343P WCH Qinheng), and the other is ESP32-S3 usb port", and an addressable RGB LED.

**The carrier** is silkscreened **`DSX063-1`**. It is passive: two 22-way female headers that the module plugs into, and two 22-position screw-terminal rails, one per side. No active parts are visible on it.

**The carrier is a 1:1 passthrough.** This is not assumed — it is corroborated. Every label on the carrier's left rail matches the module's P1 header in the same order, and the same for the right rail and P2, and that order matches the vendor's published header description independently of the photo (vendor P1: "3V3 (pins 1–2), RST/EN (pin 3), GPIO4–GPIO18 and GPIO46 …, 5V (pin 21), G (pin 22)"; vendor P2: "G (pins 1, 21, 22), TX/GPIO43 (pin 2), RX/GPIO44 (pin 3), GPIO0 (pin 14), GPIO48 RGB LED (pin 16), GPIO19 and GPIO20 USB (pins 19–20)"). Two independent sources landing on the same 44 labels in the same two orders is strong. §10 still lists the continuity check to run before the first wire goes in.

---

## 2. The two screw rails, terminal by terminal

Read top-to-bottom in the photo, with the carrier oriented so `DSX063-1` is upright at the bottom.

The verdicts come from one exhaustive list, which is why "free" is a safe claim and not an absence of evidence. ESP-IDF states, for the ESP32-S3:

> "Strapping pin: GPIO0, GPIO3, GPIO45 and GPIO46 are strapping pins." "SPI0/1: GPIO26 ~ GPIO32 are usually used for SPI flash and PSRAM and not recommended for other uses." "When using Octal flash or Octal PSRAM or both, GPIO33 ~ GPIO37 are connected to SPIIO4 ~ SPIIO7 and SPIDQS." "Therefore, on boards embedded with ESP32-S3R8 / ESP32-S3R8V chip, GPIO33 ~ GPIO37 are also not recommended for other uses." "GPIO19 and GPIO20 are used by USB-JTAG by default."

Those sentences enumerate every chip-level reservation. Anything not in them, and not claimed by something on *this board*, is free.

The two tables below are also data: `firmware/board/pins.toml` holds every terminal with its class (free, caution, reserved) and the reason for it, and the firmware's pin map (`firmware/src/pinmap.h`) and the panel's drawing of the board are generated from it (`scripts/gen_pin_table.py`); `tests/firmware/test_pin_table.py` holds these tables to it row for row. The same file says which pins each role of the pin map may take (§2.2) and which no role may take (§5).

### Left rail (module header P1)

| # | Silk | GPIO | Verdict |
|---|---|---|---|
| 1 | `3.3` | — | 3.3 V, output of the on-board `1117C 33` LDO |
| 2 | `3.3` | — | same net |
| 3 | `RST` | — | chip EN / reset (vendor: "RST/EN (pin 3)"). Not a GPIO. Pulling it low reboots the board |
| 4 | `4` | GPIO4 | **TAKEN** — INA228 `SDA` (§3). Espressif J1 row: "RTC_GPIO4, GPIO4, TOUCH4, ADC1_CH3" |
| 5 | `5` | GPIO5 | **TAKEN** — INA228 `SCL` (§3). Not strapping, not flash/PSRAM, not USB-JTAG, otherwise unused on this board |
| 6 | `6` | GPIO6 | **TAKEN** — INA228 `ALE` (§3). Idem |
| 7 | `7` | GPIO7 | **TAKEN** — relay module `IN1`, K1 the DUT lead (§8.1). Espressif J1 row: "RTC_GPIO7, GPIO7, TOUCH7, ADC1_CH6" |
| 8 | `15` | GPIO15 | **TAKEN** — the shunt bypass (§8.1): the Relay 2 Click's control pin since 2026-11-20, relay module `IN2` (K2) before. Espressif J1 row: "RTC_GPIO15, GPIO15, U0RTS, ADC2_CH4, XTAL_32K_P" |
| 9 | `16` | GPIO16 | **ALLOCATED** — relay module `IN3`, K3 spare (§8.1); not wired since 2026-11-20; from firmware 0.13.0 never driven, and from 0.16.6 parked (an input with the internal pull-down). The temporary load relay NW-00 wired to it was removed on 2026-11-21. Espressif J1 row: "RTC_GPIO16, GPIO16, U0CTS, ADC2_CH5, XTAL_32K_N" |
| 10 | `17` | GPIO17 | **ALLOCATED** — relay module `IN4`, K4 spare (§8.1); not wired since 2026-11-20; from firmware 0.13.0 never driven, and from 0.16.6 parked. Espressif J1 row: "RTC_GPIO17, GPIO17, U1TXD, ADC2_CH6" |
| 11 | `18` | GPIO18 | **free** — Espressif J1 row: "RTC_GPIO18, GPIO18, U1RXD, ADC2_CH7, CLK_OUT3" |
| 12 | `8` | GPIO8 | **free** |
| 13 | `3` | GPIO3 | **STRAPPING** — ESP-IDF, quoted above. Espressif J1 row: "RTC_GPIO3, GPIO3, TOUCH3, ADC1_CH2" |
| 14 | `46` | GPIO46 | **STRAPPING** — ESP-IDF, quoted above. Espressif J1 row: "GPIO46" |
| 15 | `9` | GPIO9 | **free** |
| 16 | `10` | GPIO10 | **free** |
| 17 | `11` | GPIO11 | **free** |
| 18 | `12` | GPIO12 | **free** |
| 19 | `13` | GPIO13 | **free** |
| 20 | `14` | GPIO14 | **free** |
| 21 | `5V` | — | 5 V rail — **TAKEN**: the relay module's `VCC` (§8.1). See §6 — what reaches it from USB depends on the `IN-OUT` jumper |
| 22 | `G` | — | ground — the INA228's `GND`, the DUT-side common-ground tap and the relay module's `GND` all land here (§8.1) |

### Right rail (module header P2)

| # | Silk | GPIO | Verdict |
|---|---|---|---|
| 1 | `G` | — | ground |
| 2 | `TX` | GPIO43 | **TAKEN** — UART0 TX, wired to the CH343P. Espressif: "U0TXD, GPIO43, CLK_OUT1" |
| 3 | `RX` | GPIO44 | **TAKEN** — UART0 RX, wired to the CH343P. Espressif: "U0RXD, GPIO44, CLK_OUT2" |
| 4 | `1` | GPIO1 | **free** |
| 5 | `2` | GPIO2 | **free** |
| 6 | `42` | GPIO42 | free, but it is JTAG MTMS — Espressif: "MTMS, GPIO42" |
| 7 | `41` | GPIO41 | free, but it is JTAG MTDI — Espressif: "MTDI, GPIO41, CLK_OUT1" |
| 8 | `40` | GPIO40 | free, but it is JTAG MTDO — Espressif: "MTDO, GPIO40, CLK_OUT2" |
| 9 | `39` | GPIO39 | free, but it is JTAG MTCK — Espressif: "MTCK, GPIO39, CLK_OUT3, SUBSPICS1" |
| 10 | `38` | GPIO38 | **held low by the firmware** — free on this board's copper (§4), but it is one of the two status-LED lines `firmware/src/status_led.h` blanks at boot and holds low, so no role may take it (`firmware/board/pins.toml`) |
| 11 | `37` | GPIO37 | **NOT AVAILABLE** — octal PSRAM. Espressif: "SPIDQS, GPIO37, FSPIQ, SUBSPIQ" |
| 12 | `36` | GPIO36 | **NOT AVAILABLE** — octal PSRAM. Espressif: "SPIIO7, GPIO36, FSPICLK, SUBSPICLK" |
| 13 | `35` | GPIO35 | **NOT AVAILABLE** — octal PSRAM. Espressif: "SPIIO6, GPIO35, FSPID, SUBSPID" |
| 14 | `0` | GPIO0 | **STRAPPING** + the `BOOT` button |
| 15 | `45` | GPIO45 | **STRAPPING** — Espressif: "GPIO45" |
| 16 | `48` | GPIO48 | the RGB LED net — see §4. Espressif: "GPIO48, SPICLK_N, SUBSPICLK_N_DIFF" |
| 17 | `47` | GPIO47 | VDD_SPI domain — see §4. Espressif: "GPIO47, SPICLK_P, SUBSPICLK_P_DIFF" |
| 18 | `21` | GPIO21 | **free** |
| 19 | `20` | GPIO20 | **TAKEN** — native USB D+. ESP-IDF: used by USB-JTAG by default |
| 20 | `19` | GPIO19 | **TAKEN** — native USB D−. ESP-IDF: used by USB-JTAG by default |
| 21 | `GND` | — | ground |
| 22 | `GND` | — | ground |

GPIO26–GPIO32 are not on either rail at all; they are the in-package SPI flash lines and the module does not break them out. GPIO33 and GPIO34 are likewise absent.

The octal-PSRAM reservation is the one that surprises people, because 35/36/37 *are* printed on the rail and look available. Espressif's own devkit guide says it plainly for exactly this module class:

> "For boards with Octal SPI flash/PSRAM memory embedded ESP32-S3-WROOM-1/1U modules, and boards with ESP32-S3-WROOM-2 modules, the pins GPIO35, GPIO36 and GPIO37 are used for the internal communication between ESP32-S3 and SPI flash/PSRAM memory, thus not available for external use."

The vendor page says the same and still lists them as ordinary pins in its own table. Three screw terminals on this carrier are decoration. Do not wire to them.

### 2.1 What the firmware does with the pins it does not use (0.16.6)

From firmware 0.16.6 every broken-out pin the meter does not use is **parked** at boot, once, after the relays are released: an input with the internal pull-down, no pull-up, no interrupt. From 0.18.0 the set follows the pin map the boot runs: every pin of §2 that a role may take and none does (`pinmap::parkedSet()`, `firmware/src/pinmap.h`, `docs/architecture.md` §9). The table below is the default map's, the rig as wired, which the build holds to `firmware/src/relay.h` `kParkedPins`; a stored map that moves a role parks the pin the role left and not the one it took. A parked pin reads a defined low instead of floating, and a wire that lands on it later meets a weak pull-down, never a driven level.

| Pins | Terminals | Why parked |
|---|---|---|
| GPIO8–GPIO14, GPIO18 | left 12, 15–20, 11 | free (§2) |
| GPIO16, GPIO17 | left 9, 10 | the unwired spares K3 and K4 (§8.1), which reach nothing |
| GPIO1, GPIO2, GPIO21 | right 4, 5, 18 | free (§2) |
| GPIO39–GPIO42 | right 6–9 | JTAG's pads, which carry JTAG only when an eFuse selects them; no eFuse is touched, and the image has no JTAG at all |
| GPIO47 | right 17 | VDD_SPI domain, but not a memory line here: GPIO48, its differential pair, is a plain output (§4); a pull-down is safe at either domain voltage (§9 q4) |

Left alone: the strapping pins GPIO0, GPIO3, GPIO45 and GPIO46 (and GPIO0 is the `BOOT` button); UART0, GPIO43 and GPIO44; the octal PSRAM's GPIO35–GPIO37; GPIO19 and GPIO20, the native USB pair, whose pad ESP-IDF disables at startup now that the image turns the USB-Serial-JTAG off (§7), and which `gpio_config()` would reach only by writing that peripheral's registers with its clock gated; the pins in use (GPIO4–GPIO7, GPIO15) and the two LED candidates GPIO38 and GPIO48, which `status_led.cpp` holds low. **Never wire a parked pin to an HW-316 input**: those are active low, and the pull-down would sink current through the input's LEDs. A pin is given its role in the map before anything is connected to it, and a wire is moved before the map is: the pin a relay leaves is parked toward its released level by the apply that moves it, and low from the next boot. On the rig a parked-low GPIO7 leaves the HW-316's K1 released (measured 2026-11-25, `docs/architecture.md` §2.7), but its input then sinks a current nobody has measured.

### 2.2 Which pins a role may take

From firmware 0.18.0 the pin map gives each of five roles a GPIO, and a map can be changed on the instrument (`SYSTem:PINS`, [scpi.md, "The pin map"](scpi.md#the-pin-map)) or from the panel's Pins view ([panel.md, "The pin view"](panel.md#the-pin-view)). What each role may take is the `[[role]]` rows of `firmware/board/pins.toml`, which the firmware's `pinmap::validate()`, the simulator and the panel all apply, so none of the three can accept a map another refuses; the table below says the same in words, and `tests/firmware/test_pin_table.py` holds it to the file. Over each role's own rule two hold for all of them: no role takes a pin the table reserves (§5: `reserved` in `SYSTem:PINS:PENDing?`), and no pin takes two roles (`shared`).

| Role | Default | Pins it may take | Why |
|---|---|---|---|
| INA228 SDA | GPIO4 (left 4), internal pull-up | GPIO1, GPIO2, GPIO4–GPIO18, GPIO21, GPIO39–GPIO42, GPIO47 | Any free or caution pin: the own I2C master routes SDA through the GPIO matrix to any pad below GPIO49. |
| INA228 SCL | GPIO5 (left 5), internal pull-up | GPIO1, GPIO2, GPIO4–GPIO18, GPIO21, GPIO39–GPIO42, GPIO47 | Any free or caution pin: the own I2C master routes SCL through the GPIO matrix to any pad below GPIO49. |
| INA228 ALERT | GPIO6 (left 6), internal pull-up | GPIO1, GPIO2, GPIO4–GPIO18, GPIO21 | A free pin below GPIO32: the ALERT handler reads one interrupt status register, GPIO_STATUS_REG, which latches GPIO0 to GPIO31 only. |
| Relay 1 | GPIO7 (left 7), active low | GPIO1, GPIO2, GPIO4–GPIO18, GPIO21, GPIO39–GPIO42, GPIO47 | Any free or caution pin: the relay bank drives its input as a plain output. |
| Relay 2 | GPIO15 (left 8), active high | GPIO1, GPIO2, GPIO4–GPIO18, GPIO21, GPIO39–GPIO42, GPIO47 | Any free or caution pin: the relay bank drives its input as a plain output. |

Relay 1 is the DUT's battery lead, K1 on the HW-316, and Relay 2 the shunt bypass, the Relay 2 Click's channel (§8.1); the panel names them by number only. GPIO39 to GPIO42 and GPIO47 are the caution pins: a role may take one, at the cost §2 states (an external JTAG probe for the first four; GPIO47 sits in the VDD_SPI power domain, §4, §9 q4). The two bus pins also carry the INA228's address (a scan of 0x40 to 0x4F at boot, or one of them) and the bus clock at boot (50 kHz to 1 MHz), the `[bus]` ranges of the same file. A relay's active level is the map's, not the pin's: whatever part is fitted, the level that energizes it is set with it.

---

## 3. The I2C pair — first and second choice

Any ESP32-S3 GPIO can carry I2C through the GPIO matrix, so this is a layout decision, not an electrical one. The criteria used: not strapping, not flash/PSRAM, not USB, not UART0, not claimed by anything on this board, and physically adjacent on the rail so the meter's 5-pin header lands on consecutive screws next to a 3V3 terminal.

### First choice — left rail, terminals 4/5/6

| Signal | GPIO | Left-rail terminal |
|---|---|---|
| `SDA` | **GPIO4** | 4 |
| `SCL` | **GPIO5** | 5 |
| `ALE` (INA228 alert) | **GPIO6** | 6 |
| `VCC` | 3.3 V | 1 or 2 |
| `GND` | ground | 22 (`G`), or either `GND` on the right rail |

This is the best block on the board. Terminals 1–6 run `3.3 · 3.3 · RST · 4 · 5 · 6`, so the whole meter — power, both bus lines and the alert — lands on one contiguous run of screws at the top of one rail, with the second `3.3` terminal spare. None of the three is a strapping pin, none is a flash or PSRAM line, none is a USB or UART0 pin, and nothing on this board uses them. Skip terminal 3 (`RST`): it is chip enable, and a wire into it reboots the board.

### Second choice — left rail, terminals 9/10/11

| Signal | GPIO | Left-rail terminal |
|---|---|---|
| `SDA` | **GPIO17** | 10 |
| `SCL` | **GPIO18** | 11 |
| `ALE` | **GPIO16** | 9 |

Equally free, equally contiguous, further from the 3V3 terminals. It is second rather than first only because GPIO17/GPIO18 are the IOMUX pins for UART1 ("U1TXD" / "U1RXD" in Espressif's table), so keeping them clear leaves a second hardware UART on its natural pins if the instrument ever needs one.

**Gone since 2026-10-30:** GPIO16 and GPIO17 drive relays K3 and K4 (§8.1), so this block no longer exists. If the bus ever has to move, the third option below is what is left.

### Third option, if the meter must sit on the right side

GPIO1 (`SDA`) / GPIO2 (`SCL`) on right-rail terminals 4/5, with GPIO21 (terminal 18) for the alert. All three are free. The drawback is that the nearest ground is right-rail terminal 1 and the nearest 3.3 V is on the *other* rail, so power has to cross the board.

**Power the breakout from 3V3, never 5V.** The INA228 breakout photo shows two 0603 resistors in the VCC/SDA/SCL corner, the usual place for the bus pull-ups; if they are pull-ups, they tie the idle bus level to whatever VCC is, and with VCC at 3.3 V the ESP32-S3 pins see a legal level. Their value is not legible in the photo, and confirming what they are and what they pull to belongs to the INA228 document. Either way 3V3 is the right supply: it is the only rail on this carrier that cannot present 5 V to a 3.3 V input.

---

## 4. The RGB LED — which GPIO, and the GPIO38 red herring

**Short answer: GPIO48 on this board, and the LED is disconnected until a solder jumper is bridged.**

**The silkscreen alone does not settle it.** The board prints only the word `RGB` beside the LED; there is no GPIO number anywhere near it. So the photo answers the question only indirectly — but it does answer it, in two ways:

1. The right rail carries both `48` and `38`. The vendor's own header table for this board names terminal 16 of P2 "GPIO48 RGB LED", and its spec section says, in Chinese, that the LED is "driven by GPIO48; the model is WS2812". A second, independent board database gives the same: "an addressable RGB LED (GPIO48)".
2. **GPIO38 belongs to a different board.** Espressif moved the RGB LED from GPIO48 to GPIO38 between ESP32-S3-DevKitC-1 **v1.0** and **v1.1** — "the initial version uses GPIO48, whereas v1.1 uses GPIO38" — and the reason is specific: "the ESP32-S3R8V chip's VDD_SPI voltage has been set to 1.8 V. Therefore, unlike other GPIOs, GPIO47 and GPIO48 in the VDD_SPI power domain of this chip also operate at 1.8 V." That is the **R8V** part. This module is marked `S3-N16R8`, no `V`, so GPIO47/GPIO48 sit in a 3.3 V VDD_SPI domain and GPIO48 is an ordinary 3.3 V pin. On this board GPIO38 is just a free pin on right-rail terminal 10.

**There is a jumper in the way, and in this photo it is open.** Zooming into the area beside the `RGB` silkscreen shows a two-pad solder jumper — two triangles facing each other — with a clear unbridged gap. A second jumper, silkscreened `IN-OUT`, sits beside the GPIO11/GPIO12 header labels and is also open in the photo. This matches the documented factory state: the board "has a jumper called 'RGB', another called 'IN-OUT', and another called 'USB-OTG', all open … it may be necessary to solder the jumper for the devices to work". Users report the LED doing nothing until the RGB jumper is bridged.

**Recommendation for the DAQ firmware: do not use the RGB LED at all, and leave the jumper open.** The instrument's whole job is a clean 3.3 V rail shared with the INA228's analog front end; a WS2812 refresh is a current burst on that rail for no measurement benefit. GPIO48 then stays a free pin.

**If a status indicator is wanted anyway, here is the safe way to handle both candidates.** Do not guess at build time and do not hard-code a bare number at a call site. Put it behind one named constant with one definition site, e.g.

```c
// Addressable RGB LED data line.
// YD-ESP32-S3 (this board): GPIO48, behind the open "RGB" solder jumper.
// ESP32-S3-DevKitC-1 v1.1:  GPIO38 (Espressif moved it for the 1.8 V VDD_SPI of ESP32-S3R8V).
constexpr int kStatusLedGpio = 48;
```

and settle it empirically in one minute: bridge the `RGB` jumper, drive GPIO48 with a WS2812 waveform, and if nothing lights, try GPIO38. Only one of the two is wired on any given board, and nothing is damaged by driving the wrong one — both are plain outputs here. Note the second reported gotcha: a plain level write does nothing, because the part is addressable; it needs a real WS2812 bit stream.

---

## 5. Pins that must not carry I2C, in one list

The firmware holds this list: its rows are the `reserved` and `caution` rows of `firmware/board/pins.toml`, the board's one table (§2). No role of the pin map may take a reserved pin (`pinmap::validate()` answers `reserved`, and the panel draws the pin as reserved by the board); a caution pin a role may take, at the cost stated (§2.2). `tests/firmware/test_pin_table.py` holds the list to the table, pin for pin.

| Pins | Why | Source |
|---|---|---|
| GPIO0, GPIO3, GPIO45, GPIO46 | strapping; their level at reset selects boot mode, JTAG source and VDD_SPI voltage | ESP-IDF |
| GPIO26–GPIO32 | in-package SPI flash | ESP-IDF |
| GPIO33–GPIO37 | octal PSRAM on an R8 part; 35/36/37 are on the rail and still unusable | ESP-IDF + Espressif devkit guide |
| GPIO19, GPIO20 | native USB / USB-Serial-JTAG | ESP-IDF |
| GPIO43, GPIO44 | UART0, wired to the CH343P bridge on this board | Espressif pin table + board photo |
| GPIO47, GPIO48 | VDD_SPI power domain; GPIO48 is also the RGB net. GPIO47 is caution, a role may take it (§2.2); GPIO48 is reserved | Espressif pin table + ESP-FAQ + vendor |
| GPIO38 | free on the copper, but a status-LED line the firmware blanks at boot and holds low (§4) | `firmware/src/status_led.h` |
| GPIO39–GPIO42 | caution: usable, but they are MTCK/MTDO/MTDI/MTMS; taking them costs an external JTAG probe | Espressif pin table |
| GPIO4–GPIO7, GPIO15 | taken by the default map: the INA228's bus and alert (§3), Relay 1 and Relay 2 (§8.1); a stored map may move any of them (§2.2) | this document, `firmware/board/pins.toml` `[defaults]` |
| `RST` terminal | chip enable, not a GPIO | vendor |

---

## 6. Power on the terminals

**Both rails are exposed, as the owner expected.** 3.3 V on left-rail terminals 1 and 2, 5 V on left-rail terminal 21, ground on left-rail 22 and right-rail 1, 21 and 22.

3.3 V is the output of the on-board `1117C 33` LDO (AMS1117-3.3 class), fed from USB 5 V. The INA228's own supply current is small enough that the LDO is not a concern; it is the right rail to power the breakout from, and it is what makes the I2C levels correct (§3).

**The 5 V terminal has a caveat worth measuring.** The documented behavior of the `IN-OUT` jumper is: "when closed, bypasses one diode, making USB VBus power coming to 5Vin", and when open, "5Vin and USB VBus are separated by diode, USB power does not come to 5Vin". The photo shows that jumper **open**, and shows three `S4` Schottky diodes on the board. Whether the 5 V terminal therefore reads ~5 V, ~4.7 V or nothing with only a USB cable attached is not something the photo can tell. Measure it (§10) before planning to power anything from it. Closing `IN-OUT` is explicitly discouraged if an external supply might also feed 5Vin, because it back-feeds the USB host.

**Measured on this board, 2026-10-30.** With `IN-OUT` open and only the USB cable attached, terminal 21 read **2.5 V** (leakage through the reverse-biased diode, not a supply). The owner then **bridged `IN-OUT` with solder**, and terminal 21 reads **4.9 V** unloaded, so the relay module's `VCC` stays on terminal 21 (§8.1). The bridge is now permanent: **nothing else may ever feed the 5V terminal**, since the bypassed diode makes the path bidirectional into the USB host. The loaded check of `rig-wiring.md` §8.5 step 1 (at least 4.5 V across 68 Ω) still applies before the module goes on.

**Terminal 21 now has a load, and the question stops being academic.** The relay module's `VCC` lands on it (§8.1). Each SRD-05VDC-SL-C coil is 70 Ω ±10 % and draws 71.4 mA at 5 V, and it is only guaranteed to pull in at 75 % of its rated voltage, 3.75 V (Songle's coil data chart, high-sensitivity 0.36 W row). The null takes two coils at once, so terminal 21 has to deliver about 145 mA (two coils plus two input LEDs) and stay above 3.75 V while it does.

The vendor's own words make "nothing" the likely answer with `IN-OUT` open, and an **unloaded** reading cannot settle it: a 10 MΩ multimeter on a terminal fed only through a Schottky's reverse leakage can read close to 5 V. So terminal 21 is measured **loaded**, with a 68 Ω resistor (one coil's worth; 0.37 W at 5 V, so a 1 W part) from 21 to 22, before the module goes on (§10 check 6). If it collapses, the remedy is closing `IN-OUT`. The warning above does not bar it here — the module is a load and nothing else feeds 5Vin — but it is the owner's call.

An unpowered module is safe, since every relay stays released and the DUT stays on the shunt, but it is **not inert**: the console still carries out orders it cannot see fail. `ROUTe:NULLpath ON` reads the shunt between its steps and refuses (`bypass check failed`) when the DUT draws 2 mA or more; below that it cannot tell and says `check=unverified` (`README.md`, "The relays"). That is why the module is proved on the bench (`rig-wiring.md` §8.5) before a relay null is trusted.

**The module's 5 V must never outlive the meter's 3.3 V.** With the module powered and the ESP32-S3 not, each `IN` pin is clamped toward ground through the chip's protection diodes, which can sink enough of the ~2 mA an input needs to energize all four coils at once — K1 opening and K2 closing in no particular order. Terminal 21 keeps the rule by construction: it is the carrier's own rail, and the module loses its 5 V with the board. A separate 5 V supply, if one is ever needed, goes **into terminal 21**, the carrier's own 5 V input, so that the meter runs from it too — never to the module's `VCC` alone.

The third jumper, `USB-OTG`, "when closed, connects together USB VBus lines from both USB-C connectors". It was not located in this photo — it may be on the underside. Leave it open: the instrument uses one cable.

---

## 7. The two USB-C ports: which to flash from, which to talk to

The board has two receptacles and they are **not** interchangeable.

**The CH343 port** goes to the `CH343P` bridge chip, which drives UART0 — GPIO43 (`U0TXD`) and GPIO44 (`U0RXD`) — plus the auto-reset circuit built from the two `J3Y` transistors, which drives EN and GPIO0 from the host's DTR/RTS lines. The host sees a WCH device, `1a86:55d3`.

**The native port** goes straight to the SoC's own USB peripheral on GPIO19/GPIO20. The host sees an Espressif device, `303a:1001`, named `USB JTAG_serial debug unit`.

**Use the CH343 port for both flashing and the console. Leave the native port unplugged.** Three reasons:

1. **It is what the owner is already on, and it is already correct.** `ioreg` on the bench's computer showed, on 2026-10-26, `idVendor = 6790` (0x1A86), `idProduct = 21971` (0x55D3) and a `USB Serial Number` (private bench data, not recorded here) — the DAQ board — enumerated **outside** the Amazon Basics hub chain, on a Mac-adapter port. That matches the bench decision to keep the instrument on a port `uhubctl` cannot cut. Its node is **`/dev/cu.usbmodem<serial>1`**. (The CH343 presents as CDC-ACM, so macOS names it `usbmodem…`; the fleet's CH9102 boards, `1a86:55d4`, appear as `usbserial-…` instead. Both patterns are visible in the same listing.)
2. **A reset on the native port destroys the capture.** On a native-USB board a reset tears down the USB device and the host's open descriptor dies with it — measured in this project at 0 characters captured by the session that triggered the reset. On a bridge board the USB endpoint is a separate chip that stays up across a target reset. For an instrument that streams samples for hours, that difference is the whole argument.
3. **Flashing over the bridge needs no strapping gymnastics.** The auto-reset transistors put the chip into download mode; `BOOT`+`RST` by hand is the fallback, not the routine.

**The trap to design around:** opening the CH343 port can itself reboot the board. The auto-reset circuit pulls EN low for exactly one line state — DTR released while RTS is still asserted — and pyserial's default open passes through it. This project's fix is to open the port as-is and **release RTS first, then DTR**, which was measured to stop the reboot on a bridge board (uptime 51697 → 55714 → 59731 ms across three opens, where every open had previously rebooted). The DAQ host tool must reuse that open sequence, not a bare `serial.Serial(...)`. The witness that it worked is an increasing uptime across opens, never the chip's own last-reset reason, which reports the same value either way.

**Which physical receptacle is which — not settled by the photo, and it does not need to be.** The silkscreen in the gap between the two connectors is a short word, most consistent with `COM`, but it is partly hidden by a connector shell and its orientation is ambiguous at this resolution, so this document will not assert which connector it labels. Two physical clues point in opposite directions — the `CH343P` die sits directly above the right-hand receptacle, while GPIO19/GPIO20 emerge at the bottom of the right-hand header, also near it — so neither settles it. The reliable test takes five seconds and needs no silkscreen:

```sh
# Plug ONE cable, then:
ioreg -p IOUSB -w0 -l | grep -E '"(USB Product Name|idVendor|idProduct|USB Serial Number)"'
# 0x1A86 / 0x55D3  (6790 / 21971)  -> the CH343 port.  Use this one.
# 0x303A / 0x1001  (12346 / 4097)  -> the native USB port.  Leave it unplugged.
```

Equivalently: `ls /dev/cu.*` before and after plugging. A `cu.usbmodem<serial>1` appearing is the CH343 port on this particular board. Mark the correct receptacle with a dab of paint once and never think about it again.

---

## 8. What this means for the INA228 header

The breakout's 5-pin header is `GND · ALE · SCL · SDA · VCC` (as the breakout's silkscreen prints it). Against the first choice in §3, left rail only:

| Breakout pin | Carrier terminal |
|---|---|
| `VCC` | left 1 (`3.3`) |
| `SDA` | left 4 (`4`) |
| `SCL` | left 5 (`5`) |
| `ALE` | left 6 (`6`) |
| `GND` | left 22 (`G`) |

`VIN+` / `VBUS` / `VIN−` on the breakout's screw block are the measurement path and have nothing to do with the carrier's rails. The `A0`/`A1` address jumpers, the R100 shunt and the range/resolution decision belong to the INA228 document.

### 8.1 And the relay module's header

The HW-316 four-relay module (owner's allocation, 2026-10-30) lands its six-pin header `GND · IN1 · IN2 · IN3 · IN4 · VCC` on the same rail, its four inputs on the next four terminals after the INA228's run:

| Module pin | Relay, role | GPIO | Carrier terminal |
|---|---|---|---|
| `GND` | — | — | left 22 (`G`) |
| `IN1` | K1, the DUT lead (series, released = closed) | **GPIO7** | left 7 |
| `IN2` | K2, the shunt bypass (released = open) until 2026-11-20; **unwired** since, GPIO15 drives the Relay 2 Click | **GPIO15** | left 8 |
| `IN3` | K3, spare; **not wired** since 2026-11-20 | **GPIO16** | left 9 |
| `IN4` | K4, spare; **not wired** since 2026-11-20 | **GPIO17** | left 10 |
| `VCC` | coils and input LEDs, 5 V | — | left 21 (`5V`) |

The JD-VCC jumper stays fitted, so the coils run from the same `VCC`. What each contact is wired to, the order of assembly and the first-power check are in `rig-wiring.md` §8; the drawing is `hardware/rig.kicad_sch` rev D. Since 2026-11-20 only K1 is used, as a single-relay module: only `GND`, `IN1` and `VCC` are wired, the bypass is a MikroE Relay 2 Click on GPIO15 (active high, `relay2-click-wiring.md`), powered from left 1 (3.3 V) and 22, and K2–K4 never energize. GPIO16 and GPIO17 reach no HW-316 input, and from 0.13.0 the firmware never drives them (`firmware/src/relay.h` `kChannels`, `ChannelSpec::wired`): the spares `k3` and `k4` read `unwired`. From 0.16.6 it parks both at boot, inputs with the internal pull-down (§2.1). The allocation below still holds, and the pins stay reserved for the relay work; the temporary load relay NW-00 wired to GPIO16 (the Relay 2 Click's RL1) was removed on 2026-11-21. **Never wire a parked pin to an HW-316 input**: the pull-down sinks current through its input LEDs; the channel is marked wired first, and the firmware then drives it high.

**Why these four pins are safe for a relay that must never close by accident.** The inputs are low-level triggered: a pin driven **low** energizes its relay, and a pin driven high or left floating leaves it off. So what matters is what each pin does before the firmware drives it, and the ESP32-S3 datasheet answers that for all four: in Table 2-1 ("Pin Overview", v2.2) `GPIO7`, `XTAL_32K_P` (GPIO15) and `XTAL_32K_N` (GPIO16) carry **no** setting at reset, and `GPIO17` carries only "IE" (input enabled) — no weak pull-up and, above all, no weak pull-down on any of them. From reset until the firmware drives them high they float, and the module reads a floating input as off. A reset, a reflash or a meter left unpowered therefore releases every relay. A pin whose reset state is a weak pull-down (the datasheet gives GPIO45 and GPIO46 one) would have been the wrong choice.

One exception, from the same datasheet's Table 2-2 ("Power-Up Glitches on Pins"): at **power-up** `GPIO7`, `XTAL_32K_P`, `XTAL_32K_N` and `GPIO17` each sit at a low-level output for typically **60 µs** ("Low-level glitch: the pin is at a low level output status during the time period"). That is a real low on an input that energizes on low, but a coil needs milliseconds of current to pull in (operate time 10 ms max), so no contact moves; an IN LED may flash. The first-power check of `rig-wiring.md` §8.5 watches for it.

What three of them give up:

- **GPIO15/GPIO16 are the 32 kHz crystal pins** ("XTAL_32K_P" / "XTAL_32K_N"). They are free only because the meter's image runs its RTC slow clock on the internal RC (`CONFIG_RTC_CLK_SRC_INT_RC`, `firmware/sdkconfig.defaults`). An image that selected an external 32 kHz crystal would claim them.
- **GPIO17 is UART1's IOMUX TX pin** ("U1TXD"), the reason §3 kept 17/18 clear. A second UART can still be routed to another pin through the GPIO matrix; it has lost only its natural pin.
- **GPIO15/GPIO16 are also "U0RTS" / "U0CTS"** in Espressif's table. The console runs UART0 with hardware flow control off and its RTS/CTS left unrouted (`UART_HW_FLOWCTRL_DISABLE`, and only GPIO43 and GPIO44 given to the port, `firmware/src/uart0.cpp`), so nothing here needs them.

**Terminal 22 now carries three conductors**: the INA228's `GND` (§8), the DUT-side common-ground tap (`rig-wiring.md` row A4) and the module's `GND`, which returns up to ~145 mA of coil current during a null. If the screw will not hold three, a twin ferrule is the way; right-rail terminals 1, 21 and 22 are the same net (§2).

---

## 9. What is NOT settled

1. **Which physical receptacle is the CH343 one.** §7 gives the test instead of a guess.
2. **What the 5 V terminal actually reads with only USB attached**, given the open `IN-OUT` jumper and the Schottky diodes. §6. It now feeds the relay module, so it has to hold above 3.75 V under load (§6, §8.1, §10 check 6).
3. **Where the `USB-OTG` jumper is** — not visible in this photo; possibly on the underside.
4. **Whether the module's chip is an R8 or an R8V.** The shield is marked `S3-N16R8` with no `V`, which says 3.3 V VDD_SPI and therefore 3.3 V on GPIO47/GPIO48 — but that marking is a clone shield's, not Espressif's. It does not matter for any choice in this document, because nothing here uses GPIO47 or GPIO48. It would matter the moment something did.

---

## 10. Checks before the first wire

Five minutes with a multimeter, before anything is connected:

1. **Continuity, carrier to module.** Probe left-rail terminal 1 against the module's `3V3` header pin, and left-rail terminal 4 against the module's `4` pin. Two matches confirm the 1:1 passthrough that §1 argues for from labels alone.
2. **3.3 V present.** Left-rail terminal 1 to terminal 22 should read ~3.3 V with the CH343 cable in.
3. **5 V terminal.** Left-rail terminal 21 to 22, same conditions. Record whatever it reads — that is open question 2.
4. **No short between the chosen pair.** Left-rail 4 to 5 should read open.
5. **The port is the one you think.** Run the `ioreg` line from §7 and confirm `1a86:55d3` and the serial `KLEIDOS_METER_PORT` names.
6. **Terminal 21 under load, before the relay module goes on.** Check 3 gives the idle number, which proves nothing on its own (§6). With the CH343 cable in, a **68 Ω, 1 W resistor from 21 to 22**: it must read **at least 4.5 V**, which leaves room for the second coil's 71 mA and still clears the 3.75 V pull-in. Under that, or ≈ 0 V, stop: the module would not switch, and powering it otherwise is the owner's call (§6). Then `rig-wiring.md` §8.5 step 8 measures it again with two real coils in. **Terminal 21 ↔ terminal 1 must read open**: a strand across them puts 5 V on the 3.3 V rail.

---

## Sources

Board and vendor:

- VCC-GND Studio, YD-ESP32-S3 repository — <https://github.com/vcc-gnd/YD-ESP32-S3> (header P1/P2 tables, the spec section's sentence on the LED, the GPIO35/36/37 octal note, the two USB ports and the CH343P)
- espboards.dev, YD-ESP32-S3 — <https://www.espboards.dev/esp32/yd-esp32-s3/> ("an addressable RGB LED (GPIO48)", GPIO35–37 used by octal PSRAM on N8R8/N16R8, GPIO19/20 native USB D−/D+, GPIO43/44 UART0)
- deepcoder/YD-ESP32-23 — <https://github.com/deepcoder/YD-ESP32-23> (the three jumpers `RGB`, `IN-OUT`, `USB-OTG`, "all open"; what `IN-OUT` and `USB-OTG` connect; the LED needing a WS2812 stream)

Espressif:

- ESP-IDF, GPIO & RTC GPIO (ESP32-S3) — <https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/gpio.html> (strapping pins; GPIO26–32 flash/PSRAM; GPIO33–37 octal; GPIO19/20 USB-JTAG)
- ESP32-S3-DevKitC-1 v1.1 user guide — <https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/user_guide_v1.1.html> (J1/J3 Function column rows quoted in §2, including GPIO7, GPIO15, GPIO16 and GPIO17; the GPIO35/36/37 "not available for external use" note; the v1.0 GPIO48 → v1.1 GPIO38 change; USB-to-UART port used by default for flashing)
- ESP32-S3 Series Datasheet v2.2 — <https://documentation.espressif.com/esp32-s3_datasheet_en.pdf>, Table 2-1 "Pin Overview": the "At Reset" column (no setting for GPIO7, XTAL_32K_P and XTAL_32K_N, "IE" for GPIO17, "WPD, IE" for GPIO45 and GPIO46) and its legend ("IE – input enabled", "WPU – internal weak pull-up resistor enabled", "WPD – internal weak pull-down resistor enabled"); Table 2-2 "Power-Up Glitches on Pins": GPIO1–GPIO14, XTAL_32K_P, XTAL_32K_N and GPIO17 "Low-level glitch", typical 60 µs, and its footnote ("the pin is at a low level output status during the time period")
- ESP-FAQ, development board — <https://docs.espressif.com/projects/esp-faq/en/latest/hardware-related/development-board.html> ("the ESP32-S3R8V chip's VDD_SPI voltage has been set to 1.8 V … GPIO47 and GPIO48 in the VDD_SPI power domain of this chip also operate at 1.8 V")

Relay module:

- Songle SRD series relay datasheet (the `SRD-05VDC-SL-C`: S sealed, L 0.36 W coil, C one form C) — coil data chart, 05 VDC high-sensitivity row: 71.4 mA, 70 Ω ±10 %, pull-in 75 % max, drop-out 10 % min; performance table: contact resistance 100 mΩ max, operation time 10 ms max, release time 5 ms max. Mirror read: <https://www.circuitbasics.com/wp-content/uploads/2015/11/SRD-05VDC-SL-C-Datasheet.pdf>

This bench:

- the owner's photographs of this board and of the INA228 breakout (not published)
- `ioreg -p IOUSB` on the bench's computer, 2026-10-26: `1a86:55d3` and its USB serial (private bench data, not recorded here), outside the hub chain
- The bench's survey of its USB hub, 2026-10-25, a working note that is not published: the ten hub ports that do cut VBUS, and why the instrument is deliberately not on one of them
