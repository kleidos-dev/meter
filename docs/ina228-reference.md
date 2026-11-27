<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# INA228 — implementer's reference for the Kleidos bench meter

> **Protocol note (2026-11-19).** The instrument speaks SCPI since firmware 0.2.0, and the command names in this document are its SCPI spellings ([`scpi.md`](scpi.md) has the command set and the map from the console protocol of 0.1.0). The replies the 0.1.0 console printed, an acknowledgement or a refusal per line, now come back as report units and queued errors.


Everything a firmware author needs to drive the TI INA228 on the bench rig without opening the datasheet again: every register, every field, every LSB, the exact decode of the wide registers, and the range/averaging choice worked out in arithmetic for *this* instrument.

- **Scope.** Register-level reference plus the range decision. It does not repeat the buy/not-buy study, which is already in the Kleidos firmware repository's `docs/research/external-power-meter-options.md` (§4 is the INA228 section); this file cross-references it and flags one row of it that looks wrong (§11).
- **Hardware it describes.** The owner's red INA228 breakout with an **R100** shunt, read by an **ESP32-S3-N16R8** on a DSX063-1 screw-terminal carrier ([`carrier-board.md`](carrier-board.md)).
- **Primary source.** TI **INA228 datasheet, literature number SLYS021A**, January 2021, revised May 2022: <https://www.ti.com/lit/ds/symlink/ina228.pdf>. Referred to below as **[DS]**, with the section, table or equation number. **Note on the literature number:** the brief for this work said "SBOS736"; the PDF TI serves at the canonical INA228 datasheet URL above carries **SLYS021A** on every page, so that is the document cited here. Every claim marked [DS] was read out of that PDF in this session, not from memory.
- **Other sources** are linked inline at the claim.

[DS]: https://www.ti.com/lit/ds/symlink/ina228.pdf

---

## 1. Proving it is an INA228

### 1.1 Identity registers

| Register | Address | Reset / expected | Meaning |
| --- | --- | --- | --- |
| `MANUFACTURER_ID` | `3Eh` | **`5449h`** | "Reads back TI in ASCII" ([DS] §7.6.1.19, Table 7-23) |
| `DEVICE_ID` | `3Fh` | **`2281h`** | bits 15-4 `DIEID` = **`228h`**, bits 3-0 `REV_ID` = `1h` ([DS] §7.6.1.20, Table 7-24) |

**Probe to implement:**

```text
read16(0x3E) == 0x5449          // manufacturer
(read16(0x3F) >> 4) == 0x228    // die id, revision masked off
```

Mask `REV_ID`: it is a silicon revision field and a future part may ship a different value. Masking is what TI's own ecosystem does — the SparkFun INA2XX library "compares the masked value against an INA228 constant" ([SparkFun_INA2XX](https://docs.sparkfun.com/SparkFun_INA2XX_Arduino_Library/sfDevINA228_8cpp.html)), and the Linux `ina238` hwmon driver puts the device-ID register at `0x3F` too ([drivers/hwmon/ina238.c](https://codebrowser.dev/linux/linux/drivers/hwmon/ina238.c.html)).

### 1.2 Why that distinguishes it from an INA219 / INA226

- **INA226** also answers `5449h` — but from **`FEh`**, with its Die ID at **`FFh`** = `2260h`, and it defines nothing at `3Eh`/`3Fh` ([TI INA226 datasheet SBOS547, register map](https://www.ti.com/document-viewer/INA226/datasheet/GUID-0EFF2FD2-6EC0-4010-8100-A32052AD0243): "7.1.9 Manufacturer ID Register (FEh)", "7.1.10 Die ID Register (FFh)"; values `5449` / `2260` as quoted in TI's register-map table and mirrored by the Zephyr driver constants [`ina226.c`](https://codex.ac6.fr/Zephyr/xref/Zephyr-latest/drivers/sensor/ti/ina226/ina226.c)). So **reading `3Fh` is the discriminator**, not reading `5449h`.
- **INA219** exposes **no identification register at all**; its map covers `00h`–`05h` only ([TI E2E, "ina219 vendor id register"](https://e2e.ti.com/support/amplifiers-group/amplifiers/f/amplifiers-forum/1258345/ina219-vendor-id-register)). A read of `3Fh` on one is undefined, so require the exact `228h` rather than "not zero".

### 1.3 Belt and braces

After a software reset the INA228's own defaults are distinctive and cheap to check ([DS] §7.6, reset columns):

| Register | After reset |
| --- | --- |
| `CONFIG` (`0h`) | `0000h` |
| `ADC_CONFIG` (`1h`) | **`FB68h`** |
| `SHUNT_CAL` (`2h`) | `1000h` |
| `DIAG_ALRT` (`Bh`) | `0001h` |

`FB68h` decodes exactly to the documented field resets — `MODE=Fh`, `VBUSCT=VSHCT=VTCT=5h`, `AVG=0h` — which is a good self-check that the I²C byte order is right (MSB first, [DS] §7.5.1.1).

Also require **`DIAG_ALRT` bit 0 `MEMSTAT` == 1**. A `0` there means "a checksum error is detected in the device trim memory space" ([DS] Table 7-16) — refuse to measure, the calibration constants are suspect.

---

## 2. Addressing, bus speed and transactions

### 2.1 Address

Two pins, `A0` and `A1`, four states each → **16 addresses** ([DS] §7.5.1, Table 7-2). The 7-bit address is `1 0 0 A3 A2 A1 A0`:

| A1 | A0 | Address | | A1 | A0 | Address |
| --- | --- | --- | --- | --- | --- | --- |
| GND | GND | `1000000` = **0x40** | | SDA | GND | `1001000` = 0x48 |
| GND | VS | `1000001` = 0x41 | | SDA | VS | `1001001` = 0x49 |
| GND | SDA | `1000010` = 0x42 | | SDA | SDA | `1001010` = 0x4A |
| GND | SCL | `1000011` = 0x43 | | SDA | SCL | `1001011` = 0x4B |
| VS | GND | `1000100` = 0x44 | | SCL | GND | `1001100` = 0x4C |
| VS | VS | `1000101` = 0x45 | | SCL | VS | `1001101` = 0x4D |
| VS | SDA | `1000110` = 0x46 | | SCL | SDA | `1001110` = 0x4E |
| VS | SCL | `1000111` = 0x47 | | SCL | SCL | `1001111` = 0x4F |

(The middle column is a spacer; the table is the same 16 rows TI prints.)

**On the owner's board** there is a 2×4 jumper field, rows silkscreened `A0` (upper) and `A1` (lower), columns silkscreened `G · V · S D` (GND / VS / SCL / SDA). **The leftmost pad of each row carries solder** — i.e. `A0 = GND`, `A1 = GND` → **0x40**, which is also the address TI annotates on its own application diagram ([DS] Figure 8-2, "* I2C ADDR: 0x40"). *Confirm with a bus scan before trusting the solder*; the device samples A0/A1 on every bus transaction, so a cold joint shows up as a wandering address.

Gotcha from [DS] §7.5.1: when `A0`/`A1` are strapped to `SDA`, the master must add **100 ns of hold time on the MSB of the address**. Not our case at `GND`.

### 2.2 Bus speed

| Mode | Range | Source |
| --- | --- | --- |
| Fast mode | **1 kHz – 400 kHz** | [DS] §6.6, Timing Requirements (I2C) |
| High-speed mode | **10 kHz – 2.94 MHz** | [DS] §6.6 |

High-speed mode is **not free**: the master must send the HS master code `00001XXX` at ≤ 400 kHz, which the device deliberately does **not** acknowledge, then issue a repeated INITiate and keep the bus in HS with repeated STARTs — a ABORt drops back to F/S mode ([DS] §7.5.1.2). Before planning on it, check that the ESP-IDF I²C master can emit that sequence; otherwise plan on **400 kHz fast mode**, or fast-mode-plus on short wiring, and size the sample rate from §8.4.

Two facts that make timing deterministic ([DS] Figure 7-8 notes): the device **does not perform clock stretching** and **does not support PEC**.

### 2.3 Transactions

- **Write**: `S` · addr(W) · pointer byte · data MSB · data LSB · `P` ([DS] §7.5.1.1). Every write carries the pointer.
- **Read**: set the pointer with `S` · addr(W) · pointer · then `S` · addr(R) · data bytes MSB-first. **The pointer is retained**: "If repeated reads from the same register are desired, it is not necessary to continually send the register pointer bytes; the device retains the register pointer value until it is changed by the next write operation" ([DS] §7.5.1.1). This is what makes a fast single-register trace affordable — see §8.4.
- **Byte counts follow the register width.** The read/write diagrams are drawn for 16-bit registers and "registers with a higher number of bytes will behave similarly" ([DS] §7.5.1.1): 16-bit → 2 bytes, 24-bit → 3 bytes, 40-bit → 5 bytes, always MSB first.
- The part answers the **SMBus Alert Response address `0001100`** ([DS] §7.5.1.3) — irrelevant for one device on a private bus, but it means the ALERT pin can be shared later.

---

## 3. Register map

All addresses not listed are reserved and must not be written ([DS] §7.6.1, Table 7-3).

| Addr | Acronym | Name | Bits | Reset | Access |
| --- | --- | --- | --- | --- | --- |
| `0h` | `CONFIG` | Configuration | 16 | `0000h` | R/W |
| `1h` | `ADC_CONFIG` | ADC Configuration | 16 | `FB68h` | R/W |
| `2h` | `SHUNT_CAL` | Shunt Calibration | 16 | `1000h` | R/W |
| `3h` | `SHUNT_TEMPCO` | Shunt Temperature Coefficient | 16 | `0000h` | R/W |
| `4h` | `VSHUNT` | Shunt Voltage Measurement | **24** | `0h` | R |
| `5h` | `VBUS` | Bus Voltage Measurement | **24** | `0h` | R |
| `6h` | `DIETEMP` | Temperature Measurement | 16 | `0h` | R |
| `7h` | `CURRENT` | Current Result | **24** | `0h` | R |
| `8h` | `POWER` | Power Result | **24** | `0h` | R |
| `9h` | `ENERGY` | Energy Result | **40** | `0h` | R |
| `Ah` | `CHARGE` | Charge Result | **40** | `0h` | R |
| `Bh` | `DIAG_ALRT` | Diagnostic Flags and Alert | 16 | `0001h` | R/W |
| `Ch` | `SOVL` | Shunt Overvoltage Threshold | 16 | `7FFFh` | R/W |
| `Dh` | `SUVL` | Shunt Undervoltage Threshold | 16 | `8000h` | R/W |
| `Eh` | `BOVL` | Bus Overvoltage Threshold | 16 | `7FFFh` | R/W |
| `Fh` | `BUVL` | Bus Undervoltage Threshold | 16 | `0000h` | R/W |
| `10h` | `TEMP_LIMIT` | Temperature Over-Limit Threshold | 16 | `7FFFh` | R/W |
| `11h` | `PWR_LIMIT` | Power Over-Limit Threshold | 16 | `FFFFh` | R/W |
| `3Eh` | `MANUFACTURER_ID` | Manufacturer ID | 16 | `5449h` | R |
| `3Fh` | `DEVICE_ID` | Device ID | 16 | `2281h` | R |

---

## 4. Registers, field by field

### 4.1 `CONFIG` — `0h`, reset `0000h` ([DS] §7.6.1.1, Table 7-5)

| Bits | Field | Type | Reset | Meaning |
| --- | --- | --- | --- | --- |
| 15 | `RST` | R/W | `0h` | **System reset**, same as power-on reset; resets all registers to defaults. `1h` = reset. **Self-clearing.** |
| 14 | `RSTACC` | R/W | `0h` | Clears `ENERGY` and `CHARGE` to 0. `1h` = clear. |
| 13-6 | `CONVDLY` | R/W | `0h` | Delay before the initial ADC conversion, **2 ms per step**: `0h` = 0 s, `1h` = 2 ms, … `FFh` = 510 ms. |
| 5 | `TEMPCOMP` | R/W | `0h` | `1h` enables shunt temperature compensation from `SHUNT_TEMPCO`. |
| 4 | **`ADCRANGE`** | R/W | `0h` | Shunt full-scale range across IN+/IN−. **`0h` = ±163.84 mV, `1h` = ±40.96 mV.** |
| 3-0 | reserved | R | `0h` | Always reads 0. |

### 4.2 `ADC_CONFIG` — `1h`, reset `FB68h` ([DS] §7.6.1.2, Table 7-6)

| Bits | Field | Reset | Meaning |
| --- | --- | --- | --- |
| 15-12 | `MODE` | `Fh` | conversion mode, table below |
| 11-9 | `VBUSCT` | `5h` | bus-voltage conversion time |
| 8-6 | `VSHCT` | `5h` | shunt-voltage conversion time |
| 5-3 | `VTCT` | `5h` | temperature conversion time |
| 2-0 | `AVG` | `0h` | sample averaging count |

**`MODE`** — `8h` is a second shutdown encoding, not a typo:

| triggered | triggered | continuous | continuous |
| --- | --- | --- | --- |
| `0h` Shutdown | `4h` Trig. temperature | `8h` Shutdown | `Ch` Cont. temperature only |
| `1h` Trig. bus | `5h` Trig. temp + bus | `9h` Cont. bus only | `Dh` Cont. bus + temperature |
| `2h` Trig. shunt | `6h` Trig. temp + shunt | `Ah` **Cont. shunt only** | `Eh` Cont. temperature + shunt |
| `3h` Trig. shunt + bus | `7h` Trig. bus + shunt + temp | `Bh` **Cont. shunt + bus** | `Fh` Cont. bus + shunt + temp |

**Conversion time** (same encoding for `VBUSCT`, `VSHCT`, `VTCT`; also [DS] §6.5 `TCT`, "subject to oscillator accuracy and drift"):

| code | `0h` | `1h` | `2h` | `3h` | `4h` | `5h` | `6h` | `7h` |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| µs | 50 | 84 | 150 | 280 | 540 | 1052 | 2074 | 4120 |

**`AVG`**: `0h`=1, `1h`=4, `2h`=16, `3h`=64, `4h`=128, `5h`=256, `6h`=512, `7h`=1024. "The averaging setting applies to all active inputs. When >0h, the output registers are updated after the averaging has completed."

### 4.3 `SHUNT_CAL` — `2h`, reset `1000h` ([DS] §7.6.1.3, Table 7-7)

| Bits | Field | Reset | Meaning |
| --- | --- | --- | --- |
| 15 | reserved | `0h` | Always reads 0 — **`SHUNT_CAL` is 15 bits, max 32767** |
| 14-0 | `SHUNT_CAL` | `1000h` | conversion constant; also sets the resolution of `CURRENT` |

Two operational facts: "The shunt calibration register needs to be correctly programmed at each VS power up in order for the device to properly report any result based on current" ([DS] §8.2.2.3), and "If the value loaded into the `SHUNT_CAL` register is zero, the power, energy and charge values will be reported as zero" ([DS] §7.3.2). Maths in §6.

### 4.4 `SHUNT_TEMPCO` — `3h`, reset `0000h` ([DS] §7.6.1.4, Table 7-8)

Bits 15-14 reserved; bits 13-0 `TEMPCO`, **1 ppm/°C per LSB**, full scale `3FFFh` = 16383 ppm/°C, referenced to +25 °C. Used only when `TEMPCOMP` = 1, as `R_ADJ = R_NOM + R_NOM × (DIETEMP − 25) × SHUNT_TEMPCO / 10^6` ([DS] Equation 1).

**Leave it at 0 for this rig.** The breakout's shunt tempco is not marked, the compensation is driven by the INA228 **die** temperature — "The effectiveness of the compensation will depend on how well the resistor and the INA228 are thermally coupled" ([DS] §7.3.5) — and the same section carries an explicit warning that the feature can *under-report* current at high shunt voltage.

### 4.5 `VSHUNT` — `4h`, 24 bits, read-only ([DS] §7.6.1.5, Table 7-9)

| Bits | Field | Meaning |
| --- | --- | --- |
| 23-4 | `VSHUNT` | Differential voltage across the shunt. **Two's complement.** `312.5 nV/LSB` when `ADCRANGE = 0`; **`78.125 nV/LSB`** when `ADCRANGE = 1` |
| 3-0 | reserved | Always reads 0 |

### 4.6 `VBUS` — `5h`, 24 bits, read-only ([DS] §7.6.1.6, Table 7-10)

| Bits | Field | Meaning |
| --- | --- | --- |
| 23-4 | `VBUS` | Bus voltage. "Two's complement value, however always positive." **`195.3125 µV/LSB`** |
| 3-0 | reserved | Always reads 0 |

The 20-bit field could express 204.8 V; the *specified* input range is 0 V to 85 V ([DS] §6.5, §8.1.1), i.e. up to 435,200 counts.

### 4.7 `DIETEMP` — `6h`, 16 bits, read-only ([DS] §7.6.1.7, Table 7-11)

Bits 15-0, two's complement, **`7.8125 m°C/LSB`** — ±256 °C of numeric range, limited by the package to −40 °C … +125 °C ([DS] §8.1.1). Accuracy ±1 °C max at 25 °C, ±2 °C max over the range ([DS] §6.5).

### 4.8 `CURRENT` — `7h`, 24 bits, read-only ([DS] §7.6.1.8, Table 7-12)

| Bits | Field | Meaning |
| --- | --- | --- |
| 23-4 | `CURRENT` | Calculated current in amperes. **Two's complement.** Scale = `CURRENT_LSB` (§6) |
| 3-0 | reserved | Always reads 0 |

### 4.9 `POWER` — `8h`, 24 bits, read-only ([DS] §7.6.1.9, Table 7-13)

| Bits | Field | Meaning |
| --- | --- | --- |
| **23-0** | `POWER` | Calculated power in watts. **Unsigned, positive value.** **No reserved nibble** |

⚠ This is the asymmetry that bites: `VSHUNT`, `VBUS` and `CURRENT` carry a 20-bit value in bits 23-4, but **`POWER` uses all 24 bits**. Do not shift it right by 4.

### 4.10 `ENERGY` — `9h`, 40 bits, read-only ([DS] §7.6.1.10, Table 7-14)

Bits 39-0, **unsigned**, joules. Scale in §6.

### 4.11 `CHARGE` — `Ah`, 40 bits, read-only ([DS] §7.6.1.11, Table 7-15)

Bits 39-0, **two's complement**, coulombs. Scale in §6.

Both accumulators: "Upon overflow, the `ENERGY` and `CHARGE` registers will roll over and start from zero" and can be cleared with `RSTACC` ([DS] §8.1.2). **Continuous mode is mandatory for them** — "In triggered mode, the accumulation registers (`ENERGY` and `CHARGE`) are invalid, as the device does not keep track of elapsed time" ([DS] §7.3.4). And they ignore averaging: "The energy and charge values are accumulated for each conversion cycle. Therefore the INA228 averaging function is not applied to these" ([DS] §7.3.2).

### 4.12 `DIAG_ALRT` — `Bh`, reset `0001h` ([DS] §7.6.1.12, Table 7-16)

| Bit | Field | Type | Reset | Meaning |
| --- | --- | --- | --- | --- |
| 15 | `ALATCH` | R/W | `0h` | `0h` transparent, `1h` latched until `DIAG_ALRT` is read |
| 14 | `CNVR` | R/W | `0h` | `1h` asserts ALERT on conversion-ready |
| 13 | `SLOWALERT` | R/W | `0h` | `0h` compare the raw ADC value, `1h` compare the **averaged** value |
| 12 | `APOL` | R/W | `0h` | `0h` active-low open-drain, `1h` active-high open-drain |
| 11 | `ENERGYOF` | R | `0h` | `ENERGY` overflowed. **Clears when `ENERGY` is read** |
| 10 | `CHARGEOF` | R | `0h` | `CHARGE` overflowed. **Clears when `CHARGE` is read** |
| 9 | `MATHOF` | R | `0h` | Arithmetic overflow — "current and power data may be invalid". **Must be cleared manually** by triggering another conversion or by `RSTACC` |
| 8 | reserved | R | `0h` | Always reads 0 |
| 7 | `TMPOL` | R/W | `0h` | over-temperature vs `TEMP_LIMIT` |
| 6 | `SHNTOL` | R/W | `0h` | shunt over-voltage vs `SOVL` |
| 5 | `SHNTUL` | R/W | `0h` | shunt under-voltage vs `SUVL` |
| 4 | `BUSOL` | R/W | `0h` | bus over-voltage vs `BOVL` |
| 3 | `BUSUL` | R/W | `0h` | bus under-voltage vs `BUVL` |
| 2 | `POL` | R/W | `0h` | power over-limit vs `PWR_LIMIT` |
| 1 | `CNVRF` | R/W | `0h` | **conversion complete** |
| 0 | `MEMSTAT` | R/W | **`1h`** | `0h` = **trim-memory checksum error**, `1h` = normal |

`CNVRF` is set "after all conversions and averaging is completed" and clears on **writing `ADC_CONFIG`** (except to select shutdown) **or reading `DIAG_ALRT`** ([DS] §7.3.4). Bits 7-2 clear on a read of this register only when `ALATCH = 1`.

### 4.13 Threshold registers ([DS] §7.6.1.13 – §7.6.1.18)

| Addr | Name | Bits | Reset | Format | LSB |
| --- | --- | --- | --- | --- | --- |
| `Ch` | `SOVL` | 15-0 | `7FFFh` | two's complement | **5 µV** (`ADCRANGE=0`) / **1.25 µV** (`ADCRANGE=1`) |
| `Dh` | `SUVL` | 15-0 | `8000h` | two's complement | as `SOVL` |
| `Eh` | `BOVL` | 14-0 (bit 15 reserved) | `7FFFh` | unsigned | **3.125 mV** |
| `Fh` | `BUVL` | 14-0 (bit 15 reserved) | `0000h` | unsigned | **3.125 mV** |
| `10h` | `TEMP_LIMIT` | 15-0 | `7FFFh` | two's complement | **7.8125 m°C** |
| `11h` | `PWR_LIMIT` | 15-0 | `FFFFh` | unsigned | **256 × Power LSB** |

The pattern: the 16-bit limit registers are **16× coarser** than the 20-bit measurement registers (`312.5 nV × 16 = 5 µV`, `78.125 nV × 16 = 1.25 µV`, `195.3125 µV × 16 = 3.125 mV`), and `PWR_LIMIT` is 256× coarser because `POWER` is 24 bits ([DS] §8.2.2.4). Limits also "are set to the default values after VS power cycle events and need to be reprogrammed each time power is applied".

---

## 5. Decoding the wide registers

| Register | Wire bytes | Value bits | Sign | Scale |
| --- | --- | --- | --- | --- |
| `VSHUNT` | 3 | 23-4 | signed | 312.5 nV or 78.125 nV |
| `VBUS` | 3 | 23-4 | signed encoding, always ≥ 0 | 195.3125 µV |
| `CURRENT` | 3 | 23-4 | signed | `CURRENT_LSB` |
| `POWER` | 3 | **23-0** | **unsigned** | `3.2 × CURRENT_LSB` |
| `ENERGY` | 5 | 39-0 | **unsigned** | `16 × 3.2 × CURRENT_LSB` |
| `CHARGE` | 5 | 39-0 | signed | `CURRENT_LSB` |
| `DIETEMP` | 2 | 15-0 | signed | 7.8125 m°C |

Reference decode (C++26, where the conversion to a signed type is two's complement by definition and `>>` on a signed value is arithmetic):

```cpp
// 24-bit register carrying a 20-bit signed value in bits 23-4
constexpr std::int32_t decode20( std::uint32_t raw24 ) noexcept
{
    // bit 23 -> bit 31 (sign), then drop the 4 reserved bits
    return static_cast< std::int32_t >( raw24 << 8 ) >> 12;   // range [-524288, 524287]
}

// 40-bit signed CHARGE
constexpr std::int64_t decode40( std::uint64_t raw40 ) noexcept
{
    return static_cast< std::int64_t >( raw40 << 24 ) >> 24;  // range ±2^39
}
```

Sanity check against the datasheet's own worked example ([DS] Table 8-4): a shunt reading of `1011 0100 0001 0000 0000` is negative; inverting and adding one gives 311040d, which at 312.5 nV/LSB is 97.2 mV, so the measurement is −97.2 mV. Full-scale check: `524288 × 312.5 nV = 163.84 mV` and `524288 × 78.125 nV = 40.96 mV` — the 20-bit field spans exactly the stated range in each case.

---

## 6. The scaling maths

### 6.1 Choosing `CURRENT_LSB` ([DS] §8.1.2, Equation 3)

```text
CURRENT_LSB = Maximum Expected Current / 2^19
```

2^19, not 2^20, because the 20-bit `CURRENT` field is **signed** — 2^19 counts per polarity. TI adds: "While the smallest `CURRENT_LSB` value yields highest resolution, it is common to select a higher round-number (no higher than 8x) value … in order to simplify the conversion."

### 6.2 Programming `SHUNT_CAL` ([DS] §8.1.2, Equation 2)

```text
SHUNT_CAL = 13107.2e6 × CURRENT_LSB × R_SHUNT        (ADCRANGE = 0)
SHUNT_CAL = 13107.2e6 × CURRENT_LSB × R_SHUNT × 4    (ADCRANGE = 1)
```

"13107.2 × 10^6 is an internal fixed value used to ensure scaling is maintained properly"; "the value of `SHUNT_CAL` must be multiplied by 4 for `ADCRANGE = 1`".

Verified against TI's own example ([DS] §8.2.2.3): `I_MAX = 10 A` → `CURRENT_LSB = 10 / 2^19 = 19.0735 µA`; with `R = 16.2 mΩ`, `13107.2e6 × 19.0735e-6 × 0.0162 = 4050` = `FD2h`, which is the value the datasheet quotes. The extraction of the constant is therefore right.

**Correcting for the real shunt.** `SHUNT_CAL` is linear in `R_SHUNT` by Equation 2, so once the shunt is measured against a known current, `SHUNT_CAL_corrected = SHUNT_CAL_nominal × R_measured / R_nominal`. With `SHUNT_CAL = 4096` (§8.3) the granularity of that correction is 1/4096 = **0.024 %**, well under the ±0.05 % max gain error of the part itself ([DS] §6.5). Correcting in software instead is equally valid and keeps the register at a round value — pick one and write it down.

### 6.3 Converting the results ([DS] §8.1.2, Equations 4–7)

```text
Current [A] = CURRENT_LSB × CURRENT
Power   [W] = 3.2 × CURRENT_LSB × POWER
Energy  [J] = 16 × 3.2 × CURRENT_LSB × ENERGY
Charge  [C] = CURRENT_LSB × CHARGE
```

Which quantity needs which conversion ([DS] §7.3.2): "The current and charge are calculated after a shunt voltage measurement, while the power and energy are calculated after a bus voltage measurement." **A shunt-only mode (`MODE = Ah`) therefore yields `CURRENT` and `CHARGE` but no `POWER` and no `ENERGY`.**

"Calculations for power, charge and energy are performed in the background and do not add to the overall conversion time."

---

## 7. What a sample period actually costs

### 7.1 The formula

Enabled inputs are converted **sequentially**, and averaging repeats the whole sequence ([DS] §7.3.4): "Enabled measurement inputs are converted sequentially so the total time to convert all inputs depends on the conversion time for each input and the number of inputs enabled… the conversion sequence repeats until the number of averages is reached."

```text
update period = ( VBUSCT? + VSHCT? + VTCT? ) × AVG
```

Confirmed by [DS] Table 8-2, whose "output sample period" column is exactly `TCT × AVG` for a single channel (1052 µs × 16 = 16.832 ms; 4120 µs × 1024 = 4218.88 ms; 84 µs × 512 = 43.008 ms).

**The reset configuration is slow.** `MODE = Fh` with all three times at 1052 µs costs **3 × 1052 µs = 3.156 ms per update**. Every channel you leave enabled and do not need costs you that fraction of the trace rate: at `VSHCT = 280 µs`, `MODE = Ah` gives a 280 µs period while `MODE = Fh` with all three at 280 µs gives 840 µs — a 3× loss of time resolution for two quantities that barely move during a BLE burst.

Cheap compromise: `MODE = Bh` (shunt + bus) with `VBUSCT = 0h` (50 µs) keeps `POWER`/`ENERGY` alive for +18 % of a 280 µs shunt period.

### 7.2 Other timing

| Item | Value | Source |
| --- | --- | --- |
| Start-up, power-up (`NPOR`) | 300 µs | [DS] §6.5 `TPOR` |
| Start-up, from shutdown | 60 µs | [DS] §6.5 `TPOR` |
| Oscillator settle to <1 % | ~300 µs after power-up | [DS] §7.3.6 |
| Oscillator tolerance | **±0.5 % at 25 °C, ±1 % over temperature** | [DS] §6.5 `FOSC_TOL` |
| Digital filter notch | `f_NOTCH = 1 / (2 × TCT)` | [DS] §7.3.4.1 |
| Filter settling | "always settles within one conversion cycle" | [DS] §7.3.4.1 |
| ALERT latency | assume **1.5 × conversion time**; best case ¼ | [DS] §8.2.3 |
| Shutdown quiescent | 2.8 µA typ, 5 µA max | [DS] §6.5 `IQSD` |
| Active quiescent | 640 µA typ, 750 µA max | [DS] §6.5 `IQ` |

The oscillator tolerance is the time base of `CHARGE` and `ENERGY` ([DS] §7.3.6), so a charge-integration result carries **±0.5 %** of timebase error — ±0.05 µA on a 10 µA reading, i.e. negligible beside the offset (§8.5).

`CONVDLY` (0–510 ms, 2 ms steps) exists to time-align *several* monitors ([DS] §7.3.4); with one device, leave it at 0.

---

## 8. The design for this instrument

### 8.1 The span the Kleidos fleet demands

| What | Current | Shunt voltage at **0.100 Ω** |
| --- | --- | --- |
| Deep sleep, floor | **10 µA** | **1 µV** |
| Deep sleep, ceiling | 200 µA | 20 µV |
| Panel dark, 80 MHz | 22–26 mA | 2.2–2.6 mV |
| HOME at 85 % backlight | 69–70 mA | 6.9–7.0 mV |
| PIN at full backlight | 77–81 mA | 7.7–8.1 mV |
| BLE typing | 85–92 mA | 8.5–9.2 mV |
| SoftAP up | 153 mA | 15.3 mV |
| BLE/Wi-Fi peak | ~160 mA | 16 mV |
| Charging | ~500 mA | **50 mV** |

(The awake figures are the Kleidos firmware repository's own measured ones, `docs/research/consumption-self-measurement.md` "POWERRUN battery runs", quoted in `AGENTS.md` §5. 10–200 µA sleep and ~500 mA charging are the task's requirement.)

That is a **50,000 : 1** span. `R100` is the standard EIA code for **0.100 Ω** (the `R` is the decimal point) — tolerance unmarked on this part, so treat it as unknown until measured (§8.6 step 8).

**Burden.** The Kleidos firmware repository's rule is "under 50 mV at 130 mA" (`docs/research/external-power-meter-options.md` §1.3, against a pack `R_int ≈ 0.2 Ω`). 0.100 Ω gives **13 mV at 130 mA** — half the cell's own sag — and **50 mV at 500 mA**, which is at the rule's absolute ceiling but applies to a *charging* board, i.e. one fed from USB rather than from the cell, so the sag rule does not bind there. A 0.25 Ω shunt, which would halve the offset error, would drop **125 mV at 500 mA** and break that rule. **Keep the R100.**

### 8.2 `ADCRANGE`: the arithmetic, and the honest verdict

**Raw coverage**, at 0.100 Ω:

| | `ADCRANGE = 0` | `ADCRANGE = 1` |
| --- | --- | --- |
| Full scale | ±163.84 mV | ±40.96 mV |
| **Current ceiling** | **±1.6384 A** | **±409.6 mA** |
| `VSHUNT` LSB | 312.5 nV → **3.125 µA** | 78.125 nV → **0.78125 µA** |
| 10 µA reads as | 3.2 counts | 12.8 counts |
| **500 mA charge** | 30.5 % of full scale, fits | **clips at 409.6 mA** |

So the first half of the answer is blunt: **`ADCRANGE = 1` cannot see the charging board.** 500 mA across 0.100 Ω is 50 mV and the range stops at 40.96 mV.

The second half is the part that is easy to get wrong. The obvious reading — "use range 1 for sleep, it has a 4× finer LSB" — assumes the LSB sets the floor. It does not. Taking TI's own noise table ([DS] Table 8-2, "noise-free ENOB … calculated based on noise peak-to-peak values") and converting each entry to peak-to-peak **current** through 0.100 Ω, i.e. `noise_pp = 2^(20 − ENOB) × V_LSB / R`:

| `TCT`, `AVG` | period | range 0 | range 1 | ratio |
| --- | --- | --- | --- | --- |
| 50 µs, 1 | 0.050 ms | 606 µA | 606 µA | 1.00 |
| 280 µs, 1 | 0.280 ms | 230 µA | 230 µA | 1.00 |
| 1052 µs, 1 | 1.05 ms | 141 µA | 132 µA | 0.93 |
| 4120 µs, 1 | 4.12 ms | 50.0 µA | 57.4 µA | 1.15 |
| 1052 µs, 16 | 16.8 ms | 35.4 µA | 33.0 µA | 0.93 |
| 4120 µs, 16 | 65.9 ms | 15.4 µA | 13.4 µA | 0.87 |
| 4120 µs, 64 | 264 ms | 7.69 µA | 8.84 µA | 1.15 |
| 1052 µs, 256 | 269 ms | 7.69 µA | 7.69 µA | 1.00 |
| 4120 µs, 1024 | 4.22 s | 3.12 µA | 1.92 µA | 0.62 |

Across **all 64 entries** of Table 8-2 the ratio range1/range0 has **median 1.000 and mean 1.019** (min 0.50, max 1.52). Expressed input-referred, both ranges sit at about **5–6 µV p-p at 4120 µs/AVG 1** and about **60 µV p-p at 50 µs/AVG 1**. In other words the INA228's noise at 0.100 Ω is set by its **front end, not by its quantizer**, and `ADCRANGE` buys **no noise advantage** — only headroom and a finer quantum.

And the finer quantum is not needed, because the noise is already many LSBs wide even in range 0 (16 LSB p-p at 4120 µs/AVG 1). Noise much greater than 1 LSB means the quantizer is **dithered**, so software or hardware averaging recovers resolution far below the LSB — which is exactly how a 3.125 µA quantum resolves a 10 µA reading.

**Verdict.**

> **One range does do both — and it is `ADCRANGE = 0`, not the one that looks right.** `ADCRANGE = 0` covers 10 µA to 1.6384 A in a single setting, with no switching, no gap, no per-range re-zero, because the limit at 10 µA is offset and noise (§8.5), not quantization. `ADCRANGE = 1` is an *option*, not the default: take it only for a run guaranteed to stay under 409.6 mA, where it buys a 4× finer quantum and, at the long conversion times, ~10–40 % less noise.

**What the two-range answer costs**, if the owner wants it anyway (for example range 1 for a sleep campaign and range 0 for a charge campaign):

1. **One register write** — `CONFIG` bit 4 — and, thanks to §8.3, **no `SHUNT_CAL` rewrite at all**.
2. **A host-side scale change of exactly 4×** on `CURRENT`, `POWER`, `ENERGY` and `CHARGE`, because `CURRENT_LSB` changes with the range.
3. **One discarded conversion period.** [DS] is explicit that writing `ADC_CONFIG` "will interrupt and restart triggered or continuous conversions that are in progress" (§7.3.4) but says nothing about `CONFIG`; discard a full period and re-check `CNVRF` rather than assume.
4. **`RSTACC` on every switch.** `ENERGY` and `CHARGE` would otherwise hold counts accumulated under two different `CURRENT_LSB` values in one number.
5. **A separate zero-null per range** (§8.5) — the offset in *volts* is the same spec, but it is ±10 µA in both and must be measured on the range in use.
6. **No autoranging mid-trace.** A switch costs a gap, so a single run cannot span sleep and charging. Split the campaigns instead.

### 8.3 `CURRENT_LSB` and `SHUNT_CAL` for the R100

Apply Equation 3 with the *range's own* full-scale current as the maximum expected current:

| | `ADCRANGE = 0` | `ADCRANGE = 1` |
| --- | --- | --- |
| Max expected current | 163.84 mV / 0.1 Ω = 1.6384 A | 40.96 mV / 0.1 Ω = 0.4096 A |
| `CURRENT_LSB` = I_max / 2^19 | **3.125 µA** (= 25/8 µA) | **0.78125 µA** (= 25/32 µA) |
| `13107.2e6 × CURRENT_LSB × 0.1` | 4096 | 1024 |
| ×4 for `ADCRANGE = 1` | — | ×4 = 4096 |
| **`SHUNT_CAL`** | **4096 = `1000h`** | **4096 = `1000h`** |

Three things fall out, and all three are worth knowing:

1. **`SHUNT_CAL` is 4096 in *both* ranges.** The ×4 the datasheet demands for `ADCRANGE = 1` exactly cancels the 4× smaller `CURRENT_LSB` that range implies. A range change is therefore **one bit in `CONFIG`** plus a host-side factor of 4 — nothing else.
2. **4096 = `1000h` is the device's own reset value** ([DS] §7.6.1.3). With a 0.100 Ω shunt the part powers up already calibrated. (Program it anyway, per [DS] §8.2.2.3, so the code does not depend on that coincidence.)
3. **Both values are exact.** No rounding of `SHUNT_CAL`, so no gain error is introduced by the calibration constant — unlike the "round number" route (`CURRENT_LSB = 1 µA` would need `SHUNT_CAL = 1310.72 → 1311`, +0.021 % error, for no benefit). And `CURRENT_LSB` is an exact binary fraction, so `µA = counts × 25 / 8` (range 0) or `counts × 25 / 32` (range 1) is exact integer arithmetic on the S3.

Because `CURRENT_LSB` equals `V_LSB / R` exactly in both ranges, **`CURRENT` counts should track `VSHUNT` counts one for one** (up to the device's internal rounding) — a free consistency check in the firmware.

Derived scales, with `SHUNT_CAL = 4096`:

| Quantity | `ADCRANGE = 0` | `ADCRANGE = 1` |
| --- | --- | --- |
| `CURRENT_LSB` | 3.125 µA | 0.78125 µA |
| Power LSB (`3.2 × CURRENT_LSB`) | 10 µW | 2.5 µW |
| Energy LSB (`16 × 3.2 × CURRENT_LSB`) | 160 µJ | 40 µJ |
| Charge LSB (`CURRENT_LSB`) | 3.125 µC | 0.78125 µC |

### 8.4 Averaging: two profiles, and why not one

The two requirements are five orders of magnitude apart **in time** as well as in current, so they are two configurations of one instrument, chosen per run. Say it plainly: **no single `AVG` setting both resolves 10 µA and shows a BLE peak.**

#### `TRACE` — everything awake, including charging

```text
CONFIG      ADCRANGE = 0
ADC_CONFIG  MODE = Ah (continuous shunt only)   -- or Bh with VBUSCT = 0h
            VSHCT = 3h (280 us)
            AVG   = 0h (1)
```

| | |
| --- | --- |
| Update period | **280 µs** → 3,571 Sa/s |
| Filter notch | `1/(2 × 280 µs)` = **1.79 kHz** |
| Per-sample noise | 230 µA p-p ≈ 35 µA rms — **0.04 %** of a 90 mA state, **0.02 %** of a 160 mA peak |
| Ceiling | 1.6384 A — covers the 500 mA charge |
| I²C cost | a repeated 3-byte read with the pointer retained (§2.3) is 1 address + 3 data bytes ≈ 36 bit-times ≈ **90 µs at 400 kHz**, i.e. **32 % bus duty** in a 280 µs slot |

**`AVG` must stay at 1 here, and this is the "hiding a BLE peak" test.** Averaging replaces N conversions with their mean, so an event shorter than the averaging window is diluted by `event_duration / window`: at `VSHCT = 280 µs` with `AVG = 16` the window is 4.48 ms, and a 1 ms, +100 mA burst shows up as **+22 mA** — and shows up *once*, with no way to tell a long small event from a short large one. At `AVG = 1` the same burst spans three to four consecutive samples, at least two of them entirely inside it. Average afterwards, in software, where the raw stream is still there to look at.

If a 1 ms event must be *shaped* rather than merely detected, drop to `VSHCT = 2h` (150 µs, notch 3.33 kHz, 6,667 Sa/s) and raise SCL to 1 MHz, where the same read costs 36 µs.

Per-state means (PIN, HOME, BLE typing, SoftAP) come from host-side averaging of this same stream — no second configuration needed. At 3,571 Sa/s, one second of `TRACE` averages to 35 µA/√3571 ≈ **0.6 µA rms**, far finer than those states need.

#### `SLEEP` — deep sleep, 10–200 µA, DC

```text
CONFIG      ADCRANGE = 0   (or 1; see §8.2 -- the DUT is far under 409.6 mA here)
ADC_CONFIG  MODE = Bh (continuous shunt + bus)   -- Bh, not Ah, so CHARGE has a
                                                 -- companion ENERGY and POWER
            VBUSCT = 0h (50 us)
            VSHCT  = 7h (4120 us)
            AVG    = 2h (16)
```

| | |
| --- | --- |
| Update period | `(4120 + 50) µs × 16` = **66.72 ms** |
| Per-point noise | 15.4 µA p-p ≈ **2.3 µA rms** (range 0); 13.4 µA p-p ≈ 2.0 µA rms (range 1) |
| After 1 s of host averaging | **0.60 µA rms** |
| After 10 s | **0.19 µA rms** |
| After 60 s | **0.077 µA rms** |

Two rules behind those numbers.

- **Total integration time is what matters, not how it is split.** `4120/AVG 16` host-averaged for 1 s and `4120/AVG 64` host-averaged for 1 s both land on 0.60 µA rms. Given the choice, **prefer the smaller `AVG` and the longer host window**: you keep per-point visibility, so a wake-up glitch, a Wi-Fi beacon or a PMIC burp is *visible and rejectable* instead of being folded silently into the mean. A large `AVG` throws that away for nothing.
- **`CHARGE` is the primary number, the stream is the check.** The accumulator sums **every** conversion and is not affected by `AVG` ([DS] §7.3.2), so `mean current = CURRENT_LSB × ΔCHARGE / Δt` integrates the whole window in hardware. The Kleidos firmware repository's own study makes this the reason the rig beats a PPK2 (`docs/research/external-power-meter-options.md` §4.5): over an 8 h sleep at 20 µA the register collects 737,280 counts in range 1, 184,320 in range 0, and quantization disappears, leaving only the offset. Continuous mode is mandatory for it (§4.11), and `RSTACC` starts the window.

### 8.5 What actually limits 10 µA

At 0.100 Ω, **10 µA is 1 µV** — which is precisely the part's maximum offset spec. The error budget, with [DS] §6.5 on the left:

| Term | Spec | At 0.100 Ω | After a zero-current null |
| --- | --- | --- | --- |
| `Vos`, shunt offset | ±0.3 µV typ, **±1 µV max** | ±3 / **±10 µA** | removed |
| `dVos/dT` | ±2 nV/°C typ, **±10 nV/°C max** | 0.02 / **0.1 µA/°C** | **±0.5 µA over ±5 °C** |
| `PSRR` (offset vs supply) | ±0.05 µV/V typ, ±0.5 µV/V max | ±0.5 / ±5 µA per volt of VS shift | keep VS steady |
| Gain error | ±0.01 % typ, ±0.05 % max | ±0.005 µA on 10 µA | — |
| Shunt tolerance | unmarked; assume 1 % | ±0.1 µA on 10 µA | removed by a one-off calibration |
| `CMRR` (offset vs **input** common mode) | **[READ OFF DS §6.5]** — not quoted here, because it has not been read off the table yet | the shift is `V_CM / 10^(CMRR/20)` volts, i.e. that over 0.100 Ω in amps | removed **only** by a null taken at the working common mode |
| Input bias `IB` | 0.1 nA typ, 2.5 nA max | ±0.0025 µA | negligible |
| Oscillator (charge window) | ±0.5 % at 25 °C | ±0.05 µA on 10 µA | — |
| ADC noise, `SLEEP` + 10 s | — | 0.19 µA rms | — |

**So the floor is the offset null's residual, about ±0.5 µA**, matching the Kleidos firmware repository's conclusion (`docs/research/external-power-meter-options.md` §4.5). On a 10 µA reading that is **±5 %**; on the fleet's 10–200 µA band it is 0.25–5 %. Nothing in the `ADCRANGE` or `AVG` choice improves it — the only lever that does is a larger shunt, and §8.1 shows 0.25 Ω breaks the burden rule at 500 mA. If the sleep floor ever proves limiting, that is a *second* shunt on a *sleep-only* rig, not a change to this one.

The null itself: run the identical window with the DUT disconnected **and the source still connected to `IN+`**, and subtract the accumulated charge. Re-null whenever the bench temperature has moved — **and whenever the input common mode has moved**, which the stored null does not record and `q=` cannot detect: it is indexed by shunt range only, and staleness is judged on die temperature alone (`firmware/src/calibration.h:83-85,92`, `:44,68`). Position A works at ~3.0–4.2 V and position B at ~5 V, so a position change needs a fresh null. That rule is also what covers the `CMRR` row above while its datasheet figure is unread. The step-by-step is step 9 of [the rig's wiring](rig-wiring.md#step-9--what-is-left-to-declare-before-the-first-number) (§5).

**One wiring trap worth repeating here**, because it is a register-level fact: the `VBUS` pin's input impedance is **1 MΩ typ** ([DS] §6.5 `ZVBUS`), so at 3.8 V it draws **3.8 µA**. Wire `VBUS` **upstream** of the shunt (the source side) or that 3.8 µA is counted as DUT current — a 38 % error on a 10 µA reading (the Kleidos firmware repository's `docs/research/external-power-meter-options.md` §4.5, which counts 19 % on 20 µA). On the owner's breakout `VBUS` is its own screw terminal beside `VIN+` and `VIN−`, so the choice is the wiring's; confirm with a continuity check whether the board also ties it to either side.

The meter's own 640 µA quiescent current is drawn from the **meter's** supply, not the DUT's ([DS] §6.5 `IQ`; the `VS` pin is separate from `IN+`/`IN−`).

### 8.6 Bring-up sequence

1. **Scan the bus.** Expect one device; confirm the address against §2.1.
2. **`CONFIG` ← `8000h`** (`RST = 1`). Self-clearing. Wait ≥ 300 µs ([DS] §6.5 `TPOR`, §7.3.6).
3. **Verify identity** — `3Eh` == `5449h`, `(3Fh >> 4)` == `228h` (§1.1).
4. **Verify defaults** — `ADC_CONFIG` == `FB68h`, `SHUNT_CAL` == `1000h` (§1.3).
5. **Check `DIAG_ALRT` bit 0 `MEMSTAT` == 1**; refuse to measure otherwise.
6. **`SHUNT_CAL` ← `1000h`** (4096) explicitly. Required at every VS power-up ([DS] §8.2.2.3), and the one register whose omission silently zeroes `CURRENT`, `POWER`, `ENERGY` and `CHARGE` ([DS] §7.3.2).
7. **`CONFIG` ← `0000h`** for `ADCRANGE = 0` (or `0010h` for `ADCRANGE = 1`), `TEMPCOMP = 0`, `CONVDLY = 0`.
8. **`ADC_CONFIG` ←** the profile from §8.4. (`TRACE` with `MODE = Ah`, `VSHCT = 3h`, `AVG = 0h` is `0xA0C0`; `SLEEP` with `MODE = Bh`, `VBUSCT = 0h`, `VSHCT = 7h`, `VTCT = 0h`, `AVG = 2h` is `0xB1C2`; both were derived from §4.2 and re-checked bit by bit, but **derive and unit-test them again from the field encodings rather than copying them**.)
9. **`CONFIG` ← … | `4000h`** (`RSTACC`) to zero the accumulators at the start of each run.
10. **Null the offset**: DUT disconnected, run the window, record the mean and the accumulated charge.
11. **Calibrate the shunt**: a known DC load, compare against a reference meter, fold the ratio into `SHUNT_CAL` or into a host scalar (§6.2) — one, not both.
12. **Poll** either `DIAG_ALRT` bit 1 (`CNVRF`) or the ALERT pin with `DIAG_ALRT` `CNVR = 1`, `ALATCH = 0`. Remember `CNVRF` clears on reading `DIAG_ALRT` and on writing `ADC_CONFIG` ([DS] §7.3.4).
13. **Check `MATHOF` (bit 9) every window.** It latches and only clears on a new conversion or `RSTACC`; while it is set, "current and power data may be invalid".

---

## 9. Wiring: what is settled and what is not

Settled from the boards as they are printed, and nothing more:

- The breakout's screw terminals are **`VIN+`, `VBUS`, `VIN−`**, with the **R100** shunt between `VIN+` and `VIN−`; the 5-pin header is **`GND · ALE · SCL · SDA · VCC`**; the address jumpers are the 2×4 field described in §2.1.
- `ALE` is the ALERT pin — **open-drain** ([DS] Table 7-16 `APOL`), so it needs a pull-up to the S3's 3.3 V if it is used at all.
- The ESP32-S3 carrier brings out, on the left terminal strip, `3.3 · 3.3 · RST · 4 · 5 · 6 · 7 · 15 · 16 · 17 · 18 · 8 · 3 · 46 · 9 · 10 · 11 · 12 · 13 · 14 · 5V · G`, and on the right strip `G · TX · RX · 1 · 2 · 42 · 41 · 40 · 39 · 38 · 37 · 36 · 35 · 0 · 45 · 48 · 47 · 21 · 20 · 19 · GND · GND` ([`carrier-board.md`](carrier-board.md) §2).

**Not settled here, deliberately:** which of those GPIOs carry SDA/SCL/ALERT. That choice depends on which pins the N16R8 module's octal PSRAM and the USB peripheral already own, which must be read out of the Espressif module datasheet and the ESP-IDF pin documentation in the wiring phase — not guessed from a photo.

---

## 10. Open questions to settle on the bench

1. **Does the `CHARGE` accumulator keep fractional counts?** §8.4 leans on `CHARGE` as the primary sleep number. [DS] states the scale (`Charge[C] = CURRENT_LSB × CHARGE`, Equation 7) and that accumulation is per conversion cycle (§7.3.2), but **not** whether each conversion's contribution is truncated. At 10 µA with `CURRENT_LSB = 3.125 µA` and a 4.12 ms conversion, one conversion contributes 0.0132 counts; if that is truncated, `CHARGE` stays at zero forever. **Cheap check:** a known 10 µA DC load for 60 s should give `ΔCHARGE ≈ 192` counts (range 0) or 768 (range 1). If it gives 0, fall back to host-side averaging of `VSHUNT`.
2. **The actual I²C address**, by bus scan (§2.1).
3. **The real shunt resistance and its tolerance** (§6.2, §8.6 step 11).
4. **Whether the ESP-IDF I²C master can emit the HS master code** (§2.2). If not, 400 kHz fast mode (or 1 MHz on short wiring) sets the trace ceiling.
5. **Whether writing `CONFIG` restarts an in-flight conversion.** [DS] documents this only for `ADC_CONFIG` (§7.3.4). Until measured, discard one period after any `CONFIG` write (§8.2).
6. **Whether this board ties `VBUS` to `VIN+` internally** (§8.5).

---

## 11. Cross-check against what the repo already says

The Kleidos firmware repository's `docs/research/external-power-meter-options.md` §4 and this file agree on the substance: 0.100 Ω is the right shunt, `SHUNT_CAL = 4096` is exact, the accumulator is what makes the sleep budget provable, the offset null leaves ±0.5 µA, and the `VBUS` 1 MΩ trap is real (the last four in its §4.5, "The accumulator is the whole trick").

Three things this file adds or corrects:

1. **`ADCRANGE` is not the low-current lever.** That document says "`0.1 Ω` is the pick, with `ADCRANGE` as the software range" and tabulates only range 1 noise (its §4.1). The range-0 column of [DS] Table 8-2, converted the same way (§8.2), shows the two ranges are within a few per cent of each other **in µA** at almost every setting — so range 0 can serve the whole span, including the 500 mA charge that range 1 cannot reach at all. That reverses the default.
2. **A suspected error in its noise table.** The row `| 1052 µs, AVG 64 | 67.3 ms | 14.6 | 33.0 µA |` (its §4.1) pairs the period of `AVG = 64` with the ENOB of `AVG = 16`. [DS] Table 8-2 gives `ADCRANGE = 1` **14.6** at 1052 µs / **AVG 16** (period **16.832 ms**, 33.0 µA) and **15.9** at 1052 µs / **AVG 64** (period 67.328 ms, **13.4 µA**). The row is pessimistic by 2.5×; worth fixing there.
3. **`SHUNT_CAL` is 4096 in *both* ranges** (§8.3), so a range switch needs no recalibration write — a simplification that document does not draw out because it only works range 1.

---

## 12. Sources

- **TI INA228 datasheet, SLYS021A** (January 2021, revised May 2022) — <https://www.ti.com/lit/ds/symlink/ina228.pdf>. Every claim tagged [DS]; the section, table or equation number is given at the claim.
- **TI INA226 datasheet SBOS547**, register map (for the ID-register contrast) — <https://www.ti.com/document-viewer/INA226/datasheet/GUID-0EFF2FD2-6EC0-4010-8100-A32052AD0243>
- **Zephyr `drivers/sensor/ti/ina226/ina226.c`** (INA226 ID constants) — <https://codex.ac6.fr/Zephyr/xref/Zephyr-latest/drivers/sensor/ti/ina226/ina226.c>
- **TI E2E, "ina219 vendor id register"** (INA219 has no ID register) — <https://e2e.ti.com/support/amplifiers-group/amplifiers/f/amplifiers-forum/1258345/ina219-vendor-id-register>
- **SparkFun INA2XX Arduino Library** (masking `REV_ID` when identifying) — <https://docs.sparkfun.com/SparkFun_INA2XX_Arduino_Library/sfDevINA228_8cpp.html>
- **Linux `drivers/hwmon/ina238.c`** (device-ID register at `0x3F`) — <https://codebrowser.dev/linux/linux/drivers/hwmon/ina238.c.html>
- **The Kleidos firmware repository:** `docs/research/external-power-meter-options.md` (§4, the INA228 study), `docs/research/consumption-self-measurement.md` (the measured awake currents), and `AGENTS.md` §5 (the power budget).
