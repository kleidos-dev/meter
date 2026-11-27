<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# The bench meter firmware — design

> **Not every path this record cites is published.** It names files of the Kleidos firmware repository as they were when it was written; they are not part of this site, and the current pages say what a reader needs instead.

> **Frozen on 2026-11-22 as the design record of firmware 0.1.0 (2026-10-26 to 2026-11-19).** The current design is [`architecture.md`](architecture.md), which every change keeps current; this document is no longer edited, and nothing outside it and the changelog cites its sections (`tests/firmware/test_no_stale_references.py` holds that). The sections later releases added to it (§3.1.1, §3.5.1, §3.10.1, §3.12 to §3.16, §6.5) moved there, each leaving its heading and a pointer; the rest is the design as it was settled before the first image, in that time's present tense, with the amendments it received up to 0.16.6. Where this document and the code disagree, the code, [`scpi.md`](scpi.md) and `architecture.md` win, and [`CHANGELOG.md`](../CHANGELOG.md) lists every change on the wire. What changed since, in short:
>
> - **The project** left the Kleidos firmware repository on 2026-11-19: its nested project is `firmware/` here, its version is `firmware/version.txt`, and it builds with `pio run --project-dir firmware -e meter` (or `-e meter_relay2`).
> - **The protocol** is SCPI-1999 from 0.2.0 (§4 is the 0.1.0 console, kept as the record; the map is in `scpi.md`).
> - **Four profiles**, not three: `BURST` (50 µs conversions, ring, needs a 1 MHz bus) joined `TRACE`, `STATE` and `SLEEP` (§3.4).
> - **PSRAM is on** (octal, `sdkconfig.defaults`), and the ring holds 1,048,576 samples (293 s of `TRACE`) where §3.9 and §6.4 say 32,768 in internal RAM; the header examples (`fw=0.1.0`, `ring=32768`) are 0.1.0's.
> - **The line is 6 Mbps** (§3.9.1 was written after that change; any 921600 elsewhere is the earlier figure).
> - **Pacing** (§3.5, 0.9.0): the `ALE` pin is held low from a conversion's end until `DIAG_ALRT` is read, whatever `ALATCH` says (bench, 2026-11-20). `STATE` and `SLEEP` are paced on it with a `DIAG_ALRT` read after every edge and report `pace=alert`; `TRACE` and `BURST` stay on the host timer and report `pace=timer pace_edges=0 pace_expect=0`. Before 0.9.0 the probe counted one edge and every capture ran `pace=timer`.
> - **The relays** (§3.11) are wired as `rig-wiring.md` and `hardware/` (rev D) record, with the bypass on a Relay 2 Click, driven by the one image (`meter`, 0.8.0 and later; the `meter_relay2` image from 0.3.0 to 0.7.0, while `meter` drove the HW-316); the identity (`serial=`, `image=`, `elf=`), the calibration registry and the per-range shunts arrived after this document (`scpi.md`, `calibration/README.md`).

> **Protocol note (2026-11-19).** The instrument speaks SCPI since firmware 0.2.0, and the command names in this document are its SCPI spellings ([`scpi.md`](scpi.md) has the command set and the map from the console protocol of 0.1.0). Replies quoted here as `METER_OK: …` or `METER_ERR: …` lines are the 0.1.0 console's; the same facts now come back as report units and queued errors. Line references into `console.cpp` predate the SCPI console.


| | |
|---|---|
| Status | Design settled. **Nothing was built, flashed or uploaded for this document.** |
| Date | 2026-10-26; §3.11, the relay module, 2026-10-30 |
| Scope | The firmware of the DAQ instrument: ESP32-S3-N16R8 on a DSX063-1 carrier, reading a TI INA228 on an R100 shunt. What it does at boot, how it samples, what it says over serial, and what it deliberately does not do yet. |
| Depends on | [`ina228-reference.md`](ina228-reference.md) (registers, LSBs, the range arithmetic), [`carrier-board.md`](carrier-board.md) (GPIOs, the two USB ports, the LED); the project was then a nested project of the Kleidos firmware repository, outside its fleet build, and is `firmware/` in this repository now |
| Boards | read off the owner's photographs of the two boards, which are not published: what this record takes from them is written where it is used, and §9 lists it |
| Rule | Every register address, bit field, GPIO number, Kconfig symbol and ESP-IDF type below is quoted from the INA228 datasheet, the WS2812B datasheet, the installed ESP-IDF 6.0.3 tree, the photographs or this repository, and cited where it is used. Nothing is from memory. §9 lists the sources. |

---

## 0. The answer in one page

| Question | Decision | Where |
|---|---|---|
| First buildable milestone | **M1: the RGB LED goes dark and stays dark. Nothing else.** | §2 |
| Explicit "off" frame needed? | **Yes.** A WS2812 latches its last color and a soft reset does not clear it | §2.1 |
| Which LED GPIO? | **Both 48 and 38**, in that order, then both held low. Narrowed to one at the end of M1 | §2.2 |
| I²C pins | `SDA = GPIO4`, `SCL = GPIO5`, `ALE = GPIO6` (left rail 4/5/6) | §3.1 |
| I²C address / speed | `0x40`, confirmed by a boot scan; **400 kHz** fast mode, settable | §3.1 |
| High-speed I²C mode | **Impossible.** ESP-IDF 6.0.3's `driver/i2c_master.h` has no master-code API | §3.1 |
| Shunt range | **`ADCRANGE = 0` always.** One range covers 10 µA … 1.6384 A | §3.3 |
| `SHUNT_CAL` | **`0x1000` (4096), exact, never corrected** | §3.3 |
| Shunt correction | **In software**, as `shunt_uohm`, never in `SHUNT_CAL` | §3.3 |
| Profiles | **`TRACE`** 280 µs · **`STATE`** 1152 µs · **`SLEEP`** 67.52 ms | §3.4 |
| `ADC_CONFIG` | `0xA0C0` · `0xF140` · `0xF1C2` — each re-derived bit by bit | §3.4 |
| Pacing | `STATE`, `SLEEP`: the `ALE` pin with a `DIAG_ALRT` read per edge, **measured at `INITiate`**, host timer if it fails; `TRACE`, `BURST`: host timer | §3.5 |
| Primary quantity | **`VSHUNT`**, not `CURRENT` | §3.6 |
| Arithmetic | **Integer, picovolts and nanoamps.** Floats only at the print boundary | §3.7 |
| Sink | **`TRACE` → 128 KiB RAM ring + `TRACe:DATA?`. `STATE`/`SLEEP` → live stream** | §3.9 |
| Where the hot path runs | **From IRAM** (0.16.1): everything a conversion executes on Core 1, ours and ESP-IDF's, held by a gate on the linked image | §3.10.1 |
| Protocol | `TELEM`-shaped: `METER t=<us> ch=<name> v=<float>`, `_OK:`/`_ERR:`, `_END` | §4 |
| Self-describing capture | A `METER_HDR` block at `INITiate` carrying every LSB and the live calibration, all as integers | §4.2 |
| Not in v1 | `ENERGY`, triggered sampling, threshold alerts, auto-ranging, tempco, SD, Wi-Fi, VBUS switch, PSRAM, `BUDGET` verdicts | §5 |
| Kept against the suggested list | `SENSe:CHARge?` / `SENSe:CHARge:RESet`, as a **read-only probe** of one open question. No v1 number depends on it | §5.1 |
| The null without hands | An HW-316 relay module on GPIO7/15/16/17: K1 opens the DUT lead, K2 feeds the DUT around the shunt. Released at boot before anything else; `ROUTe:NULLpath ON\|OFF` is make-before-break with 20 ms / 50 ms settles, and ON checks each step on the shunt; a capture on a shunt off the DUT's current is refused or `q=invalid`, and a relay null beyond 50 µV is not stored; the null path is held 120 s at most | §3.11 |
| Build command | `pio run --project-dir qa/meter -e meter` — the only `pio` command this work may run | §6.3 |

---

## 1. What this document decides, and what it inherits

The three prior notes settled the parts, the pins and the home. This one settles the program. It inherits, without re-arguing:

- **The project is a nested PlatformIO project of the Kleidos firmware repository**, outside the fleet build. It touches neither `variants/` nor the root `platformio.ini`, so none of `check_variants.py`'s 31 invariants and none of `check_layout.py`'s 16 see it.
- **The console is UART0 over the CH343 port.** The native USB port stays unplugged, because a reset on a native-USB board tears down the host's descriptor and the capture dies with it (`carrier-board.md` §7).
- **`ADCRANGE = 0` is the default and the only range a campaign normally needs**, because the floor at 10 µA is offset and noise, not quantization (`ina228-reference.md` §8.2).
- **The burden budget is met**: 0.100 Ω drops 13 mV at 130 mA, half the pack's own sag (`ina228-reference.md` §8.1).

Two facts from this session's own reading of the installed ESP-IDF 6.0.3 tree change what the earlier notes left open, and both are load-bearing below:

1. **There is no high-speed-mode API.** `i2c_device_config_t` carries `scl_speed_hz`, `scl_wait_us` and `disable_ack_check` and nothing else, and no header in `components/esp_driver_i2c/include/driver/` mentions a master code or HS mode. `ina228-reference.md` §10 open question 4 is therefore answered **no**: 400 kHz fast mode (or 1 MHz fast-mode plus on short wiring) is the ceiling, and §3.4's sample rates are sized to it.
2. **`ChargeIntegrator` cannot be reused.** `src/diag/power_meter_logic.h:155-196` takes `addSample( uint64_t tMs, int32_t microamps )`, returns `microampMs()` and caps a gap at `kMaxGapMs = 10000`. Its time unit is **milliseconds**, four orders of magnitude coarser than a 280 µs `TRACE` sample. It is the right class for an AXP192 ADC polled once a second and the wrong one here. `firmware-placement.md` §7's open question is answered: **do not reuse it**, and do not include anything from `src/` at all — a cross-boundary include would be watched by no guard (`check_layout.py` is scoped to `src/`), so a fleet refactor could break the instrument in silence.

---

## 2. Milestone 1 — the LED goes dark

The owner's immediate pain is the factory demo cycling the board's RGB LED. **M1 fixes exactly that and ships nothing else**: it is a complete, flashable image whose entire job is to leave the LED dark, print one identification line, and idle. No I²C, no sampler, no protocol.

### 2.1 Why an explicit "off" frame is mandatory

The part is a WS2812-class addressable LED: the board prints only the word `RGB` beside a 5050 package, and the vendor's own page for this board says, in Chinese, that the LED is "driven by GPIO48; the model is WS2812" (`carrier-board.md` §4).

A WS2812 **latches**. The Worldsemi datasheet describes the mechanism directly: "the first pixel collect initial 24bit data then sent to the **internal data latch**". That latch is powered from the LED's own VDD, which on this board is the 3.3 V rail. Therefore:

| Event | Does the color survive? |
|---|---|
| Power cycle (USB unplugged) | No — VDD drops, the latch clears |
| `RST` button, `esp_restart()`, an esptool auto-reset, a flash upload | **Yes** — EN is pulled low, 3.3 V never drops, the latch holds the demo's last color |

Flashing our firmware over the factory demo is the second row. **Writing nothing leaves the LED lit on whatever color the demo last wrote**, forever, until the cable is pulled. So the first thing the application does is write an explicit all-zero frame.

A second fact from the same note closes the obvious shortcut: a plain level write does nothing, because the part is addressable and "needs a real WS2812 bit stream" (`carrier-board.md` §4). Holding the data line low is a *reset code*, not a color — it re-synchronizes the frame boundary and leaves the latch untouched. **Twenty-four zero bits per pixel are required.**

> **A contradiction worth recording.** `carrier-board.md` §4 reads the board's `RGB` solder jumper as **open** in the owner's photograph, and an open jumper would mean the LED is not wired to any GPIO. The owner's bench observation is that the LED **is cycling**. A thing observed running outranks a jumper read at photo resolution: on this unit the LED net is driven. M1 is what settles it either way — if the LED is already dark because the jumper is open, M1 costs nothing and changes nothing; if it is cycling, M1 stops it and names the pin.

### 2.2 Which GPIO — write to both

The candidates, and why neither is certain:

| Pin | Evidence for | Evidence against |
|---|---|---|
| **GPIO48** | The vendor's header table for this board names P2 terminal 16 "GPIO48 RGB LED"; a second board database agrees (`carrier-board.md` §4) | The shield marking `S3-N16R8` is a clone's, not Espressif's, so the R8 / R8V question is open (`carrier-board.md` §9 q4) |
| **GPIO38** | Espressif moved the LED from GPIO48 to GPIO38 between DevKitC-1 v1.0 and v1.1, for the 1.8 V VDD_SPI of the **R8V** part (`carrier-board.md` §4) | That is a different board; GPIO38 is a free pin on right-rail terminal 10 here |

**Decision: M1 blanks both, GPIO48 first, then GPIO38.** The reasons, in order of weight:

1. **Being wrong costs a flash round-trip.** The whole point of M1 is that one flash ends the pain. A guess that misses means flashing again.
2. **Driving the wrong one is harmless.** Both are plain outputs on this board. Neither is strapping (`GPIO0/3/45/46` are), neither is in-package flash (`GPIO26–32`), neither is octal PSRAM (`GPIO33–37`), neither is USB-JTAG (`GPIO19/20`) nor UART0 (`GPIO43/44`) — the exhaustive ESP-IDF list quoted in `carrier-board.md` §2 and §5. Nothing is wired to either terminal yet.
3. **It costs about 2 ms, once, at boot.** Two frames of 96 bits at 1.25 µs plus two 1 ms reset gaps.
4. **It resolves itself.** M1 prints which pins it blanked; the owner's one-line observation ("it went dark") closes `carrier-board.md` open question 4 for this purpose. **At the end of M1 the list narrows to the one confirmed pin**, and the other reverts to an untouched, high-impedance pin — so a wire landing on right-rail terminal 10 later is never fought by our boot.

One named constant with one definition site, as `carrier-board.md` §4 asks:

```cpp
/// Addressable-LED data lines this image blanks at boot, highest confidence first.
///
/// YD-ESP32-S3 (this board): GPIO48, per the vendor header table and espboards.
/// ESP32-S3-DevKitC-1 v1.1:  GPIO38, where Espressif moved it for the 1.8 V
/// VDD_SPI of the ESP32-S3R8V part.
/// Both are plain outputs here, so blanking the wrong one is free. Narrow this
/// to the single confirmed pin once M1 has run on the bench.
constexpr std::array< gpio_num_t, 2 > kLedDataGpios = { GPIO_NUM_48, GPIO_NUM_38 };

/// Pixels blanked per line. The board carries one; the extra three cost 45 us
/// and clear a chain if one is ever plugged in.
constexpr std::size_t kLedBlankPixels = 4;
```

### 2.3 The exact waveform

The fitted part's revision cannot be read off the board, and the two WS2812B revisions in circulation are **backwards incompatible** on timing. Both tables, from the two Worldsemi datasheets:

| | WS2812B (old) | WS2812B-V5 (new) |
|---|---|---|
| `T0H` | 0.4 µs ±150 ns → **250–550 ns** | **220–380 ns** |
| `T0L` | 0.85 µs ±150 ns → **700–1000 ns** | **580–1000 ns** |
| `T1H` | 0.8 µs ±150 ns → 650–950 ns | 580–1000 ns |
| `T1L` | 0.45 µs ±150 ns → 300–600 ns | 580–1000 ns |
| `RES` | **> 50 µs** | **> 280 µs** |
| Order | GRB, MSB first | GRB, MSB first |

Read the `T1L` row: **the two revisions barely overlap on a `1` bit** (300–600 ns against 580–1000 ns), so no single `1`-bit waveform is comfortably revision-safe. They overlap generously on a `0` bit: `T0H` 250–380 ns, `T0L` 700–1000 ns.

**This is the structural reason M1 is robust and a color feature would not be: an off frame contains only zero bits.** Choose, inside both windows:

| Symbol | Value | Inside old? | Inside V5? |
|---|---|---|---|
| `T0H` | **300 ns** | yes, 250–550 | yes, 220–380 |
| `T0L` | **950 ns** | yes, 700–1000 | yes, 580–1000 |
| bit period | 1250 ns | yes, `TH+TL = 1.25 µs ±600 ns` | yes |
| `RES` | **≥ 300 µs**, implemented as 1 ms | yes, > 50 µs | yes, > 280 µs |

### 2.4 How it is driven, and the ordering trap

Use the RMT transmitter from ESP-IDF's bundled `esp_driver_rmt` with a bytes encoder. No managed component, no vendored library, no `NOTICE` row — which is the standing rule: implement what is needed, vendor nothing.

Pick **`resolution_hz = 20'000'000`** (50 ns per tick) so both durations are exact integers: `T0H = 6` ticks = 300 ns, `T0L = 19` ticks = 950 ns.

```cpp
// Verified against framework-espidf@4.60003.0:
//   driver/rmt_tx.h       rmt_tx_channel_config_t{ gpio_num, clk_src, resolution_hz,
//                                                  mem_block_symbols, trans_queue_depth,
//                                                  intr_priority, flags{ ..., init_level } }
//                         rmt_transmit_config_t{ loop_count, flags{ eot_level, ... } }
//                         rmt_new_tx_channel / rmt_transmit / rmt_tx_wait_all_done
//   driver/rmt_common.h   rmt_enable / rmt_disable / rmt_del_channel
//   driver/rmt_encoder.h  rmt_bytes_encoder_config_t{ bit0, bit1, flags{ msb_first } }
//                         rmt_new_bytes_encoder
//   hal/rmt_types.h       rmt_symbol_word_t{ duration0:15, level0:1, duration1:15, level1:1 }

constexpr rmt_symbol_word_t kBit0 = { .duration0 = 6, .level0 = 1,      // T0H = 300 ns
                                      .duration1 = 19, .level1 = 0 };   // T0L = 950 ns

// Defined only because the encoder config requires both fields. The off frame
// never emits a 1 bit. Before any color is ever written, re-derive this against
// the fitted part's own datasheet revision -- the two revisions disagree (SS 2.3).
constexpr rmt_symbol_word_t kBit1 = { .duration0 = 16, .level0 = 1,     // 800 ns
                                      .duration1 = 12, .level1 = 0 };   // 600 ns
```

**The order of the last two steps is the trap.** In IDF 6.0.3 `gpio_output_enable()` calls `gpio_hal_matrix_out_default()`, so any `gpio_set_direction( pin, OUTPUT )` or `gpio_config()` with an output mode **routes the pad to plain GPIO and detaches whatever peripheral was driving it**. Here that is exactly what we want — but only *after* the frame has gone out. Doing it first silently blanks nothing.

The sequence, per pin:

1. `rmt_new_tx_channel()` with `flags.init_level = 0` and `gpio_num = <pin>`.
2. `rmt_enable()`.
3. Wait **1 ms** — the leading reset code, so the 96 bits that follow are read as a fresh frame whatever the pin was doing before.
4. `rmt_transmit()` of `kLedBlankPixels × 3` zero bytes, `loop_count = 0`, `flags.eot_level = 0` so the line idles low when it ends.
5. `rmt_tx_wait_all_done( chan, 100 )`.
6. Wait **1 ms** — the trailing reset code, which latches the zeros.
7. `rmt_disable()`, then `rmt_del_channel()`.
8. **Only now**, `gpio_config()` the pin as a plain output and drive it **low**.

Step 8's level is a decision, not a detail. Low is the WS2812's idle level, so a driven-low line cannot be misread as data; a floating line can, in principle, pick up enough noise to form a frame. Low costs nothing but leakage. **Both pins are left driven low in M1**, and at M1's close only the confirmed one stays in `kLedDataGpios` while the other is never touched again.

Verify in the Build phase whether the pad is actually driven between `rmt_new_tx_channel()` and `rmt_enable()`; if it is not, the step-3 wait simply moves after `rmt_enable()`, where it already is above.

### 2.5 What M1 is, and how it is accepted

M1's `app_main()` is, in full:

```text
blankStatusLeds();          // SS 2.4, for every pin in kLedDataGpios
printIdentity();            // one line, see below
vTaskDelete( nullptr );     // or an idle loop; nothing else runs
```

It prints exactly two lines and never prints again:

```text
IDN kleidos-meter fw=0.1.0 chip=none stage=m1
LED blanked=48,38 pixels=4 t0h_ns=300 t0l_ns=950 res_us=1000 held=low
```

The bootloader and the IDF log are silenced in `sdkconfig.defaults` (§6.4), so those two lines are the entire output of the image. That is also the shape every later milestone keeps: **one line per fact, nothing unasked-for on the wire**, because a stray log line inside a 32,768-line dump corrupts a capture.

**Acceptance:** once flashed, the LED goes dark and stays dark across a `RST` press and a re-flash. Which pin it was — or that both had to be written — is noted, and `kLedDataGpios` narrows in the same session.

**The LED is not used as a status indicator afterwards, ever.** The instrument's whole job is a clean 3.3 V rail shared with the INA228's analog front end, and a WS2812 refresh is a current burst on that rail for no measurement benefit (`carrier-board.md` §4). Status goes over the serial line, where it can be parsed.

---

## 3. The sampling core

### 3.1 Pins, bus and address

| Signal | GPIO | Carrier terminal | Source |
|---|---|---|---|
| `SDA` | **GPIO4** | left rail 4 | `carrier-board.md` §3, first choice |
| `SCL` | **GPIO5** | left rail 5 | idem |
| `ALE` | **GPIO6** | left rail 6 | idem |
| `VCC` | 3.3 V | left rail 1 | `carrier-board.md` §3 — **never 5 V** |
| `GND` | ground | left rail 22 | idem |

The whole breakout lands on one contiguous run of screws, with the second `3.3` terminal spare, and left-rail terminal 3 (`RST`, chip enable) deliberately skipped.

**Address `0x40`.** The breakout has a 2×4 strap field, rows silkscreened `A0` / `A1`, columns `G · V · S · D`; **the leftmost pad of each row carries solder**, i.e. `A0 = GND`, `A1 = GND`, which is `1000000` = **0x40** by [DS] Table 7-2 — the address TI annotates on its own application diagram ([DS] Figure 8-2). The firmware does not trust that: at bring-up it runs `i2c_master_probe()` across **0x40…0x4F**, the whole 16-address space of [DS] §7.5.1, and reports what answered (`DIAGnostic:I2C:SCAN?`, §4.4). A cold joint shows up as a wandering address, and the instrument says so rather than silently measuring nothing.

**Bus speed: 400 kHz, with a reservation the owner must settle with a meter.** The breakout carries three 0603 resistors by the `VCC`/`SDA`/`SCL`/`ALE` corner, each marked **`01C`** — three, which is the right count for pull-ups on SDA, SCL and the open-drain `ALE`. Read as EIA-96, `01C` is 10 kΩ. *That reading must be confirmed with a meter before it is relied on*, because the number matters:

- The I²C specification caps the rise time at **300 ns in fast mode**, 1000 ns in standard mode and 120 ns in fast-mode plus (NXP UM10204; TI SLVA695 Table 1).
- `t_r ≈ 0.847 · R_p · C_b` (the 0.3 → 0.7 V_DD RC interval).
- At a short-wire `C_b ≈ 50 pF`, a 10 kΩ pull-up gives **t_r ≈ 424 ns** — over the fast-mode limit. At 30 pF it gives 254 ns and just passes.
- The pull-down floor is `3.3 V / 3 mA = 1.1 kΩ`, so there is plenty of room.

**Therefore:** meter the three resistors. If they are 10 kΩ, either keep the I²C wires under about 10 cm or fit **4.7 kΩ in parallel** on SDA and SCL (→ 3.2 kΩ, `t_r ≈ 135 ns` at 50 pF, comfortably inside fast mode). The firmware does not have to be reflashed to work around a bad bus: `BUS <hz>` sets `scl_speed_hz` at runtime and `SYSTem:STATus?` reports it, so dropping to 100 kHz is one command. The default stays 400 kHz because §3.4's `TRACE` rate depends on it.

Also enable the S3's internal pull-up on `ALE` (`gpio_config`, `pull_up_en`) regardless of what the breakout has: if the third `01C` is not on `ALE`, the pin floats and an edge interrupt fires on noise. The internal pull-up is weak (~45 kΩ) but a signal that changes every 280 µs needs nothing stronger.

Two timing facts make the bus deterministic: the INA228 **does not clock-stretch** and **does not support PEC** ([DS] Figure 7-8 notes).

### 3.1.1 One core owns the bus (0.16.4, 0.16.5)

Moved on 2026-11-22 to [`architecture.md`](architecture.md) §2.2, the current design, where it is kept current.

### 3.2 Bring-up and identity

Exactly `ina228-reference.md` §8.6, with the verdicts the firmware reports:

| Step | Action | Refusal if it fails |
|---|---|---|
| 1 | `i2c_master_probe()` over 0x40…0x4F | `METER_ERR: no device (scan found none)` |
| 2 | `CONFIG` ← `0x8000` (`RST`, self-clearing); wait 1 ms (≥ `TPOR` 300 µs, [DS] §6.5) | — |
| 3 | `0x3E` == `0x5449` and `(0x3F >> 4)` == `0x228`, `REV_ID` masked | `METER_ERR: not an ina228 (mfg=0x…. die=0x…)` |
| 4 | `ADC_CONFIG` == `0xFB68` and `SHUNT_CAL` == `0x1000` | `METER_ERR: bad reset defaults (adc=0x…. cal=0x….)` |
| 5 | `DIAG_ALRT` bit 0 `MEMSTAT` == 1 | `METER_ERR: trim memory checksum (MEMSTAT=0)` — **refuse to measure** |
| 6 | `SHUNT_CAL` ← `0x1000` explicitly | — |
| 7 | `CONFIG` ← `0x0000` (`ADCRANGE=0`, `TEMPCOMP=0`, `CONVDLY=0`) | — |
| 8 | `ADC_CONFIG` ← the profile's word | — |

Step 3's mask matters: `0x3F` reads `0x2281`, whose low nibble is a silicon revision field, and masking it is what TI's own ecosystem does. Step 4 is a byte-order self-check as much as an identity check: `0xFB68` decodes to exactly the documented field resets (`MODE=Fh`, `VBUSCT=VSHCT=VTCT=5h`, `AVG=0h`), so getting it back proves MSB-first framing ([DS] §7.5.1.1).

Step 5 is a hard stop, not a warning. `MEMSTAT = 0` means "a checksum error is detected in the device trim memory space" ([DS] Table 7-16) — the calibration constants are suspect and every number the instrument would report is suspect with them.

**A free consistency check, run once at `INITiate` and reported in the header.** Because `CURRENT_LSB` equals `V_LSB / R` exactly in both ranges (§3.3), `CURRENT` counts must track `VSHUNT` counts one for one up to the device's internal rounding. Read both once and report `vs_cur_delta=<n>`; anything beyond ±2 counts means `SHUNT_CAL` is not what we think it is.

**Do not assume pointer auto-increment.** [DS] §7.5.1.1 says the pointer is *retained*, not incremented. `VSHUNT`, `VBUS` and `DIETEMP` sit at `0x04`, `0x05` and `0x06` and look like a burst; they are three separate transactions.

### 3.3 Range and calibration, written once

| Register | Value | Why |
|---|---|---|
| `CONFIG` (`0x0`) | **`0x0000`** | `ADCRANGE = 0`, `TEMPCOMP = 0`, `CONVDLY = 0`, `RSTACC = 0` |
| `SHUNT_CAL` (`0x2`) | **`0x1000`** = 4096 | Exact in both ranges; also the reset value |
| `SHUNT_TEMPCO` (`0x3`) | **`0x0000`** | Left off — §5 |

**`ADCRANGE = 0`, always.** `ina228-reference.md` §8.2 did the arithmetic and the verdict reverses the obvious reading: range 0 covers **10 µA to 1.6384 A in one setting**, range 1 clips at 409.6 mA and so cannot see a charging board at all, and across all 64 entries of [DS] Table 8-2 the noise ratio range1/range0 has **median 1.000**. The finer quantum of range 1 buys nothing because the noise is already 16 LSB peak-to-peak even in range 0 — the quantizer is dithered, so averaging recovers resolution far below the LSB. `SENSe:CURRent:RANGe:INDex 1` remains a manual option for a run guaranteed to stay under 409.6 mA (§4.4), and the firmware **refuses it while a capture is running**, because a switch costs a discarded conversion period, an `RSTACC`, and that range's own offset null.

**`SHUNT_CAL` stays at exactly 4096 and is never corrected.** The ×4 the datasheet demands for `ADCRANGE = 1` exactly cancels the 4× smaller `CURRENT_LSB` that range implies, so 4096 is right in both ranges and a range change is one bit in `CONFIG` plus a host-side factor of four — nothing else ([DS] §8.1.2 Equations 2–3, worked in `ina228-reference.md` §8.3).

**The measured shunt resistance is corrected in software, not in `SHUNT_CAL`.** `ina228-reference.md` §6.2 says pick one and write it down. Three reasons, the third decisive:

1. 4096 is exact, so no gain error enters through the calibration constant. The "round number" route (`CURRENT_LSB = 1 µA` → `SHUNT_CAL = 1311`) costs +0.021 % for no benefit.
2. A `SHUNT_CAL` correction has 1/4096 = **0.024 %** granularity; `shunt_uohm` has **0.001 %**.
3. **The firmware's primary quantity is `VSHUNT` (§3.6), which `SHUNT_CAL` does not touch at all.** A `SHUNT_CAL` correction would apply to `CURRENT`, `POWER` and `CHARGE` and not to the primary path — a split brain in the one place an instrument must not have one.

So `k = 100000 / shunt_uohm` is a single scalar, applied at the print boundary to every derived quantity including anything read out of `CHARGE`, and named in the capture header.

### 3.4 The three profiles

The requirements are five orders of magnitude apart in current **and in time**, so they are three configurations of one instrument, chosen per run. Stated plainly, from `ina228-reference.md` §8.4: **no single `AVG` setting both resolves 10 µA and shows a BLE peak.**

Each `ADC_CONFIG` word below is derived from the [DS] §7.6.1.2 field encodings, bit by bit, not copied:

```text
bits  15-12 MODE | 11-9 VBUSCT | 8-6 VSHCT | 5-3 VTCT | 2-0 AVG
TRACE  1010 (Ah) | 000  (50us) | 011 (280) | 000      | 000 (1)   -> 0xA0C0
STATE  1111 (Fh) | 000  (50us) | 101 (1052)| 000 (50) | 000 (1)   -> 0xF140
SLEEP  1111 (Fh) | 000  (50us) | 111 (4120)| 000 (50) | 010 (16)  -> 0xF1C2
```

| | **`TRACE`** | **`STATE`** | **`SLEEP`** |
|---|---|---|---|
| The job | Shape a burst | Measure a state | Measure deep sleep |
| `MODE` | `Ah` shunt only | `Fh` bus + shunt + temp | `Fh` bus + shunt + temp |
| `VSHCT` | 280 µs | 1052 µs | 4120 µs |
| `AVG` | 1 | 1 | 16 |
| `ADC_CONFIG` | **`0xA0C0`** | **`0xF140`** | **`0xF1C2`** |
| Update period | **280 µs** | **1152 µs** | **67,520 µs** |
| Sample rate | 3,571 Sa/s | 868 Sa/s | 14.81 Sa/s |
| Filter notch `1/(2·TCT)` | 1.79 kHz | 475 Hz | 121 Hz |
| Per-sample noise ([DS] Table 8-2, range 0) | 230 µA p-p ≈ **35 µA rms** | 141 µA p-p ≈ **21 µA rms** | 15.4 µA p-p ≈ **2.3 µA rms** |
| After 1 s of host averaging | 0.59 µA rms | 0.73 µA rms | 0.60 µA rms |
| After 10 s | — | — | **0.19 µA rms** |
| Bus duty at 400 kHz | 95 µs / 280 µs = **34 %** | 406 µs / 1152 µs = **35 %** | 526 µs / 67.5 ms = **0.8 %** |
| Sink | RAM ring, `TRACe:DATA?` | live stream | live stream |

The period formula is `( VBUSCT + VSHCT + VTCT ) × AVG`, because enabled inputs convert **sequentially** and averaging repeats the whole sequence ([DS] §7.3.4), confirmed against Table 8-2's own "output sample period" column.

Three choices in that table deserve their reasons:

**`AVG = 1` in `TRACE`, and this is the "hiding a BLE peak" test.** Averaging replaces N conversions with their mean, so an event shorter than the window is diluted by `event / window`: at `AVG = 16` the window is 4.48 ms and a 1 ms, +100 mA burst shows up as **+22 mA**, once, with no way to tell a long small event from a short large one. At `AVG = 1` the same burst spans three to four consecutive samples, at least two of them wholly inside it. **Average afterwards, in software, where the raw stream is still there to look at.**

**Temperature is converted in `STATE` and `SLEEP`, for +1.2 % of the period.** `ina228-reference.md` §8.4 proposed `MODE = Bh` (shunt + bus) for `SLEEP`; this design raises it to `Fh`. The reason is in §8.5 of that same note: the offset drift is `±10 nV/°C` max = **±0.1 µA/°C** at 0.100 Ω, giving "±0.5 µA over ±5 °C", and the null must be re-taken "whenever the bench temperature has moved". **Die temperature is the quantity that tells you the null has gone stale**, which is the only thing standing between a 10 µA reading and a guess. `VTCT = 0h` (50 µs) raises the `SLEEP` period from 66.72 ms to 67.52 ms — 1.2 % — and the `STATE` period from 1102 µs to 1152 µs. Cheap at the price.

**`TRACE` gets its temperature at the boundaries instead.** 50 µs is +18 % of a 280 µs period, far too much for a quantity that does not move in 280 µs. So at `INITiate`, *before* the first sample, and at `ABORt`, *after* the last, the firmware writes `ADC_CONFIG` with `MODE = 4h` (triggered temperature), polls `CNVRF`, reads `DIETEMP`, and writes `0xA0C0` back. [DS] §7.3.4 says writing `ADC_CONFIG` "will interrupt and restart triggered or continuous conversions that are in progress" — which is precisely why this happens outside the capture and never inside it. The header carries `die_mc_start=` and the `ABORt` line `die_mc_end=`.

Per-state means do **not** need a fourth profile: at 868 Sa/s, one second of `STATE` averages to 0.73 µA rms, and at 3,571 Sa/s one second of `TRACE` to 0.59 µA rms. Both are far finer than any awake budget needs.

### 3.5 Pacing — decided by measurement, not by assumption

Two ways to know a conversion is ready:

- **The `ALE` pin**, with `DIAG_ALRT` `CNVR = 1`, `APOL = 0` (active-low, open-drain), and `ALATCH = 1` for the clip detector (§5). A GPIO falling-edge ISR gives a binary semaphore and the sampler task takes it.
- **A host timer** at the profile's nominal period.

**What the pin does, measured** (2026-11-20, a throwaway probe build that counted edges with the S3's pulse counter and timed them, never merged). `CNVRF` is set "after all conversions and averaging is completed" and clears on reading `DIAG_ALRT` or writing `ADC_CONFIG` ([DS] §7.3.4), and the pin follows `CNVRF`: it goes low at the end of a conversion and stays low until `DIAG_ALRT` is read, **in either `ALATCH` mode**. `ALATCH = 0` does not make it pulse; it only adds sub-microsecond glitches (up to 4 % more falling edges than conversions, all under 1 µs). So a reader that never reads `DIAG_ALRT` sees one edge and nothing after it, which is what every capture before 0.9.0 reported (`pace_edges=1`); a reader that reads it after each edge sees one edge per conversion:

| Profile, `ALATCH = 1`, one `DIAG_ALRT` read per edge | Edges per second | Expected | Missed, extra | Interval sd | `CNVRF` set at the read |
|---|---|---|---|---|---|
| `STATE` (1,152 µs) | 868.0 | 868.06 | 0, 0 | 0.5 µs | 100 % |
| `SLEEP` (67.52 ms) | 14.81 | 14.81 | 0, 0 | 3.5 µs | 100 % |
| `TRACE` (280 µs) | 3,171 | 3,571 | 4,000 in 10 s | 92 µs | — |

The interval mean sat 84 to 97 ppm above nominal: that is the INA228's own oscillator against the S3's crystal, well inside the ±1 % of [DS] §6.5 `FOSC_TOL`. `TRACE` cannot be paced this way: the edge, the wake and a `DIAG_ALRT` read take about 145 µs at 400 kHz, so the loop misses one conversion in nine, and the read would move the retained `VSHUNT` pointer the profile's period is built on (§3.6).

**So, from 0.9.0, the instrument paces by profile and still measures the pin at every `INITiate`:**

```text
TRACE, BURST:  pace = timer, no probe (pace_edges=0 pace_expect=0)
STATE, SLEEP (in the sampler task on Core 1, where the capture runs, 0.16.2):
1. Write the profile's ADC_CONFIG; arm the falling-edge interrupt on ALE (GPIO6),
   then read DIAG_ALRT to clear CNVRF and release the pin (the loop's order).
2. For 8 conversion periods, read DIAG_ALRT after every edge, as the capture will.
3. edges within +/-1 of 8  -> pace = alert   (one sample per edge)
   otherwise               -> pace = timer   (the wire is absent, or the pin rings)
4. Print pace= in the header. Every capture says which it used.
```

On `pace = alert` the loop takes the semaphore, stamps the sample with the time it woke (the conversion's end, before the reads' bus time), reads `DIAG_ALRT` first (which releases the pin for the next conversion and ORs the latched clip bits into `flags=`), then the profile's registers. An edge whose `DIAG_ALRT` read finds `CNVRF` clear had no conversion behind it: it takes no sample and is counted as `stale_edges=` in `FETCh:STATistics?`, so the stream stays one sample per conversion and `samples / (samples + stale_edges)` is the fresh-sample ratio. Starting the loop also reads `DIAG_ALRT` once after enabling the interrupt, because a conversion that ended since the start's last read left the pin low with no edge anyone saw. A wait that times out (two periods and 10 ms) means the pin stopped: the capture falls back to the timer from there and says `pace_fallback=1`; a `DIAG_ALRT` read that fails leaves the pin low and ends the same way.

When `pace = timer`, the header must also carry the caveat, because it is real: the INA228's oscillator is **±0.5 % at 25 °C and ±1 % over temperature** ([DS] §6.5 `FOSC_TOL`), while the S3's timer is crystal-derived. Over a long `TRACE` the two drift apart and the stream will occasionally repeat or skip a conversion, at a rate bounded by that ±1 % (on this part, about 90 ppm: one in eleven thousand). The instrument states the bound rather than hiding it, and §3.9's drift anchors let the host see the actual skew. `pace_err_ppm` in `FETCh:STATistics?` compares the achieved period with the nominal one: on a timer-paced capture it measures whether the loop kept its own deadline, not the INA228's oscillator, and on an alert-paced one it is the oscillator.

The timer path is the dedicated sampler task pinned to **Core 1**, pacing on `esp_timer_get_time()` with a short spin for the sub-millisecond remainder. A 280 µs cadence is below the FreeRTOS tick, so no blocking primitive can express it, and the core is otherwise idle: the console owns Core 0. The spin's one consequence is covered in §3.10. On the alert path the task blocks on the semaphore and `IDLE1` runs.

**Where and in what order the probe runs (0.16.2).** Up to 0.16.1 the console task ran the probe on Core 0, and released the pin before arming the interrupt. Both made the probe count what the capture would not see. A `DIAG_ALRT` read the console made late, because Core 0's other work ran first, let the next conversion end on a pin still low, which makes no edge: the probe counted 6 or 7 where the capture, reading on Core 1, sees every one. And a conversion that ended between the release and the arm left the pin low for the whole window, so the probe counted none. Measured on the NW-00 spike's image (2026-11-21, a diagnostic build timing each edge in the handler against the wake and the read; 40 probes a state): with the radio off, the console's probe took 179 µs a read and every one of the 80 probes counted 8. With the station sending 200 kB/s, its reads took 1.66 ms at the median and up to 3.8 ms, and 6 of 40 probes chose `timer`, 5 of them with no edge at all. With the SoftAP sending 200 kB/s, 20 of 40 chose `timer`, 14 of them with no edge. Armed first but still on Core 0, it chose `timer` in 2 and 6 of 40, counting 6, 7, 9 or 10 edges. Run in the sampler task, the same probe woke 15–17 µs after the edge at the median and read in at most 190 µs in every state, and chose `alert` in all 80 under the radio; with the old order it still counted no edge once in 40 under each radio, and armed first no probe on either core counted none (0 of 360). The capture loop itself woke 9–10 µs after its edge on average and at most 30 µs, in the same states. Since 0.16.2 the console asks the sampler task for the probe and waits for its answer (8 periods and 500 ms); an answer that does not come is `pace=timer`. Since 0.16.3 it asks only a task that is waiting for a start: after a stop the loop has not acknowledged (`107`), the loop could not take the request until it ended and would then run the probe on its own, over whatever the console had set up meanwhile, so the probe says `timer` at once instead, as `INITiate` refuses until the acknowledgement comes.

**`*TST?` lets a conversion end before its probe (0.16.4).** `*TST?` writes STATE's `ADC_CONFIG` and `DIAG_ALRT` and then asks for the probe, whose first `DIAG_ALRT` read came about 0.3 ms after the writes, before any conversion had ended since them. A conversion that ends inside the `ADC_CONFIG` write pulls `ALE` low, and the write clears `CNVRF` behind it ([DS] §7.3.4): the pin stays low, a read with no flag to clear does not release it, and every conversion of the window ends on a pin already low, so the probe counts no edge and bit 32 is set. On the bench (2026-11-21, a diagnostic image reading the pin before and after each write, and timing each edge), 6 of 60 `*TST?` calls failed so, every one with the pin high before the `ADC_CONFIG` write, low after it and still low after the probe's read, and no edge in the window; the other 54 counted 8, the first edge 1.35 ms after the write. `INITiate`'s probe never met it, because `beginCapture()` lets a period pass after its three writes and makes four reads before the probe, so a conversion begun after the writes has set `CNVRF` by the probe's first read. Since 0.16.4 `Sampler::probeAlert()` lets two periods (2.3 ms) pass after its writes: on the same image, 150 of 150 `*TST?` calls passed with 8 edges, 23 of them with the pin pulled low by the write.

### 3.5.1 The sampler task on the bench (0.9.0)

Moved on 2026-11-22 to [`architecture.md`](architecture.md) §3.4, the current design, where it is kept current.

### 3.6 What is read per sample

| Profile | Per sample | Bytes on the wire | At window boundaries |
|---|---|---|---|
| `TRACE` | `VSHUNT` only, **pointer retained** | `S + addr(R) + 3` ≈ 38 bit-times ≈ **95 µs** | `DIETEMP` (triggered), `DIAG_ALRT`, `CHARGE` |
| `STATE` | `DIAG_ALRT` (0.9.0, it releases the `ALE` pin, §3.5), `VSHUNT`, `VBUS`, `DIETEMP` | 120 + 143 + 143 + 120 ≈ **526 µs** | `CHARGE` |
| `SLEEP` | `DIAG_ALRT`, `VSHUNT`, `VBUS`, `DIETEMP` | 120 + 143 + 143 + 120 ≈ **526 µs** | `CHARGE` |

**`VSHUNT`, not `CURRENT`, is the primary.** Three reasons:

1. `CURRENT_LSB = V_LSB / R` exactly in both ranges (§3.3), so `CURRENT` carries no information `VSHUNT` does not.
2. `VSHUNT` is unaffected by `SHUNT_CAL`, so a mis-programmed calibration cannot corrupt the primary number — it can only be *detected*, by §3.2's one-shot consistency check.
3. `VSHUNT` is the raw quantity the software shunt correction multiplies, so the conversion is one division (§3.7) and the offset null subtracts in the same units it was measured in.

**`POWER` is never read.** With `MODE = Ah`, `TRACE` does not produce it at all ([DS] §7.3.2: "the power and energy are calculated after a bus voltage measurement"). In `STATE` and `SLEEP` it is computed from `VBUS` and the corrected current (§3.7), which costs no bus time and sidesteps the one register asymmetry that bites: **`POWER` uses all 24 bits and has no reserved nibble**, unlike `VSHUNT`, `VBUS` and `CURRENT` ([DS] §7.6.1.9). v1 never has to get that right because v1 never reads it.

**`DIAG_ALRT` is read every sample in `SLEEP` and once per window elsewhere**, to catch `MATHOF` (bit 9) — "current and power data may be invalid" while it is set, and it latches until a new conversion or `RSTACC` ([DS] Table 7-16). The `ABORt` line reports `flags=0x…` and a window with `MATHOF` set is reported with `q=invalid`.

**The boundary `CHARGE` reads are the capture's own**, reported as `charge_raw_start` in the header (§4.2) and `charge_raw_end` on the `ABORt` line, against the capture's own `dur_us`. That is deliberate rather than leaving `SENSe:CHARge?` as the only way to see the accumulator: a hand-made `SENSe:CHARge:RESet` / `SENSe:CHARge?` bracket does not coincide with the capture it is meant to describe. Neither boundary resets the accumulator, so a window the operator opened himself is left alone. One consequence is that every `SLEEP` capture now settles §5.1's open question on its own — whether `CHARGE` accumulates at all at 10 µA — instead of needing a dedicated bench run.

The retained pointer is what makes `TRACE` affordable: "If repeated reads from the same register are desired, it is not necessary to continually send the register pointer bytes" ([DS] §7.5.1.1). In ESP-IDF that is `i2c_master_transmit( dev, &ptr, 1, … )` once, then `i2c_master_receive( dev, buf, 3, … )` in the loop. `STATE` and `SLEEP` change pointers, so they use `i2c_master_transmit_receive()` and pay the extra two bytes each.

### 3.7 The conversion arithmetic — integer, exact, no float

The accumulation path carries **no floating point**. Floats appear only at the print boundary, where a `v=<float>` token is formatted. Units are **picovolts** and **nanoamps**, chosen so every datasheet LSB is an exact integer.

**Decoding the wide registers** ([DS] §7.6.1.5–7.6.1.11). The asymmetry is the thing to get right: `VSHUNT`, `VBUS` and `CURRENT` carry a 20-bit value in bits 23-4 with four reserved bits below; `POWER` uses all 24.

```cpp
/// 24-bit register carrying a 20-bit signed value in bits 23-4.
/// Range [-524288, 524287]. C++26: the conversion is two's complement by
/// definition and >> on a signed value is arithmetic.
constexpr std::int32_t decode20( std::uint32_t raw24 ) noexcept
{
    return static_cast< std::int32_t >( raw24 << 8 ) >> 12;
}

/// 40-bit signed CHARGE. Range +/-2^39.
constexpr std::int64_t decode40( std::uint64_t raw40 ) noexcept
{
    return static_cast< std::int64_t >( raw40 << 24 ) >> 24;
}
```

**The LSBs, as exact integers**, from [DS] §7.6.1 and §8.1.2 with `SHUNT_CAL = 4096`:

| Quantity | `ADCRANGE = 0` | `ADCRANGE = 1` | Integer constant |
|---|---|---|---|
| `VSHUNT` | 312.5 nV | 78.125 nV | `kShuntLsbPv` = **312500** / **78125** pV |
| `VBUS` | 195.3125 µV | same | `kBusLsbPv` = **195312500** pV |
| `DIETEMP` | 7.8125 m°C | same | `kTempLsbNc` = **7812500** n°C |
| `CURRENT` | 3.125 µA | 0.78125 µA | `kCurrentLsbPa` = **3125000** / **781250** pA |
| Power | 10 µW | 2.5 µW | `kPowerLsbPw` = **10000000** / **2500000** pW |
| Charge | 3.125 µC | 0.78125 µC | `kChargeLsbPc` = **3125000** / **781250** pC |

**The conversions**, each exact:

```text
shunt_pv  = decode20( vshunt_raw ) * kShuntLsbPv            // +/-1.638e11, int64
i_na      = ( shunt_pv - offset_pv ) * 1000 / shunt_uohm    // see the units note
bus_pv    = decode20( vbus_raw ) * kBusLsbPv                // always >= 0
vbus_uv   = bus_pv / 1000000                                // SOURCE side; see below
die_mc    = static_cast<int32_t>( dietemp_raw ) * 125 / 16  // 7.8125 m C = 125/16
v_dut_pv  = bus_pv - ( shunt_pv - offset_pv )               // VBUS is on the source side
p_uw      = ( v_dut_pv / 1000000 ) * i_na / 1000000000      // int64; see the overflow note
charge_nc = decode40( charge_raw ) * ( kChargeLsbPc / 1000 ) // NOMINAL shunt; see below
```

**The units note.** `I = V / R`. With V in picovolts (1e-12 V) and R in micro-ohms (1e-6 Ω), `I = (pv · 1e-12) / (uohm · 1e-6) = pv/uohm` **microamps**, so `i_na = shunt_pv · 1000 / shunt_uohm` — a single division, and with the nominal `shunt_uohm = 100000` one count of range 0 gives exactly `312500 · 1000 / 100000 = 3125 nA` = 3.125 µA, which is `CURRENT_LSB`. The arithmetic is self-checking.

**The overflow note.** `v_dut_pv × i_na` would reach 3.8e20 at 3.8 V and 100 mA and overflow `int64_t` (max 9.2e18). Reducing to microvolts first keeps it at 3.8e14. Do not be clever here.

**`CHARGE` is the one quantity the firmware does *not* shunt-correct**, and that is deliberate. A full-scale 40-bit count is 5.5e11; converting it to nanocoulombs is 1.7e15, and applying `× 100000 / shunt_uohm` on top of that overflows `int64_t`. Doing the division first throws away precision, and a 128-bit intermediate is a lot of machinery for a register §5.1 keeps only as a probe. So `SENSe:CHARge?` reports the **raw count** beside a conversion at the **nominal** 100 mΩ, the header carries `shunt_uohm`, and the host applies `k = 100000 / shunt_uohm` — one multiplication, in a language with arbitrary precision. Every other quantity is corrected on the device.

**Two voltages, and the token names which.** `vbus_uv` is the bus voltage as the INA228 measured it, on the source side of the shunt; the stream's `v_mv` channel is `v_dut_pv`, the voltage the DUT actually sees. They differ by the burden voltage — 13 mV at 130 mA, 50 mV at 500 mA — so a host that derived power from a `v_uv` whose side was not in its name would get source power where it meant delivered power. `SYSTem:STATus?` therefore prints **`vbus_uv`** and the stream prints **`v_mv`**, and the header's `vbus_side=` says which terminal the bus pin was wired to.

**The burden voltage is free.** `I · R_shunt` is, by construction, `shunt_pv − offset_pv` — the offset-corrected shunt voltage itself. So the voltage the DUT actually sees is `bus_pv` minus that, with no extra arithmetic, **provided `VBUS` is wired upstream of the shunt**. Which it must be anyway: the `VBUS` pin's input impedance is **1 MΩ typ** ([DS] §6.5 `ZVBUS`), so at 3.8 V it draws **3.8 µA**, and wiring it downstream puts that 3.8 µA through the shunt — a 38 % error on a 10 µA reading. On this breakout `VBUS` is its own screw terminal beside `VIN+` and `VIN−`, so the choice is the wiring's; confirm with a continuity check whether the board also ties it to either side (`ina228-reference.md` §8.5, §10 q6). The header records `vbus_side=source|unknown` and the owner sets it with `CALibration:VBUSside SOURce` once the continuity check is done.

The meter's own 640 µA quiescent current is drawn from the **meter's** 3.3 V, not the DUT's: the `VS` pin is separate from `IN+`/`IN−` ([DS] §6.5 `IQ`).

### 3.8 The offset null and the quality token

At 0.100 Ω, **10 µA is 1 µV** — exactly the part's maximum offset spec (`Vos` ±1 µV max, [DS] §6.5). The null is therefore not an optional refinement; it is the measurement.

```text
CALibration:ZERO <seconds>      DUT lead open, SOURCE STILL CONNECTED to IN+ (unmate the
                    pigtail pair; do not unplug the cell -- the null must be
                    taken at the common mode it will be measured at, and in a
                    VBUS-tied-to-IN- wiring it is the only thing that makes the
                    pin's 3.8 uA cancel). Runs the live profile for <seconds>, takes the
                    mean VSHUNT in picovolts, stores it per range in NVS together
                    with the die temperature at which it was taken, and reports
                    the sample standard deviation so a bad null is visible.
```

With the relay module fitted, the lead is opened by K1 rather than by hand and the DUT stays powered through K2 for the whole window: `ROUTe:NULLpath ON`, `CALibration:ZERO`, `ABORt`, `ROUTe:NULLpath OFF` (§3.11). The source stays on `IN+` either way, so the common-mode argument above holds unchanged. Two things differ: `ROUTe:NULLpath ON` checks on the shunt that the lead really opened, and a null taken with K1 commanded open whose offset is beyond 50 µV is printed and not stored, because it is a lead that did not open.

NVS holds, as one blob: `shunt_uohm` (and, since 0.4.0, each range's own value and the calibration date), `off_pv_r0`, `off_pv_r1`, `off_die_mc_r0`, `off_die_mc_r1`, `vbus_side`, and `cal_tag`. Since 2026-11-20 that blob is a **working copy**: the calibration is the registry in `calibration/` (`calibration/README.md`), which survives a flash erase, and `kleidos-meter calibration restore` writes it back. Firmware 0.4.0 reads the version 2 blob of earlier images and rewrites it as version 3 on the next save.

**There is no wall clock on this instrument** — no RTC chip, no Wi-Fi, no NTP. `ina228-reference.md` §8.6 step 10 wants a calibration date; inventing a clock to carry one would be worse than not having it. So `cal_tag` is an **opaque string of at most 24 bytes that the host writes** (`CALibration:TAG 2026-10-26`) and the instrument only ever echoes. It never parses it, never compares it, never acts on it. The date of 0.4.0 (`CALibration:DATE`) is the host's in the same way: checked to be a calendar date, stored and echoed, never compared with anything.

**The quality token**, carried by `SYSTem:STATus?`, every `ABORt` and every header:

| `q=` | Meaning |
|---|---|
| `uncal` | No null for the live range, or `shunt_uohm` still at its nominal 100000 |
| `stale` | Nulled, but `die_mc` has moved more than **±5 °C** since the null |
| `cal` | Nulled for the live range, shunt measured, within ±5 °C |
| `invalid` | `MATHOF` was set during the window, or the shunt was off the DUT's current — `path=both` or `path=bypass` — at any moment of it (§3.11) — the data may be wrong |

The ±5 °C threshold is not arbitrary: `dVos/dT` is ±10 nV/°C max = **±0.1 µA/°C**, so ±5 °C is the **±0.5 µA** that `ina228-reference.md` §8.5 identifies as the instrument's floor. Beyond it the null is no longer the dominant correction it was measured to be.

**The error budget this leaves**, unchanged from §8.5 of that note: the floor is the null's residual, about **±0.5 µA** — **±5 %** on a 10 µA reading, 0.25–5 % across the fleet's 10–200 µA sleep band. Nothing in the `ADCRANGE` or `AVG` choice improves it. The only lever that would is a larger shunt, and 0.25 Ω would drop 125 mV at 500 mA and break the burden rule. If the sleep floor ever proves limiting, that is a **second shunt on a sleep-only rig**, not a change to this one.

**Calibrating the shunt** (superseded on 2026-11-20: the shunt is calibrated against a 100 Ω reference at one current on both ADC ranges, `calibration/README.md`, "The shunt, two points", and `kleidos-meter calibrate` files it in the registry; what follows is the plan of 0.1.0, never run). With a known DC load and a reference DMM in series, `shunt_uohm = shunt_pv / i_reference_ua`. Do it at **two points, a known 20 µA and a known 20 mA**, which also settles open question 4 of `docs/research/external-power-meter-options.md` §7 — whether the INA228's input bias current is significant at microamp ranges. Two points agreeing on one resistance is the answer; two points disagreeing is a bias current, and the disagreement measures it.

### 3.9 Storage — a ring for the two fast profiles, a stream for the other two

This is forced by arithmetic, not preference. The console runs at **6 Mbps** (§3.9.1), so the line carries **600,000 B/s**, and a `METER t=… ch=i_ua v=…` line is bounded at **48 bytes** — derived in `sampler.h` from the longest each field can print, never guessed.

| Profile | Rate | One channel costs | Verdict |
|---|---|---|---|
| `BURST` | 9,898 Sa/s | 475 kB/s (79 %) | **Capture to RAM**: no headroom for a second channel |
| `TRACE` | 3,571 Sa/s | 171 kB/s (29 %) | **Capture to RAM**: fits the line now, but the ring costs nothing and keeps a burst's shape off the host's scheduler |
| `STATE` | 868 Sa/s | 41.7 kB/s (7 %) | Streams. Three channels = 21 %, where 921600 baud refused them |
| `SLEEP` | 14.81 Sa/s | 0.71 kB/s | Streams, three channels = 2.1 kB/s |

**The ring.** The timestamps are implicit — the period is fixed — so a sample is **4 bytes**: the sign-extended 20-bit `VSHUNT` count in an `int32_t`. It lives in the module's 8 MB of PSRAM, which leaves half of it free:

```text
kTraceSamples = 1048576  ->  4 MiB  ->  293 s at 3,571 Sa/s, 106 s at 9,898 Sa/s
```

#### 3.9.1 The line is 6 Mbps, and that is measured

The console is **UART0 on GPIO43/44 behind the carrier's CH343P bridge**, not the S3's native USB (`carrier-board.md` §7), so the baud rate is a real divider and not a USB formality. It was 921600, then 2 Mbps; a timed `TRACe:DATA?` showed 2 Mbps delivering 196 kB/s — 98 % of its own ceiling, i.e. **saturated**, so the line and not the firmware's formatting was what bounded a dump.

The CH343P is rated to 6 Mbps and the S3's fractional divider puts 6 Mbps 0.08 % off exact from the 80 MHz APB, inside any UART's tolerance. Measured on a 7.85 MB dump of 209,936 samples: **582.6 kB/s, 97 % of the line, every one of the 209,936 sample lines parsing in full, zero non-ASCII bytes, and timestamp gaps forming the exact arithmetic progression the declared period predicts.** A dump that would have taken 195 s at 921600 takes 13 s.

Allocated at `INITiate` with `heap_caps_malloc( n, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT )`. The board JSON for this module class declares `maximum_ram_size: 327680`, so 128 KiB is a large but plausible slice of a radio-free image. **The firmware does not guess**: on failure it halves and retries down to a floor of 4096 samples, then reports the size it actually got as `ring=<n>` in the header. A short allocation is a documented fact of the capture, never a silent truncation.

**Drift anchors.** Every 1024 samples the firmware stores `{ uint32_t n; uint64_t t_us; }` — 1,024 anchors over a full ring of 1,048,576 samples, 16 KiB, 0.1 % overhead — and a forced one at the sample after a failed read, at most 16 a capture (0.15.0), in an array sized for both, so a forced anchor never pushes out a regular one. They let the host reconstruct real timestamps by interpolation and **see** any stall or oscillator skew instead of trusting the nominal period. `TRACe:DATA?` prints them interleaved as `A n=<idx> t=<us>`.

**Marks.** `TRACe:MARK "<label>"` stores `{ uint32_t n; char label[16]; }`, up to 16 of them, so a trace aligns to an event on the DUT's own console. Printed in `TRACe:DATA?` as `K n=<idx> label=<text>`.

**No PSRAM in v1.** The module is marked `S3-N16R8`, but assuming octal PSRAM where the part is quad silently degrades to no PSRAM and crashes (the Kleidos firmware learned that on another board), so the memory mode must be read off the installed board JSON rather than guessed. 9.18 s of internal RAM answers every v1 question; PSRAM is a v2 lever with a bring-up cost attached.

**Back-pressure is refused, never absorbed.** Before starting a stream the firmware computes the byte rate of the requested channel set and **refuses** if it exceeds 80 % of the line rate:

```text
METER_ERR: stream too fast (needs 110592 B/s, line 92160 B/s, cap 73728 B/s)
```

And at runtime it counts what `uart_write_bytes` could not place as `dropped=`, rather than assuming a write took everything: a short write is the default failure, so use what a write or read returns and count what it could not place.

### 3.10 Tasks, cores and the watchdog

| Task | Core | Priority | Job |
|---|---|---|---|
| `meter-sample` | **1** | 10 | Pace, read, decode, append to the ring or hand to the writer |
| `meter-console` | **0** | 5 | Read UART0 lines, dispatch, print |

The split mirrors the fleet's own: the latency-critical loop gets a core to itself, and everything that blocks runs on the other (`AGENTS.md` §8). **The interrupts follow the sampler (0.14.0).** ESP-IDF allocates an interrupt on the core that installs it, and `app_main()` runs on Core 0, so until 0.14.0 the I2C master's ISR and the ALERT pin's GPIO ISR ran beside UART0's at 6 Mbps while the task they wake runs on Core 1: every transfer's completion crossed cores. `app_main()` now runs the bring-up and `Sampler::begin()` on Core 1 through `esp_ipc_call_blocking()` (the IPC task's stack raised to 4 KiB for it), and only the console's UART interrupt stays on Core 0 with the console. Should the IPC call be refused, the same steps run on Core 0 as before. From 0.16.5 every I2C transaction is made on the core the bus was opened on, the console's through the IPC task (§3.1.1). The sampler never prints; it hands finished samples to the console task through a `FreeRTOS` queue when streaming, so a slow line can never stall the cadence — it can only raise `dropped=`.

**Supervision (0.14.0).** Both tasks subscribe to the task watchdog from their own task, and a task that goes 5 s unfed panics the chip (`CONFIG_ESP_TASK_WDT_PANIC`): the reboot releases every relay to the measuring circuit and `SYSTem:HEALth?` names it `reset=task_wdt`. The console feeds at most every 100 ms from its loop, from every write (a dump or a stream is its longest work), from the acquisition loop and from a settle; the sampler feeds while it waits for a start (in 1 s slices) and, in a capture, every 200 ms' worth of samples by count, so the loop reads no clock for it. The watchdog sees a task that stops; the console also watches a sampler that runs and takes nothing: a running capture whose count has not moved for 2 s (thirty periods of `SLEEP`, the slowest), queues `107` once and sets QUEStionable TIME. `DIAGnostic:WATChdog:TEST` stops the console's feeding to prove the path on the bench. A watchdog reboot under a held null path is the exposure `README.md` describes for any reset, bounded by the relays' release times.

**The watchdog trap.** A spinning sampler on Core 1 starves `IDLE1` and the task watchdog fires. Set `CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1=n` in `sdkconfig.defaults` — verified to exist in `framework-espidf@4.60003.0` — and leave the Core 0 check and the interrupt watchdog alone. The spin only exists on the `pace = timer` path; on the `pace = alert` path the task blocks on a semaphore and `IDLE1` runs normally.

A queue or semaphore that both tasks may reach is **created before the consumer task exists**, never by a bare `if ( handle == nullptr )` create — both cores can pass that check and build it twice into one control block (`AGENTS.md` §8).

### 3.10.1 The hot path in IRAM (0.16.1)

Moved on 2026-11-22 to [`architecture.md`](architecture.md) §5.2, the current design, where it is kept current.

### 3.11 The relays — the null without hands

The one step of a campaign that needed hands was §3.8's null: the DUT lead open, the source still on `IN+`. An HW-316 four-relay module now does it, and does it without the DUT losing power. `src/relay.h` holds the whole design as one table and a set of `constexpr` functions; the console runs it.

**The module.** Four SRD-05VDC-SL-C Form C relays (COM / NC / NO, 10 A at 30 VDC), each behind a 1 kΩ input resistor, an IN LED, a PC817 opto and an S8050 driver with a 1N4148 flyback diode. `VCC` on carrier terminal 21 (`5V`), `GND` on 22 (`G`), the `JD-VCC` jumper fitted. The inputs are **low-level triggered**: driven low, an input sinks about 2 mA through the IN LED and the opto LED in series and energizes its coil; driven high at 3.3 V, or left floating, the coil stays off, because those two LEDs need about 3 V between them and 5 − 3.3 V cannot light them.

| Channel | Input | GPIO | Left-rail terminal | Role | COM | NC | NO |
|---|---|---|---|---|---|---|---|
| K1 | `IN1` | 7 | 7 | `lead` | `VIN−`, the shunt's DUT end | DUT battery + | — |
| K2 | `IN2` | 15 | 8 | `bypass` | cell +, the `VIN+` node | — | DUT battery + |
| K3 | `IN3` | 16 | 9 | `spare` | — | — | — |
| K4 | `IN4` | 17 | 10 | `spare` | — | — | — |

The table is the HW-316 rig's, which the `meter` image drove until 0.7.0. From 0.8.0 the one image drives the rig on the bench since 2026-11-20 (`hardware/rig.kicad_sch` rev D), the `bypass` channel is one LCA717 PhotoMOS of a MikroE Relay 2 Click on the same GPIO15 and the same two nodes, active high, and of the HW-316 only `IN1` is wired: `IN2`, `IN3` and `IN4` are not. From 0.13.0 the spares `k3` and `k4` are marked unwired (`ChannelSpec::wired`), so GPIO16 and GPIO17 are never driven (`relay.h` `kBypassChannel`, `kChannels`; `docs/relay2-click-wiring.md`); from 0.16.6 both are parked at boot, inputs with the internal pull-down (§6.5).

GPIO4/5/6 are the INA228's `SDA`/`SCL`/`ALE` (§3.1), so the relays take the next four terminals of the same rail; all four are free on this board (`carrier-board.md` §2), and GPIO15/16 are free of their 32 kHz crystal role because the image runs the RTC on its internal RC (`CONFIG_RTC_CLK_SRC_INT_RC`). The negatives are not switched.

**Released is the safe state, and the wiring makes it the measuring one.** K1's NC contact is in series with the shunt and K2's NO contact is across it, so every coil off is the DUT on the shunt with the bypass open — the circuit before the module was fitted. That is what a meter reset, a reflash, a meter with no firmware, or a pin the driver failed to configure all leave behind. The two contacts make four circuits, which every relay line names:

| `path=` | K1 | K2 | The DUT is fed | The shunt carries |
|---|---|---|---|---|
| `shunt` | closed | open | through the shunt | everything: the measuring circuit and the boot state |
| `bypass` | open | closed | around the shunt | nothing of a DUT that is running: **the null**, and no place for a capture |
| `both` | closed | closed | both ways | a fraction, so every reading is `q=invalid` |
| `none` | open | open | not at all from the battery | nothing, which is what the DUT draws: a capture there is valid |

`relay::shuntCarriesAll()` is that last column made a rule: only `shunt` and `none` are readings of the DUT.

**The boot order.** `gRelays.begin()` is the first statement of `app_main()`, ahead of the LED frame of §2. For each pin it writes the GPIO output latch high **before** `gpio_config()` makes the pad an output: in 4.60003.0 `gpio_set_level()` writes the output register whether or not the pad is enabled, and `gpio_output_enable()` inside `gpio_config()` only routes the plain GPIO signal to the pad, enables the driver and selects the GPIO function (`components/esp_driver_gpio/src/gpio.c`) — so the pad's first driven level is the high written beforehand, never the register's reset value. From that line on every pin is push-pull and never floating. Before it the pins are not driven by this image at all; the module reads a floating input as off, and the ESP32-S3 datasheet's Table 2-1 gives none of the four a pull-down at reset. Its Table 2-2 gives all four a **60 µs low-level glitch at power-up**, a real low on an input that energizes on low but far shorter than the milliseconds a coil needs to pull in. The first-power check in `rig-wiring.md` §8.5 (every IN LED dark, no click, through a reset and a power-up) is what confirms both on the bench. The pads are `GPIO_MODE_INPUT_OUTPUT`, so `ROUTe:STATe?` reads each one back against the level it is driven to (`pads=`). A pin `gpio_config()` refuses leaves the bank not ready: the boot prints `METER_ERR: relays unavailable (gpio config failed at boot)`, `SYSTem:INFormation?` says `relays=0`, and every order is refused rather than reported as done.

**Orders are planned before they are driven.** Each command becomes a plan of at most two steps, each a coil change and the settle time after it. The plans and the interlocks are `constexpr`, and the orders this module exists for are `static_assert`s beside them, so a change that breaks make-before-break fails the meter's own build:

| Order | From rest (`path=shunt`) | Steps |
|---|---|---|
| `ROUTe:NULLpath ON` | → `bypass` | read the shunt; energize K2 (bypass closes), settle, read; energize K1 (lead opens), settle, read — each reading judged below |
| `ROUTe:NULLpath OFF` | from `bypass` → `shunt` | release K1 (lead recloses), settle; release K2 (bypass opens), settle |
| `ROUTe:LEAD OPEN\|CLOSE` | | K1 alone |
| `ROUTe:BYPass ON\|OFF` | | K2 alone |
| `RELAY 3\|4 ON\|OFF` | | a spare alone; `RELAY 1\|2` is refused by name, so each role has one spelling and one set of interlocks |

From any other state `ROUTe:NULLpath` runs only the steps still missing, in the same order, and no step of either direction ever passes through `path=none`.

**The settle times.** There is no contact sense on the module, so every order waits out the relay's own bounds before it answers, and these are bounds, not observations; what the rig measured against them, every move well inside its wait, is `docs/relay-timing.md`:

| | Value | Source |
|---|---|---|
| Operate time | 10 ms max | Songle SRD sheet; LCSC and JLCPCB listings of part C35449 |
| Release time | 10 ms max used | Songle's sheet says 5 ms, the LCSC and JLCPCB listings 10 ms; the larger is used |
| Flyback diode | 30 ms allowance on a release | The bounds are for a bare coil; a plain diode across the coil gives "the slowest" current decay (TE Connectivity, coil suppression), and no source gives the figure for this part. Three times the release bound. Measured on 2026-11-21: the release, diode and all, took 11.20 ms (`docs/relay-timing.md`) |
| Bounce | 10 ms margin | No source states one; the margin equals the operate bound. The record of 2026-11-21 shows the operate's step at 1.96 to 2.52 ms and the release's at 11.20 ms, bounce included (`docs/relay-timing.md`) |
| Settle after energizing | **20 ms** | `kOperateSettleMs` |
| Settle after releasing | **50 ms** | `kReleaseSettleMs` |
| Relay 2 Click bypass (the one image from 0.8.0; `meter_relay2` before) | **10 ms** each way | `kSsrOperateSettleMs`, `kSsrReleaseSettleMs`: IXYS's 3 ms LCA717 bound at 5 mA, doubled for the Click's unstated LED current, plus 4 ms; the TRACE run of `docs/relay2-click-wiring.md` step 7 measured 0.56 to 1.12 ms on and 0.56 to 0.84 ms off on 2026-11-21 (`docs/relay-timing.md`) |

The release wait is the one make-before-break rests on when it opens the bypass: `ROUTe:NULLpath OFF` must not open K2 before K1's NC contact has reclosed. `ROUTe:NULLpath` is therefore about 100 ms each way, ON's checks included. Every reply's `settle_us` is the time the order actually took, on `esp_timer`. Two bench runs prove the bounds (§8): a `TRACE` across `LIVE FORCE` orders, which shows each contact's real time against its mark, and a relay null against a hand-removed-lead null, agreeing within 0.1 µV.

**The checks: what `ROUTe:NULLpath ON` sees.** A commanded state is not evidence: a module with no 5 V (`carrier-board.md` §6 makes that the likely first-power case), a loose `IN` wire or a coil below its 3.75 V pull-in leaves every relay where it was while the console reports the order done — and a null taken through such a "null path" measures the DUT's own current through the shunt and would be stored as the offset. So `ROUTe:NULLpath ON`, when no capture is open and the INA228 is up, reads the shunt before the first step and after each one. Each reading is a 16.8 ms average (continuous shunt-only, 1052 µs × 16, read 20 ms after `ADC_CONFIG` is written, the profile's word written back afterwards: continuous rather than triggered, so the accumulators stay valid), and its *share* is the reading less the stored null for the live range. The verdicts are `constexpr` in `relay.h`, with `static_assert`s on the T-Deck's own numbers:

| Step | Passes when | Why that bound |
|---|---|---|
| K2 closes | the share falls to **≤ ¾** of the share before | K2's branch is a contact and two short wires, the shunt's 0.100 Ω plus a contact and one wire; with every contact at its 100 mΩ maximum against the other at nothing, a closed K2 leaves the shunt 55 % at worst |
| K1 opens (or any step that lands on `path=bypass`) | the reading is **≤ 50 µV**, and — for K1's step, when the first share was judged — the share is **≤ ½** of the one before | 50 µV is seven times the largest null this rig has measured (−6.85 µV, 2026-10-27) and 500 µA through the shunt. The half test catches a K1 that never moved but whose leftover share happens to sit under the limit |
| Judged at all | the first share is **≥ 200 µV** (2 mA) | below it neither move stands clear of the offset and of the DUT's own variation between readings |

A failed step releases the path again, make-before-break, and refuses with every reading it rests on (`bypass check failed`, `lead check failed`). A passed order carries its verdict in the relay tokens as `check=`: `ok` (both moves seen), `unverified` (under 2 mA: only the 50 µV limit applied), `skipped` (`ROUTe:NULLpath ON,LIVE` under a capture, which reads nothing), or `-` (any other order, or boot). `ROUTe:NULLpath OFF` is not checked: both of its steps are spring releases, which need no coil supply to happen.

The last line of defense is in `CALibration:ZERO`: a window opened with K1 commanded open (`path=bypass` or `path=none`) whose offset comes out beyond 50 µV prints its numbers and then `METER_ERR: null not stored (…)`, and neither RAM nor NVS takes it. The hand null (`path=shunt`) is not bounded: the relays cannot see that lead.

**The hold.** A release nobody commanded — a meter reset, a panic, a brown-out, the meter's USB lost, a port opened with RTS and DTR in the wrong order — drops both coils at once, and with the null path held K2's NO can open before K1's NC recloses. So the null path is held at most `kNullPathHoldS` = **120 s**: armed by any order that ends on `path=bypass`, re-armed whenever a window closes. When it expires the console loop runs `ROUTe:NULLpath OFF` itself and prints nothing; every relay reply says `hold_s=`, and `ROUTe:STATe?` counts the releases in `hold_released=`. From 0.13.0 an open lead is held too, and from 0.16.0 a bypass across a closed lead, and no capture suspends the hold any longer, only a null window that samples (§3.16).

**The interlocks.**

1. **No order disconnects a powered DUT unless it says `FORCE`.** A step that takes the circuit into `path=none` from a powered one — `ROUTe:LEAD OPEN` with the bypass open, `ROUTe:BYPass OFF` with the lead open — is refused: `METER_ERR: order disconnects the dut (k1 open, k2 open: add FORCE)`. The test is a transition *into* `none`, so a spare switched while a cold power cycle has the DUT disconnected is not refused again. `ROUTe:LEAD OPEN,FORCe` with the DUT's USB port cut at the hub is the hands-free cold power cycle.
2. **No contact moves under a running capture unless the order says `LIVE`**, and none moves under a running `CALibration:ZERO` even then, because a null assumes one circuit for its whole window. A `LIVE` move records a mark per coil at the sample index it was driven at, and prints it exactly as `TRACe:MARK` prints it (`METER_OK: mark=k1:open n=… t=…`) before the relay reply: a streamed capture has no `TRACe:DATA?` to replay a `K` line from, and the marks share `TRACe:MARK`'s table of `kMaxMarks`. The reply says `live=1` and how many marks it stored. An order that changes nothing is answered with the state whatever is running.
3. **A shunt off the DUT's current is never measured silently.** `INITiate` is refused on `path=both` and on `path=bypass` unless it says `FORCE`, naming the order that gives the shunt the whole current back (`ROUTe:BYPass OFF`, `ROUTe:NULLpath OFF`). `CALibration:ZERO` is refused on `path=both` with no override, because nothing makes that a null. A `LIVE` move that would take a running capture's shunt off the DUT's current (`relay::bypassesShunt()`: every `ROUTe:NULLpath` order, `ROUTe:BYPass ON`) needs `FORCE` too, because the header has already gone out with its `q=`. A capture that was off the DUT's current at any moment then says `q=invalid` on its `ABORt`, in `SYSTem:STATus?` and in every relay reply — and in its header too when it was so at `INITiate`. `SYSTem:STATus?` and `SYSTem:INFormation?` say `q=invalid` for as long as the shunt is off the DUT's current.
4. **Every record says which circuit it was taken in.** Each capture header carries `METER_HDR relays=1 k1=… k2=… k3=… k4=… path=…` (§4.2), and both `CALibration:ZERO` lines carry the same tokens as the window's start found them — the result line too, so an order between the window's last sample and `ABORt` cannot rewrite the circuit the null was taken in. `path=bypass` there is a null taken with the lead opened by K1, `path=shunt` one that rests on a lead opened by hand, which the relays cannot see, and `check=` what `ROUTe:NULLpath ON` saw of the path.

**Nothing waits on a relay but the console.** Orders run on the console task (Core 0). A settle wait drains the stream queue every millisecond instead of sleeping through it, so a `LIVE` order never holds back a capture's samples, and the sampler task on Core 1 never sees a relay at all. The checks read the device only with no capture open, so they never move the register pointer out from under a `TRACE`. The console is the bank's only user after boot, so no order can interleave with another.

**The RTC slow clock stays on the internal RC.** GPIO15 and GPIO16 are the 32 kHz crystal pins; `sdkconfig.defaults` states `CONFIG_RTC_CLK_SRC_INT_RC=y` (and `test_meter_sdkconfig_reaches_the_build` checks it arrives), and `relay.cpp` refuses to compile with an external crystal or oscillator selected, which would put a clock on the bypass relay's input.

**What the relays cannot do.** They cannot see whether the DUT is also fed by USB. They cannot order a release they did not command: while the null path is held, a meter reset releases both coils at once, K2's NO contact can open before K1's NC recloses, and the DUT can lose its battery for a few milliseconds; a slow fall of the 5 V can even release K2 first and leave `path=none` with the firmware still reporting `bypass`. The hold bounds the exposure, and a host should still close the path in a `finally`. They cannot see below 2 mA (the checks above). And each energized coil draws about 71 mA from the carrier's 5 V terminal (Songle's coil chart: 70 Ω, 71.4 mA), so holding the null path costs about 145 mA there — which assumes the terminal carries 5 V with only the USB cable attached, measured loaded as `carrier-board.md` §10 check 6 asks. The module's 5 V must also never outlive the meter's 3.3 V: an unpowered ESP32-S3 clamps every `IN` toward ground through its protection diodes (`carrier-board.md` §6).

### 3.12 The capture statistics (0.10.0)

Moved on 2026-11-22 to [`architecture.md`](architecture.md) §3.8, the current design, where it is kept current.

### 3.13 The triggered ring, marks in order, a capture that ends itself (0.11.0)

Moved on 2026-11-22 to [`architecture.md`](architecture.md) §3.9, the current design, where it is kept current.

### 3.14 Self-test, health and status (0.12.0)

Moved on 2026-11-22 to [`architecture.md`](architecture.md) §7, the current design, where it is kept current.

### 3.15 The lead cycle, the open lead's hold, the unwired spares (0.13.0)

Moved on 2026-11-22 to [`architecture.md`](architecture.md) §6.2, the current design, where it is kept current.

### 3.16 The hold is the teardown, and a reset says what it released (0.16.0)

Moved on 2026-11-22 to [`architecture.md`](architecture.md) §6.3, the current design, where it is kept current.

---

## 4. The host protocol

### 4.1 Grammar

Copied from the console conventions the QA harness already parses — `key=value`, one fact per token, `_OK:` / `_ERR:` prefixes, an `_END` sentinel on multi-line replies — with `TELEM` as the model (`docs/testing/serial-debug-commands.md:571-575`). The harness's parser is `parse_fields()` in `qa/qa_lib/dut/debug_console.py:83`, whose regex is `\b(\w+)=(\S+)` — so **every token must be `word=non-space`**, and no value may contain a space. That is a contract, not a style.

Rules the firmware obeys without exception:

1. Every line ends with `\n`. Nothing is ever printed that was not asked for.
2. Logging is compiled out (`CONFIG_LOG_DEFAULT_LEVEL_NONE`) and the IDF console is off (`CONFIG_ESP_CONSOLE_NONE`); the firmware owns UART0 directly through the UART driver. **A log line inside a 32,768-line dump would corrupt a capture**, so there are no log lines.
3. No echo, no prompt, no line editing. The reader accumulates bytes to `\n` or `\r` and dispatches from a lookup table of `{ name, handler }`, not a switch.
4. A command is refused with one `METER_ERR: <reason>` line and changes nothing.
5. Commands that reconfigure the device (`SENSe:PROFile`, `SENSe:CURRent:RANGe:INDex`, `SYSTem:COMMunicate:I2C:FREQuency`, `CALibration:ZERO`, `CAL …`) are **refused while a capture is running**, naming the command that releases it: `METER_ERR: capture running (ABORt first)`. A relay order that would move a contact is refused the same way unless it says `LIVE`, and one that would take that capture's shunt off the DUT's current unless it also says `FORCE` (§3.11).

### 4.2 The header — what makes a capture self-describing

`INITiate` prints one block before the first sample. Every calibration number in it is an **integer**, so the file carries no rounding of its own.

```text
METER_HDR_BEGIN
METER_HDR fw=0.1.0 chip=ina228 addr=0x40 mfg=0x5449 die=0x228 rev=0x1
METER_HDR config=0x0000 adc_config=0xA0C0 shunt_cal=4096 range=0
METER_HDR diag_cfg=0xC001 alatch=1 sovl=0x7FF8 suvl=0x8008
METER_HDR mode=0xA vbusct_us=0 vshct_us=280 vtct_us=0 avg=1 period_us=280
METER_HDR pace=timer pace_edges=0 pace_expect=8 osc_tol_ppm=10000
METER_HDR shunt_lsb_pv=312500 bus_lsb_pv=195312500 temp_lsb_nc=7812500
METER_HDR current_lsb_pa=3125000 power_lsb_pw=10000000 charge_lsb_pc=3125000 charge_raw_start=0
METER_HDR shunt_uohm=100000 shunt_measured=1 offset_pv=-312000 off_die_mc=28750 vbus_side=source
METER_HDR cal_tag=2026-10-26 q=cal die_mc_start=29500 vs_cur_delta=0
METER_HDR scl_hz=400000 sink=ring ring=32768 anchor_every=1024 t0_us=1234567890
METER_HDR relays=1 k1=lead:closed k2=bypass:open k3=spare:unwired k4=spare:unwired path=shunt check=-
METER_HDR_END
```

Everything a host needs to turn counts into amps is in those lines, with no reference to this document: the LSBs, the live calibration, the range, the profile word, which pacing source produced the timestamps, and the oscillator tolerance that bounds a timer-paced stream's skew. `q=` says how far to trust it.

Three tokens there are worth naming:

- **`charge_raw_start`** is `CHARGE` at the opening boundary, **read and not reset**, so a window the operator opened with `SENSe:CHARge:RESet` is not stomped on. `ABORt` prints `charge_raw_end` beside it, and the difference over the capture's own `dur_us` is the hardware-integrated mean current of `ina228-reference.md` §8.4 — the number that makes this rig beat a PPK2 on a sleep figure. It is `-` when the read failed.
- **`shunt_measured`** is 0 until `SENSe:CURRent:SHUNt` has written a measurement. It is what `q=uncal` keys off, *not* the stored resistance: the single most likely outcome of the two-point calibration on an R100 part is exactly 100000 µΩ, and that has to be able to reach `q=cal`.
- **`off_die_mc`** is the die temperature the null was taken at, and it prints the word `unknown` when that read failed — the same spelling `die_mc_start` uses, because a sentinel printed as a number reads as −2.1 million degrees.
- **`path`** is the relay circuit the capture was started in (§3.11): `shunt` for a measurement, `none` for a disconnected DUT whose zero the shunt reads, and `bypass` or `both` — only ever reached by `INITiate:FORCe` — for a shunt that carries none or a fraction of a running DUT's current, in which case `q=` above says `invalid`. `check` is what `ROUTe:NULLpath ON` saw of that path. `relays=0` means the inputs could not be configured at boot and the states are the released ones the module reads from floating pins.

### 4.3 Sample lines

**Streaming (`STATE`, `SLEEP`)** uses the `TELEM` shape verbatim, so the existing parser works unchanged:

```text
METER t=<us> ch=<name> v=<float>
```

| Channel | Unit | Default in `STATE` | Default in `SLEEP` |
|---|---|---|---|
| `i_ua` | µA, signed | yes | yes |
| `v_mv` | mV at the sense point | | yes |
| `p_mw` | mW delivered to the DUT | | |
| `die_c` | °C | | yes |

`INITiate` selects the set, exactly as `TELEM INITiate [period_ms] [ch1,ch2,…]` does. The defaults are chosen to sit inside §3.9's byte-rate cap.

**`TRACE`** prints nothing while capturing. `TRACe:DATA?` then replays it in the same shape, with the implicit timestamps resolved:

```text
METER_DUMP count=32768 t0_us=1234567890 period_us=280 ring=32768
A n=0 t=1234567890
K n=4096 label=ble_start
METER t=1234567890 ch=i_ua v=-3.125
...
METER_DUMP_END
```

A full ring is 1,048,576 lines of about 38 bytes — 40 MB, **68 s** on the wire at 6 Mbps, for a 293 s capture. `TRACe:DATA? RAW` is the compact alternative: sixteen counts per line, about 6× smaller, **11 s**:

```text
METER_DUMP count=1048576 t0_us=… period_us=280 fmt=raw20
R n=0 c=-1,0,-1,2,1,0,-1,-1,0,1,0,-1,1,0,0,-1
...
METER_DUMP_END
```

Note `c=` carries a comma-separated list with **no spaces**, which keeps it a single `\S+` token and so keeps `parse_fields()` working on it.

**Anchors and marks are emitted per row, not per row start.** A `RAW` row covers sixteen samples, so every `A` and `K` whose index falls *inside* the row goes out before it; testing only the row's first index dropped every anchor of a `TRACe:DATA? RAW <from>` with `from % 16 != 0`, and about fifteen marks in sixteen always — a mark is recorded at whatever sample index the operator typed `TRACe:MARK` at, and it is the only thing that aligns a trace to an event on the DUT's own console. Anchors with an index **below** `from` are emitted once up front, so a partial dump still carries the drift reference its timestamps rest on.

### 4.4 The command set

| Command | Reply |
|---|---|
| `SYSTem:INFormation?` | `IDN kleidos-meter fw=<v> chip=ina228 addr=0x<hh> mfg=0x<hhhh> die=0x<hhh> rev=0x<h> shunt_uohm=<n> cal_tag=<s> q=<t> relays=<b>` — `relays=1` advertises the relay module's commands (§3.11), `0` that its inputs could not be configured |
| `SYSTem:STATus?` | `METER running=0\|1 sink=ring\|stream\|null\|idle profile=<p> period_us=<n> pace=<p> pace_fallback=<b> range=<r> samples=<n> dropped=<n> read_err=<n> i_na=<f> vbus_uv=<n> die_mc=<n> die_age_us=<n> flags=0x<hhhh> scl_hz=<n> q=<t>` — the idle line reports `running=0 sink=idle period_us=0`, like `TELEM?`. `sink=` is what tells a **finished capture awaiting `ABORt`** (`running=0 sink=ring`) from an idle instrument: a `TRACE` that fills its ring ends itself but stays open, and `INITiate` is refused until `ABORt` has collected the statistics. `pace=` is the pace *in force*, not the one the header announced; `pace_fallback=1` says they differ. `die_age_us` is how old that temperature is, stated rather than expired — nothing in [DS] or this repository supplies a number of seconds after which a die temperature stops being evidence |
| `SENSe:PROFile TRACE\|STATE\|SLEEP` | `METER_OK: profile=<p> adc_config=0x<hhhh> period_us=<n> vshct_us=<n> avg=<n>` |
| `SENSe:CURRent:RANGe:INDex 0\|1` | `METER_OK: range=<r> current_lsb_pa=<n> ceiling_ma=<n> rstacc=<b> discard_us=<n> cnvrf=<b> q=<t>` — `q` drops to `uncal` if that range has no null. A **real** change pays §3.3's three costs and says so: `rstacc=1` (the accumulators are zeroed, because `CHARGE` counts in units of `CURRENT_LSB` and the ranges do not share one), `discard_us` (one conversion period thrown away) and `cnvrf` (re-checked afterwards, not assumed). The `SENSe:CHARge?` window restarts with them |
| `BUS <hz>` | `METER_OK: scl_hz=<n>` — re-creates the I²C device at the new speed |
| `INITiate` | The §4.2 header, then the stream, or silence for `TRACE`. Refused on `path=both` and `path=bypass` (`METER_ERR: shunt bypassed (…)`) unless `FORCE`; a forced capture is `q=invalid` in its header and on `ABORt` |
| `ABORt` | `METER_OK: stopped samples=<n> dropped=<n> read_err=<n> pace_fallback=<b> dur_us=<n> mean_na=<f> min_na=<f> max_na=<f> die_mc_end=<n> flags=0x<hhhh> charge_raw_start=<i64> charge_raw_end=<i64> osc_tol_ppm=<n> q=<t>`. `read_err` counts conversions that never came off the bus — the loop does not advance on one, so for `TRACE` each costs a period of implicit-timestamp accuracy and forces a drift anchor at the gap. On an open `CALibration:ZERO` window `ABORt` prints that command's line instead, see below |
| `TRACe:DATA?` | The §4.3 block, `METER_DUMP_END` |
| `TRACe:MARK "<label>"` | `METER_OK: mark=<label> n=<idx> t=<us>` |
| `CALibration:ZERO <seconds>` | `METER_OK: zero started profile=<p> over_s=<n> n_expect=<n> <relay tokens> (ABORt to finish)` — an **acknowledgement**; it does not block. `SYSTem:STATus?` then reports `sink=null samples=<n>` while it runs, and `ABORt` closes it: `METER_OK: range=<r> offset_pv=<n> sd_pv=<n> over_s=<n> die_mc=<n> n=<samples> <relay tokens>`, computed from the samples actually taken, so `ABORt` also ends a window early. Both lines carry the relay tokens as the window's start found them. Refused on `path=both`, with no override; a window opened with K1 commanded open whose offset is beyond 50 µV is printed, then `METER_ERR: null not stored (…)`, and not stored (§3.11). Blocking the handler left the console deaf for up to ten minutes with no progress and no way to abort, because `ABORt` could not be read |
| `SENSe:CURRent:SHUNt <ohms>` | `METER_OK: shunt_uohm=<n> shunt_measured=1` — bounded to **50000…200000 µΩ**, half the nominal R100 to twice it, and the refusal names the window. It is the one scalar every nanoamp is divided by, so a typo rescales every reading the instrument will ever print, with `q=cal` beside it |
| `CALibration:TAG <text>` | `METER_OK: cal_tag=<text>` |
| `CALibration:VBUSside SOURce\|UNKNOWN` | `METER_OK: vbus_side=<s>` |
| `CALibration:DATA?` | `CAL shunt_uohm=<n> shunt_measured=<b> off_pv_r0=<n> off_pv_r1=<n> nulled_r0=<b> nulled_r1=<b> off_die_mc_r0=<n> off_die_mc_r1=<n> vbus_side=<s> cal_tag=<s>` — either `off_die_mc_*` prints `unknown` when its `CALibration:ZERO` could not read a temperature |
| `DIAGnostic:ALERt?` | `DIAG raw=0x<hhhh> memstat=<b> mathof=<b> energyof=<b> chargeof=<b> cnvrf=<b>` |
| `DIAGnostic:I2C:SCAN?` | `SCAN expected=0x40 found=<csv> n=<count>` |
| `DIAGnostic:REGister? <addr>` | `REG addr=0x<hh> bytes=<n> raw=0x<hex>` — read-only |
| `SENSe:CHARge?` | `CHARGE raw=<i64> nc_nom=<i64> dur_us=<n> of=<b> shunt_uohm=<n> osc_tol_ppm=<n>` — nominal-shunt conversion; the host applies `k` (§3.7, §5.1). `osc_tol_ppm` bounds any mean derived from it: the accumulator counts on the device's own oscillator while `dur_us` comes from `esp_timer` |
| `SENSe:CHARge:RESet` | `METER_OK: charge reset` — `CONFIG` ← `… \| 0x4000` (`RSTACC`) |
| `SYSTem:LED?` | `LED blanked=<csv> pixels=<n> held=low` |
| `ROUTe:STATe?` | `RELAY <relay tokens> pads=ok\|<csv> operate_settle_ms=20 release_settle_ms=50 hold_s=<n>\|- hold_released=<n>` — `pads=` lists any channel whose pin does not read back the level it is driven to |
| `ROUTe:LEAD OPEN\|CLOSE [FORCE] [LIVE]` | `METER_OK: relay <relay tokens> moved=k1\|- settle_us=<n> live=<b> marked=<n> vs_pv=- hold_s=<n>\|- q=<t>` — K1 (§3.11), preceded by one `METER_OK: mark=` line per move under a `LIVE` order |
| `ROUTe:BYPass ON\|OFF [FORCE] [LIVE]` | the same line, `moved=k2\|-` — K2 |
| `ROUTe:NULLpath ON\|OFF [LIVE [FORCE]]` | the same line, `moved=k2,k1` (ON) or `k1,k2` (OFF), or only the steps still missing; ON's `vs_pv=` carries its check readings, first to last |
| `RELAY 3\|4 ON\|OFF [LIVE]` | the same line for a spare. `RELAY 1` and `RELAY 2` are refused, naming `LEAD` and `BYPASS`; from 0.13.0 `ROUTe:RELay3\|RELay4` is refused (`-241`), the spares not being wired |

`<relay tokens>` is always `k1=lead:<closed|open> k2=bypass:<open|closed> k3=spare:unwired k4=spare:unwired path=<shunt|bypass|both|none> check=<ok|unverified|skipped|->` (the spares read `off` or `on` before 0.13.0), the words naming the circuit rather than the coil. `settle_us` is what the order took, checks included; `live=1` says a capture was running through the order and `marked` how many of its moves the capture's mark table took; `vs_pv` the shunt readings a check took, or `-`; `hold_s` the seconds left on the null path's hold, or `-`; and `q` what a capture would carry now.

`DIAGnostic:REGister?` is read-only on purpose. The repository has a register-introspection tradition (`REGS?` / `DIAGnostic:REGister?` / `REGW`, `docs/hardware/introspection/README.md:32`) and a read of any address is invaluable at bring-up; a **write** would let the device be reconfigured behind the protocol's back, so that the header would describe a configuration that is no longer in force. There is no `REGW`.

### 4.5 Errors

One line, one reason, nothing changed:

```text
METER_ERR: no device (scan found none)
METER_ERR: not an ina228 (mfg=0x0000 die=0x000)
METER_ERR: trim memory checksum (MEMSTAT=0)
METER_ERR: bad reset defaults (adc=0x0000 cal=0x0000)
METER_ERR: capture running (ABORt first)
METER_ERR: not running
METER_ERR: no memory (ring floor 4096 refused)
METER_ERR: stream too fast (needs <n> B/s, line <m> B/s, cap <c> B/s)
METER_ERR: unknown channel <name>
METER_ERR: use CALibration:ZERO <seconds>
METER_ERR: i2c <esp_err_name>
METER_ERR: shunt bypassed (path=both: ROUTe:BYPass OFF, or INITiate:FORCe)
METER_ERR: shunt bypassed (path=bypass: ROUTe:NULLpath OFF, or INITiate:FORCe)
METER_ERR: shunt bypassed (path=both: ROUTe:BYPass OFF or ROUTe:NULLpath ON first)
METER_ERR: null not stored (a relay null beyond 50 uV: k1 may not have opened; the stored null stands)
METER_ERR: order disconnects the dut (k1 open, k2 open: add FORCE)
METER_ERR: order bypasses the shunt under a capture (add FORCE: the capture becomes q=invalid)
METER_ERR: capture running (ABORt first, or add LIVE)
METER_ERR: zero running (ABORt first)
METER_ERR: bypass check failed (k2 took no current: vs_pv=<n>,<n>; released to path=<p>)
METER_ERR: lead check failed (the shunt still conducts: vs_pv=<n>,<n>,<n>; released to path=<p>)
METER_ERR: i2c <esp_err_name> (null-path check: nothing moved)
METER_ERR: i2c <esp_err_name> (null-path check: released to path=<p>)
METER_ERR: k1 is the lead (use ROUTe:LEAD OPEN|CLOSe)
METER_ERR: k2 is the bypass (use ROUTe:BYPass ON|OFF)
METER_ERR: use ROUTe:LEAD OPEN|CLOSe [FORCE] [LIVE]
METER_ERR: use ROUTe:BYPass ON|OFF [FORCE] [LIVE]
METER_ERR: use ROUTe:NULLpath ON|OFF [LIVE [FORCE]]
METER_ERR: use RELAY 3|4 ON|OFF [LIVE]
METER_ERR: relays unavailable (gpio config failed at boot)
```

---

## 5. What is deliberately not in v1

| Left out | Why |
|---|---|
| **`ENERGY` (`0x9`)** | It is `POWER` integrated, and `POWER` is itself `VBUS × CURRENT`. The instrument already reports both at full rate, so `ENERGY` adds a 40-bit register read and a second accumulator to re-derive a number the host can integrate from data it already has. Nothing in the fleet's budgets is stated in joules. |
| **Triggered sampling (`MODE` 0h–7h)** | Three reasons. In triggered mode the accumulators are invalid because "the device does not keep track of elapsed time" ([DS] §7.3.4). It costs a register **write** per sample — a whole extra transaction — to save power on an instrument that is USB-powered. And the continuous modes already give every cadence §3.4 needs. **The one exception is deliberate**: `TRACE` uses `MODE = 4h` for a single triggered temperature conversion at each capture boundary, outside the capture (§3.4). |
| **Threshold alerts** (`BOVL`, `BUVL`, `TEMP_LIMIT`, `PWR_LIMIT`, and `SOVL` / `SUVL` as measurement limits) | The limit registers are **16× coarser** than the measurements ([DS] §7.6.1.13–18): the finest step in range 0 is 5 µV = **50 µA**, a quarter of the whole 10–200 µA sleep band, so a limit cannot express a µA-scale threshold at all. They also reset on every VS power cycle and would need reprogramming. And a budget verdict is `pass` / `fail` / **`unknown`** — the on-device meter's own three-way rule — which a hardware comparator cannot express. Verdicts belong on the host. **The one use kept is the clip detector**: `SOVL = 7FF8h` and `SUVL = 8008h`, eight limit counts (128 `VSHUNT` counts, 0.024 %) inside each end of the field on either range, written at every capture and null window (`ina228_regs.h` `kShuntOverLimitClip`). It expresses no threshold of the measurement, only "a conversion reached the end of the field", which the comparator sees on every raw conversion (`SLOWALERT = 0`) and an averaged sample does not. |
| **The `ALE` pin as an *alert*** | — but **the pin itself is used**, for conversion-ready pacing (`CNVR = 1`, `APOL = 0`), which is a different feature wearing the same wire. `SLOWALERT` stays 0. `ALATCH` is 1, so the clip detector's `SHNTOL` / `SHNTUL` hold until the next `DIAG_ALRT` read and reach the `ABORt` line's `flags=` (`STATE` and `SLEEP` read it every sample, every profile at `ABORt`); the pacing does not depend on it, the pin being held until that read in either mode. §3.5. |
| **Two-range auto-switching** | `ina228-reference.md` §8.2: one range does both, and it is `ADCRANGE = 0`. A mid-capture switch would cost a discarded conversion period, an `RSTACC`, the other range's own null and a 4× host-side scale change — six costs for a range that buys **no noise advantage** (median ratio 1.000 across Table 8-2) and cannot reach 500 mA. `SENSe:CURRent:RANGe:INDex 0\|1` stays a manual, per-run setting, refused while running. |
| **`SHUNT_TEMPCO` / `TEMPCOMP`** | The breakout's shunt tempco is unmarked; the compensation is driven by the INA228's **die** temperature and "will depend on how well the resistor and the INA228 are thermally coupled" ([DS] §7.3.5) — they are centimeters apart on this board; and that same section warns the feature can *under-report* current at high shunt voltage. Both registers stay at 0. We measure the die temperature and let the **host** decide the null has gone stale (§3.8), which needs no thermal coupling to be true. |
| **`CONVDLY`** | It exists to time-align several monitors ([DS] §7.3.4). There is one. |
| **High-speed I²C** | Not reachable: ESP-IDF 6.0.3 has no master-code API (§1). Fast-mode plus at 1 MHz is reachable and is the v2 lever if a 1 ms event ever has to be *shaped* rather than detected. |
| **SD sink, Wi-Fi streaming, the DUT VBUS switch, PSRAM, `WINDOW` / `BUDGET` verdicts** | Each is a real stage-3 feature from `docs/research/external-power-meter-options.md` §6, and each adds a subsystem that can fail in a way the owner would have to debug before getting his first number. v1's job is the first number. PSRAM came in 0.14.0 for the TRACE ring. **Wi-Fi never will**: the NW-00 radio A/B failed (a radio on the meter shifts its null whatever its transmit power, `calibration/records/radio-ab-2026-11-21.md` and `radio-tx-sweep-2026-11-21.md`), and the owner ruled on 2026-11-21 that the meter stays serial-only (§6.5). |
| **The RGB LED as a status indicator** | A WS2812 refresh is a current burst on the 3.3 V rail the INA228's front end shares, for no measurement benefit (`carrier-board.md` §4). The LED is blanked and never lit again. |

### 5.1 One deliberate departure: `CHARGE` stays, as a probe

`ENERGY` is out. **`CHARGE` is out of the sampling path, but `SENSe:CHARge?` and `SENSe:CHARge:RESet` stay in v1**, and this is a departure from the obvious "no accumulators" line that is worth the four paragraphs.

`ina228-reference.md` §8.4 makes `CHARGE` the *primary* sleep number: the accumulator sums **every** conversion and is not affected by `AVG` ([DS] §7.3.2), so `mean current = CURRENT_LSB × ΔCHARGE / Δt` integrates a whole eight-hour window in hardware, and over that window quantization disappears entirely, leaving only the offset. That is the specific reason the repo's own study concludes this €30 rig beats a €84 PPK2 on the 20 µA figure.

But §10 open question 1 of the same note says we **do not know whether it works at 10 µA**. At `CURRENT_LSB = 3.125 µA` and a 4.12 ms conversion, one conversion contributes **0.0132 counts**. If the hardware truncates each contribution, `CHARGE` stays at zero forever and the headline claim is false. [DS] states the scale and that accumulation is per conversion cycle; it does not state whether the contribution is truncated.

So:

- **No v1 number depends on `CHARGE`.** Every figure the protocol reports — `mean_na`, `min_na`, `max_na`, the streams, the dumps — comes from the `VSHUNT` path. A 10 s `SLEEP` window already gives **0.19 µA rms** from host-side averaging alone, which is inside the ±0.5 µA offset floor that is the instrument's real limit.
- **The two commands cost about forty lines**, because the accumulator runs in hardware whether we read it or not. The registers are free; only the reading is a choice.
- **They settle the question with one bench run.** A known 10 µA DC load for 60 s should give `ΔCHARGE ≈ 192` counts in range 0. If it does, v2 makes `CHARGE` the authority for the long sleep runs and the 72-hour CoreInk standby becomes practical. If it gives 0, the answer is recorded and nobody designs around it. Since §3.6's boundary reads are now in the capture path, *every* capture carries the pair, so the question is answered by the first `SLEEP` run anybody makes rather than by a run dedicated to it. The 192-count expectation is a `static_assert` in `console.cpp`, against `scale::chargeNanocoulombs`.

`SENSe:CHARge?` therefore reports the **raw** count beside a conversion at the nominal 100 mΩ (§3.7 says why the shunt correction is left to the host here), and the `of=` bit from `DIAG_ALRT` `CHARGEOF` — which clears when `CHARGE` is read ([DS] Table 7-16), so it must be read in the same breath. `SENSe:CHARge:RESet` writes `RSTACC` and is what starts a window. That is all.

---

## 6. The project

### 6.1 Layout

The layout, with the files this design needs:

```text
firmware/                  # the nested project, as it was then
├── platformio.ini          # [env:meter], the only env
├── CMakeLists.txt          # project( kleidos-meter )
├── sdkconfig.defaults      # SS 6.4
├── version.txt             # the INSTRUMENT's own version, independent of the fleet's
├── README.md               # wiring, command surface, how to build
└── src/
    ├── CMakeLists.txt
    ├── main.cpp            # app_main(): release the relays, blank the LEDs, then bring up the rest
    ├── relay.h/.cpp        # SS 3.11 -- the HW-316 inputs, the four circuits, the planned orders
    ├── status_led.h/.cpp   # SS 2 -- the whole of M1
    ├── ina228_regs.h       # the register map, one /// doc per register citing [DS]
    ├── ina228.h/.cpp       # bring-up, identity, profile writes, decode
    ├── scale.h             # SS 3.7 -- the integer conversions, host-unit-testable
    ├── sampler.h/.cpp      # SS 3.5, 3.9, 3.10 -- pacing, ring, anchors, marks
    ├── calibration.h/.cpp  # SS 3.8 -- the NVS blob and the quality token
    └── console.h/.cpp      # SS 4 -- the UART0 reader and the command table
```

Nothing under `src/meter/` includes anything from the repository's `src/` (§1).

### 6.2 Milestones

| | Ships | Proves |
|---|---|---|
| **M1** | §2 only. `blankStatusLeds()`, two printed lines, idle | The LED is dark and stays dark. Names the pin. The one thing the owner is waiting for |
| **M2** | I²C bring-up, `SYSTem:INFormation?`, `DIAGnostic:I2C:SCAN?`, `DIAGnostic:ALERt?`, `DIAGnostic:REGister?` | The wiring is right, the part is an INA228, the address is 0x40, `MEMSTAT` is good. Answers `ina228-reference.md` §10 q2 and q6 |
| **M3** | The three profiles, the sampler, the streams, the ring, `TRACe:DATA?`, `TRACe:MARK` | A first trace of a real board. Answers §10 q4 (already answered on paper, §1) and q5 |
| **M4** | `CALibration:ZERO`, `CAL …`, the `q=` token, `SENSe:CHARge?` | A number that can be believed. Answers §10 q1 and q3 |

### 6.3 The build command

From the repository root, with the PlatformIO virtualenv active:

```bash
source ~/.platformio/penv/bin/activate
pio run --project-dir qa/meter -e meter
```

It writes to the nested project's own `.pio/`, never to the fleet's. The upload adds `-t upload` with `KLEIDOS_METER_PORT` naming the port (README "Flash"). Two concurrent `pio` invocations share `.pio/build` and fake failures, which is a second reason the nested project's build tree is separate.

### 6.4 `sdkconfig.defaults`

Every symbol below was confirmed to exist in `~/.platformio/packages/framework-espidf@4.60003.0`:

```ini
# Silence. A log line inside a 32,768-line dump corrupts a capture (SS 4.1).
CONFIG_ESP_CONSOLE_NONE=y
CONFIG_LOG_DEFAULT_LEVEL_NONE=y
CONFIG_BOOTLOADER_LOG_LEVEL_NONE=y

# No radio. The meter needs neither, and both would share the 3.3 V rail the
# INA228's front end sits on.
CONFIG_BT_ENABLED=n
CONFIG_ESP_WIFI_ENABLED=n

# A 280 us cadence is below the tick; the sampler paces on esp_timer (SS 3.5).
CONFIG_FREERTOS_HZ=1000

# The timer-paced sampler spins on Core 1 and would starve IDLE1 (SS 3.10).
CONFIG_ESP_TASK_WDT_INIT=y
CONFIG_ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1=n

# No PSRAM in v1 (SS 3.9). The module is N16R8; the mode is a v2 question.
CONFIG_SPIRAM=n
```

`board = esp32-s3-devkitc-1` — the only ESP32-S3 DevKitC JSON this platform ships (`~/.platformio/platforms/espressif32/boards/`), and its no-PSRAM profile is what v1 wants. It declares 8 MB of flash on a 16 MB part, which is harmless: v1 has no SD, no log partition and nothing to put in the other 8 MB.

**Two things the project file must restate deliberately**, because a nested project inherits nothing: the `-Werror` flag set from `[esp32_base] build_src_flags` (`platformio.ini:126-152`), and the toolchain pins `platform = platformio/espressif32 @ 7.0.1` plus `platform_packages = platformio/framework-espidf @ ~4.60003.0`. A host test of the firmware repository keeps both from drifting (`qa/tests/firmware/test_meter_project_tracks_fleet.py`); this design does not change it.

<!-- REUSE-IgnoreStart -->
New files start with `// SPDX-License-Identifier: GPL-3.0-or-later` (`AGENTS.md` §9). Writing the INA228 driver from the datasheet rather than vendoring `huming2207/esp-ina228` keeps the `NOTICE` obligation at zero.
<!-- REUSE-IgnoreEnd -->

---

### 6.5 Serial-only, and the hardware it does not use parked (0.16.6)

Moved on 2026-11-22 to [`architecture.md`](architecture.md) §9, the current design, where it is kept current.

## 7. Checks before the first wire, and before the first number

From `carrier-board.md` §10, plus what this design adds:

1. **Continuity, carrier to module.** Left-rail terminal 1 against the module's `3V3` pin, left-rail 4 against the module's `4`.
2. **3.3 V present** between left-rail 1 and 22.
3. **No short** between left-rail 4 and 5.
4. **The port is the CH343 one**: `ioreg -p IOUSB -w0 -l | grep -E '"(idVendor|idProduct|USB Serial Number)"'` → `0x1A86 / 0x55D3`, the serial and node `KLEIDOS_METER_PORT` names (private bench data, never tracked). Open it **releasing RTS before DTR**, or the open itself reboots the board (`carrier-board.md` §7).
5. **Meter the three `01C` resistors** on the breakout (§3.1). If 10 kΩ, keep the I²C wires short or parallel 4.7 kΩ onto SDA and SCL.
6. **Continuity from the `VBUS` screw terminal to `VIN+` and to `VIN−`** — which side, if either, the board ties it to (§3.7). Then `CALibration:VBUSside SOURce`.
7. **`CALibration:ZERO 60` with the DUT lead open and the source still on `IN+`** — unmate the pigtail pair, do not unplug the cell (§3.8), or with the relay module fitted `ROUTe:NULLpath ON` first and `ROUTe:NULLpath OFF` after `ABORt` (§3.11) — then `SENSe:CURRent:SHUNt` from two points, a known 20 µA and a known 20 mA (superseded on 2026-11-20 by the registry's calibration, `calibration/README.md`). Re-null after any change of position or source voltage: the stored null is indexed by shunt range only.
8. **With the relay module fitted and the DUT lead disconnected**: terminal 21 measured loaded first (`carrier-board.md` §10 check 6); every IN LED dark and no click through a meter reset and a power-up; `ROUTe:STATe?` with `path=shunt check=- pads=ok`; then each order once, one coil at a time, and the hold left to expire once (`rig-wiring.md` §8.5).

---

## 8. Open questions this design closes, and the ones it does not

**Closed here, with evidence:**

| Question | Answer |
|---|---|
| `ina228-reference.md` §10 q4 — can ESP-IDF emit the HS master code? | **No.** `driver/i2c_master.h` in 4.60003.0 exposes `scl_speed_hz` only; no header in `esp_driver_i2c` mentions a master code. 400 kHz is the ceiling, 1 MHz FM+ the v2 lever |
| §10 q5 — does writing `CONFIG` restart a conversion? | Moot in v1: `CONFIG` is written only at bring-up and by `SENSe:CURRent:RANGe:INDex`/`SENSe:CHARge:RESet`, all of which are refused while running, and each discards a full period afterwards |
| Reuse `ChargeIntegrator`? | **No.** Its time unit is milliseconds and its gap cap is 10 s (`power_meter_logic.h:155-196`); a 280 µs stream needs neither. And no cross-`src/` include at all |
| Which pacing source? | Measured on the bench (2026-11-20): the pin is held until `DIAG_ALRT` is read, so `STATE` and `SLEEP` pace on it with a read per edge and `TRACE` and `BURST` on the timer; the instrument still measures it at every `INITiate` and records it as `pace=` (§3.5) |
| Which sink? | Forced by the line rate: ring for `TRACE`, stream for the other two (§3.9) |
| Shunt correction in `SHUNT_CAL` or software? | **Software**, because `VSHUNT` is the primary and `SHUNT_CAL` does not touch it (§3.3) |

**Still open, for the bench:**

1. **Which LED pin it was** — settled by M1 in one minute (§2.2).
2. **The real I²C address**, by the boot scan (§3.1).
3. **The value of the three `01C` resistors**, and therefore whether 400 kHz is clean (§3.1).
4. **Whether `VBUS` is tied internally to either side of the shunt** (§3.7).
5. **Whether `CHARGE` accumulates at 10 µA or truncates to zero** — the one run described in §5.1.
6. **The real shunt resistance and its tolerance**, from the two-point calibration, which also measures the input bias current (§3.8).
7. ~~**Whether the `ALE` pin pulses or is level-held**~~ — settled on 2026-11-20: held until `DIAG_ALRT` is read, in either `ALATCH` mode (§3.5).
8. **Whether the relay settle times are long enough** — a `TRACE` across `LIVE FORCE` orders that times each contact against its mark, and a `ROUTe:NULLpath ON` null against a hand-removed-lead null, which should agree within 0.1 µV (§3.11, `rig-wiring.md` §8.6).
9. **Whether the relays stay released through a meter reset and a power-up**, before `app_main()` drives the pins — the chip's reset pad state and the 60 µs power-up glitch of its Table 2-2, settled by the first-power check in `rig-wiring.md` §8.5 (§3.11).
10. **Whether terminal 21 holds the coils** — loaded, not idle (`carrier-board.md` §6, §10 check 6). Until it does, `ROUTe:NULLpath ON` refuses under a DUT drawing 2 mA or more and cannot tell below that.

---

## 9. Sources

**Datasheets.**

- TI **INA228, SLYS021A** (Jan 2021, rev. May 2022) — <https://www.ti.com/lit/ds/symlink/ina228.pdf>. Cited as [DS] with the section, table or equation; all register, field and LSB claims trace through [`ina228-reference.md`](ina228-reference.md), which read that PDF directly.
- **Worldsemi WS2812B** (old revision) — the Adafruit and SparkFun mirrors, <https://cdn-shop.adafruit.com/datasheets/WS2812B.pdf> and <https://cdn.sparkfun.com/assets/e/6/1/f/4/WS2812B-LED-datasheet.pdf>: `T0H` 0.4 µs ±150 ns, `T0L` 0.85 µs ±150 ns, `TH+TL` 1.25 µs ±600 ns, `RES` > 50 µs, GRB MSB-first, "internal data latch".
- **Worldsemi WS2812B-V5** — <http://www.peace-corp.co.jp/data/WS2812B-V5_V1.0_EN.pdf>: `T0H` 220–380 ns, `T0L` 580 ns–1 µs, `T1L` 580 ns–1 µs, "RESET time > 280 µs".
- **Songle SRD-05VDC-SL-C** — the relay sheet, as mirrored at <https://www.scribd.com/document/396809795/5V-Relay-Datasheet-pdf> (release time 5 ms max, contact resistance 100 mΩ max), and the parametric listings of the same part at LCSC (<https://www.lcsc.com/product-detail/C35449.html>: operate 10 ms, release 10 ms, contact resistance 100 mΩ, coil 360 mW) and JLCPCB (<https://jlcpcb.com/partdetail/C35449>). The PDF behind the LCSC listing, <https://www.lcsc.com/datasheet/C35449.pdf>, could not be read in this session; the release time is taken as the larger of the two published values for that reason (§3.11).
- **TE Connectivity, coil suppression and relay life** — <https://www.te.com/en/products/relays-and-contactors/electromechanical-relays/intersection/coil-suppression-relay-life.html>: of the suppression schemes, "the slowest [decay] is experienced with a simple diode shunt across the coil" — the reason for the release allowance (§3.11). No figure is given.
- **ESP32-S3 Series Datasheet v2.2** — <https://documentation.espressif.com/esp32-s3_datasheet_en.pdf>: Table 2-1 (no pull-down at reset on GPIO7, XTAL_32K_P, XTAL_32K_N, GPIO17) and Table 2-2 "Power-Up Glitches on Pins" (a typical 60 µs low-level glitch on all four) (§3.11)
- **NXP UM10204, I²C-bus specification** — <https://www.nxp.com/documents/user_manual/UM10204.pdf>; rise-time limits cross-checked against **TI SLVA695** Table 1 (<https://www.ti.com/lit/pdf/slva695>): `t_r(max)` 300 ns fast mode, 120 ns fast-mode plus.

**The installed toolchain**, read in this session at `~/.platformio/packages/framework-espidf@4.60003.0`:

- `components/esp_driver_rmt/include/driver/rmt_tx.h` — `rmt_tx_channel_config_t` (incl. `flags.init_level`), `rmt_transmit_config_t` (incl. `flags.eot_level`), `rmt_new_tx_channel`, `rmt_transmit`, `rmt_tx_wait_all_done`
- `components/esp_driver_rmt/include/driver/rmt_common.h` — `rmt_enable`, `rmt_disable`, `rmt_del_channel`
- `components/esp_driver_rmt/include/driver/rmt_encoder.h` — `rmt_bytes_encoder_config_t{ bit0, bit1, flags.msb_first }`, `rmt_new_bytes_encoder`
- `components/esp_hal_rmt/include/hal/rmt_types.h` — `rmt_symbol_word_t{ duration0:15, level0:1, duration1:15, level1:1 }`
- `components/esp_driver_gpio/src/gpio.c` — `gpio_set_level()` writes the output register with no output-enable check, and `gpio_output_enable()` (called by `gpio_config()`) routes `SIG_GPIO_OUT`, enables the driver and selects `PIN_FUNC_GPIO`: why the relay latch is written before the pad is configured (§3.11)
- `components/esp_driver_i2c/include/driver/i2c_master.h` — `i2c_master_bus_config_t`, `i2c_device_config_t` (**`scl_speed_hz` and no HS-mode field**), `i2c_new_master_bus`, `i2c_master_bus_add_device`, `i2c_master_transmit`, `i2c_master_receive`, `i2c_master_transmit_receive`, `i2c_master_probe`
- Kconfig symbols confirmed to exist: `ESP_CONSOLE_NONE`, `LOG_DEFAULT_LEVEL_NONE`, `BOOTLOADER_LOG_LEVEL_NONE`, `ESP_WIFI_ENABLED`, `BT_ENABLED`, `FREERTOS_HZ`, `ESP_TASK_WDT_INIT`, `ESP_TASK_WDT_CHECK_IDLE_TASK_CPU1`, `SPIRAM`
- `~/.platformio/platforms/espressif32/boards/esp32-s3-devkitc-1.json` — `mcu: esp32s3`, `maximum_ram_size: 327680`, no PSRAM; it is the only S3 DevKitC JSON the platform ships

**This repository.**

- `docs/ina228-reference.md` — §1 identity, §2 addressing, §4 fields, §5 decoding, §6 scaling, §7 timing, §8.1–8.6 the design arithmetic, §10 the open questions
- `docs/carrier-board.md` — §2 the rails, §3 the I²C choice, §4 the LED and the jumper, §5 the forbidden pins, §7 the two USB ports, §10 the checks
- `src/diag/power_meter_logic.h:155-196` — `ChargeIntegrator`, and why it is not reused
- `docs/testing/serial-debug-commands.md:571-575` — the `TELEM` command surface this protocol copies
- `qa/qa_lib/dut/debug_console.py:43,83` — `parse_fields()` and its `\b(\w+)=(\S+)` contract
- `docs/research/external-power-meter-options.md` §4.5–4.8, §6.1–6.3, §7 — the rig study, the firmware sketch this design makes concrete, and its open questions
- `docs/hardware/introspection/README.md:32` — the `DIAGnostic:REGister?` precedent
- `AGENTS.md` §5 (power budget), §8 (dual core, `rtos::Once`), §9 (SPDX, versioning)
- The owner's photographs of the two boards (not published): on the INA228 breakout, the `VIN+ / VBUS / VIN−` terminals, the `R100` shunt, the `GND · ALE · SCL · SDA · VCC` header, the `A0`/`A1` strap field with the leftmost pad of each row soldered, and the three `01C` 0603 resistors; on the ESP32-S3 board, the `S3-N16R8` shield, the `DSX063-1` carrier, both screw rails, the `1117C 33` LDO, the `CH343P`, the two USB-C receptacles, the `RGB` LED and its jumper
