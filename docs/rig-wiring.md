<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Building the INA228 meter — the document you follow with the board in your hand

> **Protocol note (2026-11-19).** The instrument speaks SCPI since firmware 0.2.0, and the command names in this document are its SCPI spellings ([`scpi.md`](scpi.md) has the command set and the map from the console protocol of 0.1.0). Replies are quoted as the SCPI console gives them (report units and queued errors), and the firmware is cited by symbol, not by line.


| | |
|---|---|
| Status | Build document, followed to wire the rig (drawn as wired in `hardware/`, rev D, 2026-11-20). No file under `firmware/src` was changed by this work — one firmware change is *noted as pending* in §7 and not made. |
| Date | 2026-10-26 (revised: the null's place in the order and its exact gesture, the case-2 verdict, the two-pass meaning of S1/S4/S6, the new S10, the pass-through moved to position A). 2026-10-30: §8, the HW-316 relay module that takes the null and the cold power cycle off the bench by hand. 2026-11-21: where the breakout sits (§3, "Where the breakout sits") |
| Scope | The one ordered procedure: what to measure before cutting a wire, every wire as a row, the order of assembly, the checks before power, the first console test, the better topology where the source notes recommend one, and what is still open. |
| Language | The body is **English**, like every other document in the tree. Headings, token names, command words and the source list stay as they are printed on the hardware and by the firmware. |
| Sources | `ina228-reference.md`, `carrier-board.md`, `firmware-design.md`, the owner's photographs of the two boards (not published: what they show is written where it is used), and `firmware/src` at the symbols cited. Full list at the end. |
| Rule | Every pin, terminal, number and console reply below is read off the boards, quoted from the firmware with a `file:line`, or worked out on this page. Where a look at a board cannot settle something it says so and gives the measurement that settles it. Nothing is from memory. |

**The three pieces, and what they are called here:**

- **The meter (carrier)** — an ESP32-S3-N16R8 on a DSX063-1 carrier. Its terminals are numbered **1…22 top to bottom on each rail**, and that numbering only holds with `DSX063-1` legible at the **bottom**. Turn the board around and terminal 1 becomes terminal 22, and there is no number printed on the connector to warn you.
- **The INA228 breakout** — the red board. Orientation: **screw terminals up, five-pin header down**. Terminals `VIN+ · VBUS · VIN−` left to right; header `GND · ALE · SCL · SDA · VCC` left to right (as the silkscreen prints them).
- **The DUT** — the Kleidos board being measured. The first target is the **T-Deck**, because its cell is user-fitted and nothing has to be opened.

---

## 1. What to check before wiring anything

This section has **two blocks, and the multimeter is not used the same way in both**. Write down every reading: they are what turns the rest of this document into fact.

- **1.1 to 1.7 — unpowered.** Multimeter on **continuity / resistance**. The breakout **unpowered**, with nothing in the three screw terminals and nothing on the five-pin header.
- **1.8 and 1.9 — powered.** These measure the **carrier with power on**, with the CH343 cable plugged in. **Switch the multimeter to DC volts before plugging anything in**, and never leave it on the current range while touching a live rail: that range is a short circuit with a fuse inside it.

### 1.1 — Zero the probes

Touch the two probes together, write down what they read, and subtract that number from everything that follows. At 0.1 Ω the probes are a third of the measurement.

### 1.2 — Identify the shunt

> **Put the probes on `VIN+` and on `VIN−`.**

| If you read | The case | What changes downstream |
|---|---|---|
| **≈ 0.100 Ω** (probes subtracted) | The `R100` is the only thing between the two terminals. Correct. | Nothing. Carry on. |
| **Between ~0.02 and ~0.09 Ω** | **Something is in parallel with the shunt**: a stray strand left over from the cut wire, a blob of solder, a pinched piece of insulation across the two screws. | **Stop and find it**, even though the reading looks "nearly right". See the warning below. |
| **< 20 mΩ** | An outright bridge. | **Stop.** The shunt is shorted and the meter would read ≈ 0 forever. |
| **kΩ or open** | A broken element, or a loose screw. | **Stop.** Check the screw; if it is still open, the part is broken. |

**Why a partially bridged shunt is the most expensive failure.** A parallel path breaks nothing: it shares the current out and lets the instrument see only its own portion. With `R_p` in parallel, the meter reads `R_p / (R_p + 0.1)` of the true current — **83 % with 0.5 Ω**, 91 % with 1 Ω, and every one of the numbers still looks plausible. The check in §5 step 7 (`i_na ≈ 0`) only catches a **total** bridge, so this case escapes it. There is exactly one real defense, and it is the two-point calibration: **`SENSe:CURRent:SHUNt` measured from two known loads** absorbs a stable parallel path, because it measures the whole path with real current. What does **not** absorb it is typing `SENSe:CURRent:SHUNt 0.1` by hand — the bound accepts it, `shunt_measured` is set to 1, and the instrument prints `q=cal` over scaled numbers. Never type the nominal value.

A handheld multimeter resolves 0.1 Ω to within several percent, so this is **a sanity check, not the calibration**. The real shunt measurement is the two-point calibration (`CALibration:SHUNt`, `calibration/README.md`; its result is written with `SENSe:CURRent:SHUNt <ohms>`), which measures the whole path — element, solder, pads and terminals — with the same current the instrument will use. The `R100` is a **two-terminal** shunt (not Kelvin), so part of the screws' resistance is inside the number: **tighten the screws before calibrating, and do not touch them again afterwards.**

### 1.3 — The `VBUS` question (the important one)

> **Measure both, and read them together. Either one on its own is ambiguous.**
>
> - Probes on `VBUS` and on `VIN+`.
> - Probes on `VBUS` and on `VIN−`.

| `VBUS`↔`VIN+` | `VBUS`↔`VIN−` | It is… | What changes downstream |
|---|---|---|---|
| **≈ 0 Ω** (the probes alone) | **≈ 0.1 Ω** (through the shunt) | **Case 1 — `VBUS` already tied to `VIN+`** | **Fit no wire at all** between `VBUS` and `VIN+`. Skip row A5 of §2. At startup: `CALibration:VBUSside SOURce`. |
| **≈ 0.1 Ω** | **≈ 0 Ω** | **Case 2 — `VBUS` tied to `VIN−`** | **Do not run `CALibration:VBUSside SOURce`**; leave it at `unknown`. The **current is still correct** if you take the null the way §5 step 9 requires (cell connected, DUT unplugged): the null absorbs the pin's 3.8 µA. What is **not** correct is the **voltage**, nor the **power**: see §7. |
| **high / open** | **high / open** | **Case 3 — `VBUS` floating** (which is what the photo suggests) | **Fit the jumper yourself**, `VIN+`→`VBUS`: row **A5** of §2. Then `CALibration:VBUSside SOURce`. |

**The nuance that makes this measurement honest.** The 1 MΩ of `ZVBUS` is an *active-mode* specification ([DS] §6.5); with the board unpowered, all there is between `VBUS` and everything else is ESD protection. So "open" may read hundreds of kilohms, or a diode drop on the diode range, and **that is not a tie**. What distinguishes a tie is that it is **ohmic and nearly zero**. If in doubt, use the diode range: 0.3–0.7 V one way and open the other is a protection device; a tie reads ~0 mV both ways.

**Why it matters so much.** The `VBUS` pin has a typical input impedance of 1 MΩ ([DS] §6.5 `ZVBUS`), which is **3.8 µA at 3.8 V** and **5.0 µA at 5 V**. If that current enters on the load side, it goes through the shunt and the instrument counts it as DUT consumption: **+38 % to +50 % on a 10 µA reading**, **seven to ten times** the ±0.5 µA residual the whole instrument is designed around (3.8 / 0.5 = 7.6; 5.0 / 0.5 = 10).

**And yet case 2 does not block the sleep campaign**, because those 3.8 µA are a **constant** current that the null can absorb. There is a single condition, and it is the one §5 step 9 fixes: **the null is taken with the cell connected and only the DUT unplugged**, so that during the null the shunt carries exactly the same `VBUS` pin current it will carry during the measurement. Then `i_na = (shunt_pv − offset_pv) × 1000 / shunt_uohm` (`scale.h:92-98`) subtracts it on its own: with a 10 µA DUT, `(1.38 µV − 0.38 µV) / 0.1 Ω = 10.0 µA` exactly. All that survives is the **common-mode drift** between the null and the measurement: nulling at 4.1 V and measuring at 3.6 V is 0.5 V / 1 MΩ = **±0.5 µA**, which doubles the instrument's floor. That is why case 2 does not call for lifting a trace on a brand-new breakout: it calls for **re-nulling at the working cell voltage**. What case 2 does genuinely break is the voltage and the power (§7).

### 1.4 — The `V+` jumper, which might save you the wire

In the photo, to the left of the chip, there is a two-pad solder jumper silkscreened **`V+`**, and it is **open** (two separated half-pads, no solder). **What it would join if you bridged it is not legible in the photo**, and it is not going to be assumed here.

> **Put one probe on each half-pad of the `V+` jumper, and then each half-pad against `VIN+` and against `VBUS`.**

| If you read | The case | What changes downstream |
|---|---|---|
| One half-pad **≈ 0 Ω** to `VIN+` and the other **≈ 0 Ω** to `VBUS` | The jumper is exactly the jumper of row A5 | **Bridge `V+` with solder instead of fitting wire A5.** Then repeat measurement 1.3: it must become case 1. |
| Any other combination | The jumper does something else | **Leave it open**, and fit wire A5 if you are in case 3. |
| Both pads open against everything | Continuity cannot read it | **Leave it open** and fit wire A5. |

It ships open from the factory, so as long as you do not touch it nothing changes.

### 1.5 — That `VCC` does not touch the measured path

> **Probes on `VCC` and on `VIN+`. Then on `VCC` and on `VIN−`. Then on `VCC` and on `VBUS`. Then on `VCC` and on `GND`.**

| Pair | If you read | What it means |
|---|---|---|
| `VCC` ↔ `VIN+` | **open** | Correct. |
| | continuity | Something ties the chip's supply to the measured rail. **Stop** and find out what, before you put any voltage on `VIN+`. |
| `VCC` ↔ `VIN−` | **open** | Correct. |
| | continuity | The same, and worse: the INA228's 640 µA of `IQ` would flow **through the shunt**, 64 times the sleep floor. **Stop.** |
| `VCC` ↔ `VBUS` | **open** | Correct. |
| | continuity | The board ties the bus pin to its own supply: the bus voltage would read **a flat 3.3 V forever**, and `v_mv` and `p_mw` with it, while the current stays perfectly correct. It is the quietest failure this board can have. **Stop.** |
| `VCC` ↔ `GND` | **open** (megohms, MLCC leakage) | Correct. |
| | ~0 Ω | A short. **Stop.** |

Take this measurement with the `V+` jumper **open**, and bear in mind that an open `VCC ↔ VIN+` with the jumper open **tells you nothing** about what the jumper does once it is bridged.

### 1.6 — The three `01C` resistors

In the photo there are **three** identical resistors marked `01C` (two together at the right edge, near the `VCC` end of the header, and one below and to the right of the chip). `01C` read as EIA-96 is **10 kΩ**. That is the count and the value TI's reference schematic draws ([DS] Figure 8-2: pull-ups from VS to SCL, SDA and ALERT). It stays a hypothesis until you measure it.

> **Probes on `VCC` and on `SDA`. Then `VCC` and `SCL`. Then `VCC` and `ALE`.**

| If you read | The case | What changes downstream |
|---|---|---|
| **≈ 10 kΩ** on all three | On-board pull-ups to `VCC`, the way TI draws them | **Keep the I²C wires under about 10 cm**, or fit 4.7 kΩ in parallel across `SDA` and across `SCL`. At 400 kHz with 10 kΩ the bus-capacitance budget is only **35 pF** (the I²C specification's 300 ns rise time: 300 ns / (0.8473 × 10 kΩ) ≈ 35 pF). |
| **Open** on one of them | That line has no pull-up on the board | The default pin map enables the ESP32-S3's internal pull-ups on all three lines (`i2c_bus::begin()` in `i2c_bus.cpp`, `Sampler::begin()` in `sampler.cpp`), so it **starts up anyway**. But the internal one is weak (~45 kΩ): shorten that line and keep it away from the DUT's wires. |
| **Something very different from 10 kΩ** | They are not `01C`, or they do not go to `VCC` | Redo that arithmetic, 300 ns / (0.8473 × R), with the real value before trusting 400 kHz. |

And one more, to rule out the opposite:

> **Probes on `GND` and on `SDA` / `SCL` / `ALE`** → all three **open**. A pull-**down** there would be wrong.
>
> **Probes on `VIN+` and on `SDA`** → **open**. If there is continuity, the resistors pull on the measured rail and not on `VCC`. **Stop.**

### 1.7 — No short to ground on the bare board

> **Probes on `GND` and on `VIN+`; then `GND` and `VIN−`; then `GND` and `VBUS`.** All three **open**.

The shunt network is isolated from the chip's ground on the bare board; the link to the DUT's ground is one you make yourself with a wire, deliberately (row A4 of §2).

### 1.8 — The carrier's terminal 21 (`5V`)

This one closes open question 2 of `carrier-board.md` §9, and it is the one that decides whether the setup the owner prefers is even possible (§6).

> **With the CH343 cable plugged in and nothing else connected, measure left-rail terminal 21 (`5V`) against terminal 22 (`G`).**

| If you read | The case | What changes downstream |
|---|---|---|
| **≈ 0 V** | The `IN-OUT` jumper does what the vendor says: terminal 21 is **input only** | Powering the DUT from there requires **bridging the `IN-OUT` jumper with solder**, which is exactly what the vendor discourages if something external might also feed 5Vin: with the diode bypassed the path is bidirectional and it **back-feeds the USB host** (`carrier-board.md` §6). See §6. |
| **≈ 4.5–4.8 V** | A Schottky drop from VBUS | Usable, but that drop **grows with current**, so the rail sags exactly when the DUT draws most (§6). |
| **≈ 5.0–5.2 V** | VBUS passes straight through | Electrically usable; the other arguments of §6 still stand. |

The photo shows the **`IN-OUT` jumper open** (two separated pads with a clean gap) and three SOD-123 diodes marked `S4` to its right. The photo cannot say which way they face, nor how many of them are in the 5 V path.

> **Measure terminal 1 (`3.3`) against 22 (`G`) too: it must read ≈ 3.3 V**, the output of the `1117C 33` LDO.

### 1.9 — Which of the two USB-C ports is the CH343 one

The photo does not settle it, and it does not need to: two physical clues point in opposite directions (`carrier-board.md` §7).

> **Plug in one cable only and ask the USB tree:**
> ```sh
> ioreg -p IOUSB -w0 -l | grep -E '"(USB Product Name|idVendor|idProduct|USB Serial Number)"'
> ```
>
> - `0x1A86 / 0x55D3` (6790 / 21971) → **this is the CH343. Use this one.**
> - `0x303A / 0x1001` (12346 / 4097) → this is the native USB port. **Leave it unplugged.**

Mark it with a dab of paint and never think about it again. A reset on the native port takes the host's descriptor down with it, and the capture along with it.

---

## 2. The connection table

### 2.1 Control side — ESP32-S3 (carrier) ↔ INA228

**Five wires, identical in every case and in both measurement positions.** The three GPIOs come from the firmware as built, not from a design note.

| # | From (board, terminal) | To (board, terminal) | Signal | Why |
|---|---|---|---|---|
| **C1** | INA228, header pin `VCC` | Carrier, **left rail terminal 1** (`3.3`) | 3.3 V (VS) | The board's pull-ups leave the bus idling at `VCC`; with 5 V there, `SDA`/`SCL`/`ALE` would sit at 5 V against 3.3 V pads. And [DS] §6.5 characterizes the part **at VS = 3.3 V**, which is where the ±1 µV of offset that everything depends on holds. |
| **C2** | INA228, pin `SDA` | Carrier, **left rail terminal 4** (`4`) | I²C SDA | GPIO4 — the default pin map's `sda`, `firmware/board/pins.toml`. |
| **C3** | INA228, pin `SCL` | Carrier, **left rail terminal 5** (`5`) | I²C SCL | GPIO5 — the default pin map's `scl`, `firmware/board/pins.toml`. |
| **C4** | INA228, pin `ALE` | Carrier, **left rail terminal 6** (`6`) | ALERT (pace) | GPIO6 — the default pin map's `alert`, `firmware/board/pins.toml`. Optional: the instrument paces itself off the host timer whenever the pin does not pulse per conversion, and says so (`pace=timer`, `Sampler::measurePace()` in `sampler.cpp`). |
| **C5** | INA228, pin `GND` | Carrier, **left rail terminal 22** (`G`) | ground / return | The chip's own reference: nothing works without it, and `VBUS` measures **with respect to the chip's ground** ([DS] §7.3.1). |

**Three terminals you do not touch:**

- **Left 3 (`RST`)** is the chip *enable*, not a GPIO. A wire or a stray strand there reboots the board in the middle of a capture. It sits right between the spare `3.3` and `SDA`.
- **Left 21 (`5V`)** never reaches the breakout's `VCC`.
- **Right 37, 36, 35** are printed on the rail and are the octal-PSRAM lines of an R8 part. They are decoration.

**Leave terminal 2 empty** and keep the `VCC` wire clear of terminal 3. **Run the ground wire right alongside `SDA` and `SCL`**, not by some other route: loop area is where the rise-time budget of 1.6 gets spent. If terminal 22 is awkward to reach, **terminal 1 of the right rail** (`G`) is the same net and sits at the top of the other rail; either one will do.

### 2.2 Measurement side — position A: the shunt in the cell's positive wire

**This is the default position, and the only one that can measure a 10 µA sleep.** The reason, and the comparison with the setup the owner prefers, are in §6.

The path is: **cell + → `VIN+` → R100 → `VIN−` → the board's battery +**. Ground does **not** pass through the meter.

| # | From (board, terminal) | To (board, terminal) | Signal | Why |
|---|---|---|---|---|
| **A1** | LiPo cell, **+** terminal (through the pigtail that accepts the cell's connector) | INA228, terminal **`VIN+`** | +cell, **source side** | `IN+` is the shunt's supply side ([DS] Table 5-1). |
| **A2** | INA228, terminal **`VIN−`** | DUT battery socket, **+** terminal (through the pigtail that goes into the board) | +cell, **load side** | `IN−` is the load side ([DS] Table 5-1). With A1, the `R100` sits in series and is the conductor you have cut. |
| **A3** | LiPo cell, **−** terminal | DUT battery socket, **−** terminal | cell ground, **pass-through** | The negative wire is **not cut**. The shunt always goes in the positive: the DUT's USB cable, whose ground is never cut, would short out a shunt placed in the ground. |
| **A4 · a tap, never a cut** | The pass-through wire of A3, **on the cell side** (**−** terminal) | Carrier, **left rail terminal 22** (`G`) | common ground | **It is a third wire taken off A3 without interrupting it**, not a cut-and-route row like A1 and A2. It sets the common mode of `VIN+`/`VIN−`. Without it, and with the DUT's USB unplugged, the instrument prints numbers that mean nothing — and it does not warn you. |
| **A5 · case 3 only** | INA228, terminal **`VIN+`** | INA228, terminal **`VBUS`** | bus voltage, **source side** | Puts the `VBUS` pin's 3.8 µA **upstream** of the shunt, where they are not counted. It is what TI draws ([DS] Figure 8-2). A short piece of wire between the two screws. |
| **A5 · case 1** | — | — | — | `VBUS` is already tied to `VIN+` on the board: **fit no wire.** |
| **A5 · case 2** | — | — | — | `VBUS` is tied to `VIN−`: **fit no wire and do not declare `source`.** See §7. |

**With the relay module fitted, A2 is two wires and a contact**: `VIN−` → K1 `COM`, K1 `NC` → pigtail 3, and the bypass bridges `VIN+` to that same `NC` screw when told to (since 2026-11-20 the Relay 2 Click's LCA717, before it the HW-316's K2). Released, both leave this table exactly as it reads. §8 has the rows and what the contact adds.

**As wired since 2026-11-20 (`hardware/rig.kicad_sch` rev D)**: pigtail 2, the cell's +, lands on the breakout's **`VBUS`** screw, and the A5 wire (JP2 on the drawing) carries it on to `VIN+`. It is the same node as A1 plus A5, landed the other way round; `VBUS` reads the cell's side. The calibration against the 100 Ω reference moves the cell's + to `VIN+` and takes that wire off (`calibration/README.md`, the schematic's sheet 2).

**What is not lost by measuring on the source side.** Almost all of the voltage the DUT sees falls out of the arithmetic without a second pair of wires: it is the bus voltage minus the shunt drop, which *is* the offset-corrected shunt voltage. The firmware already does it this way — `SYSTem:STATus?` prints `vbus_uv` (source) and the stream prints `v_mv` (delivered) (`architecture.md` §3.6, `scale::dutPicovolts()` in `scale.h`).

**But `v_mv` is an upper bound, not the truth.** The arithmetic subtracts the **shunt**, and `SENSe:CURRent:SHUNt` measures "the whole path — element, solder, pads and terminals" (§1.2): neither of the two includes the **two lengths of pigtail** that exist only because the meter is in the loop. If the JST 1.25 pair is 28 AWG (0.213 Ω/m), 20 cm is **~42 mΩ**, and with the two screw joints call it **50–60 mΩ unaccounted for**, against the shunt's 100 mΩ. At 160 mA that is **8–10 mV** the DUT loses and `v_mv` does not show; at 500 mA, **25–30 mV**. It is little — 0.26 % and 0.8 % on 3.8 V — but it is systematic. **Use the shortest, thickest pigtail the connector accepts**, and measure its resistance if you ever quote `v_mv` to three figures.

**The drop across the shunt**, so that it comes as no surprise, and what sits beside it:

| State | I | Drop across the shunt (0.100 Ω) | % of range 0's span (±163.84 mV) | The pack's own drop (R_int ≈ 0.2 Ω) | The pigtail's drop (~55 mΩ) |
|---|---|---|---|---|---|
| Deep sleep | 10 µA | **1.0 µV** | 0.0006 % | 2 µV | 0.6 µV |
| HOME (85 % brightness) | 70 mA | 7.0 mV | 4.3 % | 14 mV | 3.9 mV |
| Awake, fleet average | 160 mA | **16.0 mV** | 9.8 % | 32 mV | 8.8 mV |
| **Wi-Fi TX peak** | 400 mA | **40.0 mV** | 24.4 % | 80 mV | 22 mV |
| Pack charging | 500 mA | **50.0 mV** | 30.5 % | 100 mV | 28 mV |

The peak row is there because "160 mA" invites the thought that the peak case was never considered. It was: **nothing saturates** (range 0 reaches ±1.6384 A) and the DUT has volts to spare on every row (cutoff at 3.0 V − 50 mV = 2.95 V). The shunt always stays **below half of what the pack itself drops**, which is the Kleidos firmware repository's rule: "under 50 mV at 130 mA" (`docs/research/external-power-meter-options.md` §1.3, with `R_int ≈ 0.2 Ω` from `docs/research/consumption-self-measurement.md` §5.11).

### 2.3 Measurement side — position B: the shunt in the USB 5 V line

**Only for the milliamp ranges (PIN, HOME, BLE, SoftAP, charge current), and always as an upper bound.** Never quote a sleep number taken from here: inside the figure sit the charger, the PMIC's conversion loss, the current going into the pack, and the board's own USB-UART bridge — 0.93 mA measured where the truth was 8 µA, on an ESP32-S3 devkit (§6).

| # | From (board, terminal) | To (board, terminal) | Signal | Why |
|---|---|---|---|---|
| **B1** | USB pass-through breakout, **VBUS host side** | INA228, terminal **`VIN+`** | +5 V, source side | The hub port is the source. |
| **B2** | INA228, terminal **`VIN−`** | USB pass-through breakout, **VBUS device side** | +5 V, load side | The `R100` sits in the cable's 5 V line. |
| **B3** | USB pass-through breakout, **`GND`** | Carrier, **left rail terminal 22** (`G`) | common ground | The same as A4, and for the same reason. |
| **B4 · case 3 only** | INA228, terminal **`VIN+`** | INA228, terminal **`VBUS`** | bus voltage, source side | Identical to A5, with the same three cases of measurement 1.3. |

**The condition that makes this position legitimate: the pass-through breakout's VBUS line has to be physically cut**, so that `D+`, `D−` and `GND` still pass through and the 5 V arrives only by way of the shunt. A hub port switched off in software is **no** substitute for that cut: a `pytest`, a replug, a hub reset, a Mac reboot or a person undoes it.

**If the source were the carrier's terminal 21 (`5V`)** — the setup the owner prefers — then **B1, B2 and B3** change: B1 becomes carrier 21 → `VIN+`, B2 becomes `VIN−` → the DUT's 5 V injection point (there is no pass-through breakout in the power path any more), and carrier 22 is already B3's ground. And **three** more things are needed, in this order:

1. **The physical VBUS cut in the DUT's USB cable.** First, and in bold, because this variant no longer has the pass-through breakout in the power path and the condition of the previous paragraph **does not carry over by itself**. With `IN-OUT` bridged, the DUT's cable in a powered port and 5 V injected through `VIN−`, two low-impedance sources a few hundred millivolts apart end up joined by one wire, with current flowing backwards through the shunt, through the bypassed diode and all the way to the Mac's USB VBUS. **A hub port switched off in software is no substitute for the cut** (§4, §6).
2. **Bridging the `IN-OUT` jumper** with solder, which is what the vendor discourages (`carrier-board.md` §6).
3. **A 5 V injection point on the DUT.**

**The T-Deck has neither of the last two**: its documented connectors carry no 5 V input (the Kleidos firmware repository's `docs/hardware/devices/tdeck.md`). See §6.

---

## 3. The assembly order

**Everything unplugged before you start: the carrier's cable out, the cell out, the DUT's USB out.**

1. **Measure the bare breakout** — measurements 1.1 to 1.7 of §1. Nothing in the terminals and nothing on the header. Write the readings down and decide your **`VBUS` case** (1, 2 or 3).
2. **Measure the carrier** — measurements 1.8 and 1.9 of §1. **Both** need the CH343 cable plugged in, and both are **powered** measurements: switch the multimeter to DC volts before plugging in. Unplug it again when you are done.
3. **Orient the carrier** with `DSX063-1` legible at the **bottom** and mark terminals 1 and 22 of the left rail with a pen. The numbering of §2 only holds that way.
4. **Wire the control side**, with USB **out**, in this order: `GND`→22, `VCC`→1, `SDA`→4, `SCL`→5, `ALE`→6. The ground wire right alongside `SDA` and `SCL`; the `VCC` one clear of terminal 3.
5. **Run the checks of §4** on what is wired so far.
6. **Plug in the CH343 cable** and do the first startup **with no load** — steps 1 to 6 of §5. This is the moment to discover a misplaced wire, before there is a cell in the circuit.
7. **Unplug the cable again.**
8. **Prepare the DUT's pigtail** (position A): a pair of 2-pin JST 1.25 pigtails that, plugged into each other, reproduce the cell's connector — one accepts the cell's connector, the other goes into the board's socket. **Cut the positive wire only** and leave the negative passing through.

   > **Identifying the wire to cut is two measurements, not one.** Getting the cell's pin right and the pigtail's wire wrong leaves the shunt in the ground leg, which is exactly what the DUT's USB cable shorts out (A3) — a meter that lies without warning, and the DUT's ground sitting at the shunt's burden voltage.
   >
   > 1. **The cell's pin.** The polarity of the T-Deck's battery connector is a documented gotcha (the Kleidos firmware repository's `docs/research/external-power-meter-options.md` §2, its row for the T-Deck, which cites the Meshtastic thread about that connector). Measure the cell's two pins with the voltmeter, label which one is the **+**, and **do not trust the color of the insulation**.
   > 2. **The pigtail's wire.** Between the cell's pin and the wire you are about to cut there are **two mated connectors**. Plug the two pigtails into each other, plug the cell into the one that accepts it, and use the voltmeter to find **which wire of the pigtail that goes into the board is at the potential of the cell's +**. Mark that wire.
   > 3. **Unplug the cell.** The cutting and the stripping are done with the cell out. **That** marked wire is the one you cut.

   **The ground tap (A4) is prepared here, and it is a T, not a cut.** Solder a third wire onto the pass-through negative **on the cell side** — or put a second wire into the same crimp on that side — and insulate it. It has to end up on the cell side of the mated connector, because the null of §5 step 9 is taken by unmating that connector, and the common ground has to survive that. If instead of a T you cut the negative and routed it through terminal 22, **every milliamp of the DUT's return would cross the five-pin header's `GND` pin and the carrier's screw**: 160 mA awake, up to 500 mA charging, through a signal ground that is also the INA228's measurement reference. It does not blow up: it moves the reference under every `VBUS` reading, and it never warns you.
9. **Wire the measurement side** — rows A1 to A5 of §2.2 (or B1 to B4 of §2.3). Fit the A5 jumper **only if you are in case 3**.
10. **Tighten the screws.** All of them. Part of the resistance you are about to calibrate is in them, so this happens **before** the calibration and is not touched again afterwards.
11. **Run the checks of §4 again**, now with the measurement side wired and still with no source. Careful: **S1** changes its criterion on this second pass, and **S10** can only be done here. Read §4.
12. **Plug in the meter's CH343 cable.** The meter starts up, and the INA228 gets its 3.3 V.
13. **Plug the DUT's USB cable into its hub port.** Confirm it with `python3 qa/tools/hub_power.py status`; for the T-Deck the label is 9 (it is checked live; there is no stored table). You need it for the DUT's console and for the single-source check of §5 step 7. **If you have the USB pass-through breakout with VBUS cut, it goes here** (§6 recommendation 3).
14. **The last thing plugged in is the DUT's source**: the cell (position A) or the hub port (position B).
15. **Run the powered tests** — §5 step 7, and step 8 if the bus misbehaves.
16. **Declare the calibration, in the order of §5 step 9**: first `CALibration:VBUSside`, then the **null**. The null has its own gesture — **cell connected, DUT unmated** — and it is not a refinement: it is the measurement. Do not leave it for "later": any current that flows through the `R100` during the null window is stored in NVS as the offset, and from then on every sleep reading comes out almost at zero with `q=cal` beside it.

**Where the breakout sits.** Mount the INA228 breakout **about 5 cm from the ESP32-S3 carrier**, beside it and never stacked on it: not under the module's PCB antenna, and not over the module at all. The wiring does not change (the schematic is the same, `hardware/README.md`); only the header wires of §2.1 get longer, and they stay together as step 4 says. Until 2026-11-21 the breakout was stacked on the carrier, right under the antenna, and it picked up the meter's own radio. Measured that day on instrument `KM-0000000000`, the NW-00 spike's image (Wi-Fi linked in; the `meter` image has no radio), the null path held, `STATE` streamed on range 1: stacked, with the meter's SoftAP up at 20 dBm and no client, a 20 s null stream held **197 impulses**, one per 102.4 ms beacon, each about **−25 µV** deep (median), and the floor's sd was **36.0 µA** against 18.9 µA with the radio off; in 60 s blocks with a client pulling 200 kB/s from the SoftAP the null's mean moved by **−2.97 µV** from the radio-off block before it. Moved 5 cm away, with nothing else changed: **0 impulses** in the same 20 s, the floor's sd **18.9 µA** with the SoftAP up as with the radio off, and the same 60 s pair **−0.05 µV** apart; no state tried (radio off, station idle or sending 50 and 200 kB/s, SoftAP idle, with a client or sending 200 kB/s, transmit power 2 to 20 dBm) showed an effect the radio-off blocks did not. The impulses always went the same way, which is what RF rectified at the shunt inputs gives, and nothing on the rig filters those inputs (the bill's one capacitor, C1, decouples the INA228's supply; [DS] Figure 8-1 shows the optional input filter). Moving the board also moved range 1's null, by about −2 to −3 µV (from +1.7 to +3.0 µV stacked, radio off, to −0.3 µV after, steady for the 40 minutes that followed), most likely from handling the terminals: take fresh nulls after any such work (`calibration/README.md`, "The null, before every measurement session").

**Why the meter before the source.** Electrically it makes no difference: the INA228's inputs accept −0.3 V to +85 V of common mode independently of `VS` ([DS] §7.3.1, and the `VS` pin is separate from `IN+`/`IN−`, [DS] Figure 5-1). It is operational: this way the first thing that happens when the load appears is a valid conversion you can read, and it is the order in which the single-source check of §5 means something.

---

## 4. The checks before applying power

**This section is done twice** (steps 5 and 11 of §3) and **S1's criterion changes between the one pass and the other**, because on the second there is a DUT hanging off `VIN−`. Read both rows before you start.

Multimeter on **continuity / resistance**, with the cell **out**, the CH343 cable **out** and the DUT's USB **out** (in the order of §3, the DUT's is not plugged in until step 13) — except S9, which is a **powered** measurement and has its own block at the end.

| # | Put the probes on… | It must read | If it does not, what happens |
|---|---|---|---|
| **S1a** · pass 1, bare breakout | `VIN+` ↔ `GND` (of the breakout) **and** `VIN−` ↔ `GND` | **open**, with no qualifications (it is measurement 1.7) | **← THIS IS THE ONE THAT BREAKS SOMETHING.** With the cell connected, either of the two shorts the LiPo through the A4 ground wire. A 2000 mAh pack with no protection but its own PCM heats up, swells and can catch fire. **Do not plug the cell in until both read open.** |
| **S1b** · pass 2, measurement side already wired | the same | `VIN+` ↔ `GND`: **open**. `VIN−` ↔ `GND`: it will **NOT** be open — the DUT is there | The probes now read the **DUT's input impedance** from battery+ to ground: the PMIC's input, the bulk capacitance and the leakage. On continuity the buzzer may sound while the capacitors charge; on resistance you will see **kΩ and up**, and that is normal. **The threshold is ~100 Ω: below it, stop.** Write the reading down so you can compare it next time. |
| **S2** | Breakout `VCC` ↔ carrier terminal **21** (`5V`) | **open** | The `VCC` wire has landed on the wrong terminal. The bus would idle at 5 V: the INA228 would survive it on `SDA`/`SCL` (`VIO` goes to 6 V, [DS] §6.1) but **not on `ALE`**, which is limited to VS+0.3 V — and neither would the ESP32-S3's pads. This kills silicon. |
| **S3** | Breakout `VCC` ↔ carrier terminal **1** (`3.3`) | **continuity** | The complement of S2: it confirms the wire is where it belongs. |
| **S4** | Carrier terminal **3** (`RST`) ↔ terminals **1**, **2** and **4** | **never ~0 Ω**. Against **4** (`SDA`), open. Against **1** and **2** (`3.3`), **write the value down**: what is expected is the module's boot network, not an open | `RST` is the chip *enable*, and on these ESP32-S3 modules the `EN` pin conventionally carries a pull-up to 3.3 V with a capacitor beside it (the photo shows the `RST` button, the `BOOT` button and two `J3Y` of the auto-reset circuit). **This document does not know the value and will not invent one**: measure it and write it down. What you are looking for is that no stray strand of yours has put `RST` at ~0 Ω against anything — that reboots the board in the middle of a capture, and nothing in the protocol says why: it looks like a firmware bug. |
| **S5** | Carrier terminal **4** ↔ terminal **5** | **open** | `SDA` and `SCL` bridged: the bus never starts. `SYSTem:INFormation?` would answer `chip=none`. |
| **S6** | Breakout `VCC` ↔ `GND` | **never ~0 Ω**. Megohms **on pass 1 only**, with the breakout bare | On pass 2, with C1 and C5 already fitted, the probes measure the **whole carrier's 3.3 V rail**: its `1117C 33` LDO and all of its decoupling. It is not megohms, and it has no reason to be. What you are looking for is the absence of a **short**: a ~0 Ω puts the carrier's 3.3 V to ground. |
| **S7** | Each breakout pin ↔ its terminal from §2.1 | **continuity**, all five | A loose wire on `ALE` never raises an error: only `pace=timer` (§5). |
| **S8** | Carrier terminal **1** ↔ the module's `3V3` pin, and terminal **4** ↔ the module's `4` pin | **continuity** | Confirms that the terminal numbering you are using is the right one — that is, that the board is the right way round. |
| **S10** · pass 2 only | **Position A:** the cell's **−** terminal (the pass-through wire of A3, on the cell side) ↔ carrier terminal **22** (`G`). **Position B:** the USB pass-through breakout's `GND` ↔ carrier terminal **22** | **continuity** | **Nothing else checks the common ground.** S7 only walks the five wires of §2.1, which are the control side: **A4 and B3 fall outside that whole list**. And their absence raises no error: `VBUS` measures **with respect to the chip's ground** ([DS] §7.3.1), so without this wire the bus voltage is referred to nothing and can read anything at all, 0 included. |

And one **powered** reading, before connecting anything else — **switch the multimeter to DC volts and plug in the CH343 cable only**:

| # | With the CH343 cable plugged in | It must read |
|---|---|---|
| **S9** | Terminal **1** (`3.3`) against terminal **22** (`G`) | **≈ 3.3 V** |

**The other one that breaks something, and it is not a short but a gesture:** in position B, leaving the DUT's USB cable in a **powered** hub port while you inject 5 V through `VIN−`. Two low-impedance sources a few hundred millivolts apart are then joined by one wire, current flows backwards through the shunt and — if you bridged `IN-OUT` so that terminal 21 would be live — on through the bypassed diode to the meter's USB VBUS and to the host. It is exactly the path the vendor warns about (`carrier-board.md` §6). The defense is the physical VBUS cut of §2.3, not a port switched off in software.

---

## 5. The first test

**Open the port at 6000000 baud, releasing RTS before DTR**, or the open itself reboots the board (`carrier-board.md` §7; the baud rate in `firmware/src/sampler.h`, `kConsoleBaud`, and in `platformio.ini` as `monitor_speed` — they must agree). The witness that you did it right is that the *uptime* grows across opens, not the last-reset reason.

At startup the firmware prints **nothing**: it sets the power-on bit of `*ESR?` and queues one error for each thing that failed at boot (the INA228's bring-up as `-241`, the sampler as `107`, the relay inputs as `100`; `Console::announce` in `console.cpp`). Read them with `SYSTem:ERRor:ALL?`; `0,"No error"` is a clean boot.

### Step 1 — `SYSTem:INFormation?`

```
model=kleidos-meter fw=<ver> chip=ina228 addr=0x40 mfg=0x5449 die=0x228 rev=0x<r> shunt_uohm=100000 cal_tag=- q=uncal relays=1 bypass_part=<part> serial=KM-<hex> image=<image> elf=<hash>
```

| What you read | What it means |
|---|---|
| `chip=ina228 addr=0x40 mfg=0x5449 die=0x228` | **It worked.** The bus is alive, the part is an INA228, and it answers at the address the `G` straps in the photo imply. `0x5449` and `0x228` are the values the firmware demands (`kManufacturerId` and `kDieId` in `ina228_regs.h`). |
| `chip=none` + `-241,"Hardware missing;no device (scan found none)"` | Nobody answered in 0x40…0x4F. **Look in this order:** the `GND` wire (C5), the `VCC` one (C1), and `SDA`/`SCL` swapped or out of place (C2/C3 on terminals 4 and 5). |
| `-241,"Hardware missing;i2c transaction failed"` | The bus could not be opened, or a transaction died. `SDA` or `SCL` to ground, or the two of them bridged together (check S5). |
| `-241,"Hardware missing;not an ina228"` | Something answered, but the identifier is not TI's. It is not this board that is on the bus. |
| `-241,"Hardware missing;bad reset defaults"` | `ADC_CONFIG` or `SHUNT_CAL` did not read back their reset values. A marginal bus corrupting reads: go to step 7. |
| `-241,"Hardware missing;trim memory checksum (MEMSTAT=0)"` | The part's *trim* memory is in doubt. This is not wiring: it is the part. |
| `107,"Sampler not running;sampler engine failed to start (reboot)"` | Not wiring either: a queue, a semaphore, `ALE`'s `gpio_config` or the task failed. Reboot. |

`q=uncal` and `shunt_uohm=100000` are the normal first startup: there is no null and no measured shunt yet.

### Step 2 — `SYSTem:LED?`

```
blanked=48,38 pixels=<n> held=low <boot report>
```

It says which pins were blanked at startup. It proves no wiring; record the figure, because once the bench says which the real LED pin is, `kLedDataGpios` has to be narrowed (`architecture.md` §8).

### Step 3 — `DIAGnostic:I2C:SCAN?`

```
SCAN expected=0x40 found=0x40 n=1
```

| What you read | What it means |
|---|---|
| `found=0x40 n=1` | **It worked.** A single device, at the expected address. Closes open question 2 of `ina228-reference.md` §10. |
| `found=- n=0` | Nobody answers: the same as `chip=none` above. |
| `n=1` but a different address | A strap column other than `G` is bridged. The firmware names it instead of failing silently. |
| `n>1` | More than one device on the bus, or a cold joint in the `G` column: the part samples `A0`/`A1` **on every transaction** ([DS] §7.5.1), so a badly soldered strap shows up as an address that dances between runs. Re-solder the `G` column. |

### Step 4 — `DIAGnostic:ALERt?`

```
DIAG raw=0x00<nn> memstat=1 mathof=0 energyof=0 chargeof=0 cnvrf=<0|1>
```

What has to hold: **`memstat=1`** (if it were 0, startup would already have rejected it) and **`mathof=0`**. `cnvrf` can read 0 or 1 depending on whether a conversion has finished since the last read, and it says nothing about the wiring.

`DIAGnostic:ALERt?`, `DIAGnostic:I2C:SCAN?` and `DIAGnostic:REGister?` are **rejected while a capture is running** (the `kRefusedOpen` flag, `console.h`): stop it with `ABORt` first.

### Step 5 — `SYSTem:STATus?` with the instrument stopped

```
METER running=0 sink=idle profile=<p> period_us=0 pace=<p> ... scl_hz=400000 q=uncal
```

`running=0 sink=idle period_us=0` is the idle state. `scl_hz=400000` confirms the boot bus speed (the default pin map's `scl_hz`, `firmware/board/pins.toml`).

### Step 6 — `CALibration:DATA?`

Check that the persisted calibration is the one you expect. On the first startup `vbus_side` is `unknown`, which is the honest value until you take measurement 1.3.

**The no-load test ends here.** If everything above came out right, unplug and carry on with step 8 of §3.

### Step 7 — With the source connected: the wiring tests

Turn the source on (the cell in position A, the hub port in B) and start a capture that reads bus voltage — **`SLEEP` or `STATE`, never `TRACE`**, because `TRACE` reads only `VSHUNT` (`architecture.md` §3.5):

```
SENSe:PROFile SLEEP
INITiate
SYSTem:STATus?
```

`SLEEP` streams live, so between your `SYSTem:STATus?` and its answer you will see `METER t=… ch=… v=…` lines appearing at about 15 samples per second. That is normal; the line that starts with `METER running=` is the answer to your question.

| What you read in `SYSTem:STATus?` | What it means |
|---|---|
| `vbus_uv` ≈ the source voltage (3,000,000–4,200,000 with a cell; ≈ 5,000,000 from the hub) | **`VBUS` is wired correctly.** |
| `vbus_uv` ≈ 0 with the source live | Three causes, in this order: the A5 jumper is missing (you were in case 3 and did not fit it); the `VBUS` terminal is loose; or **the A4/B3 common-ground wire is missing** (check S10) — and in that third case the bus voltage is referred to nothing and can read anything, not only 0. |
| `i_na` positive and plausible with the DUT awake | **`VIN+`/`VIN−` are the right way round.** |
| `i_na` **negative in position A with the DUT's USB present** | **Touch nothing.** It is the charger filling the cell (see the single-source check, below). Cut the hub port and look again. |
| `i_na` **negative in position B**, or in position A **with the hub port already cut** and the DUT awake | `VIN+` and `VIN−` are swapped. **Swap them.** Nothing breaks — `VSHUNT`, `CURRENT` and `CHARGE` are two's complement, and the firmware reads neither `POWER` nor `ENERGY`: it computes power in software from the **signed** current (`scale::powerMicrowatts()`; `kRegPower`, `kRegEnergy` in `ina228_regs.h`) — but **the two voltage tokens trade places**: with the terminals swapped, `A5` leaves the bus pin on the load side, `shunt_pv` comes out negative, and `v_dut = bus − (−\|burden\|)` prints the **source** voltage under the token that promises the delivered one. |
| `i_na` ≈ 0 with the DUT awake | The shunt is bridged **completely**: the DUT has **another** source (see the single-source check, below), or `VIN+` and `VIN−` are shorted together. A **partial** bridge does not show here: measurement 1.2 catches it, and `SENSe:CURRent:SHUNt` from two known loads absorbs it. |
| `pace=alert` | On `STATE` and `SLEEP` (0.9.0 and later): the `ALE` wire (C4) works and the hardware sets the timestamps. The pin stays low from a conversion's end until `DIAG_ALRT` is read, so the sampler reads it after every edge (`docs/architecture.md` §3.3); `pace_edges=` is 7 to 9 of `pace_expect=8`. |
| `pace=timer` on `TRACE` or `BURST` | Always, by design: those profiles keep the register pointer on `VSHUNT` and cannot afford the read, so they are paced by the timer without a probe (`pace_edges=0 pace_expect=0`). |
| `pace=timer` on `STATE` or `SLEEP` | **Suspect the wire on terminal 6 before you suspect the part**: a bad `ALE` connection produces no error at all, only this (`pace_edges=0`, `Sampler::measurePace()`). An image older than 0.9.0 reports `pace=timer pace_edges=1` here, because it never released the pin. |
| `read_err=` climbing | Conversions that never made it off the bus. Step 8. |

Then `ABORt`, and `FETCh:STATistics?` reports the window's summary: `samples=… dropped=… read_err=… mean_na=… min_na=… max_na=… … q=…`.

**The single-source check** (it holds in both positions): with everything assembled and the DUT alive, cut its hub port.

```sh
python3 qa/tools/hub_power.py status
python3 qa/tools/hub_power.py off --dut <name>
```

- **Position B:** the DUT must power off **and** the meter must read ≈ 0. If the board stays alive, something else is giving it the +5 V and the shunt is being bypassed: the meter lies without warning.
- **Position A:** the DUT does **not** power off (it lives off the cell) — that is what you want, and it is why cutting the hub is this rig's "unplug the USB" from the sleep checklist. What changes is the sign: with USB present and the pack not full, the charger pushes current **into** the cell and the INA228 shows it **negative** (it reads signed, which is exactly what a PPK2 does not do). When you cut the port, the reading turns **positive and small**: that is the sign that you are now measuring what the board consumes and not what the charger delivers. If the pack was already full the previous current can be almost zero, so the reliable witness is the change when you cut, not the value before it.

> **And the sentence that explains why this whole procedure exists: with the hub port ON, position A does not measure consumption.** On a board with a power-path PMIC, the system load is served from VBUS and the cell only receives (or gives up) the difference, so what the shunt carries with USB live is the **cell's net current** — charge current, almost always — not what the board consumes. The consumption figure **only exists with the port cut**. That is the entire reason the *arm-and-cut* procedure (start the capture, then cut the DUT's hub port so that it runs on its cell alone) exists, and it is why §6 lists it as position A's only cost.

**The deterministic check, which costs ten seconds and does not depend on the pack's state of charge:** with the **hub port cut** and the cell connected, **loosen the `VIN−` screw**. The DUT has to **die**. If it stays alive, something is feeding it around the shunt and the meter is reading a fraction of the truth. Tighten the screw again before calibrating (§1.2: part of the resistance you are about to measure is in it).

Cutting the port is safe for the meter: the source side falls to 0 V while the DUT's capacitors discharge, the differential stays five orders of magnitude below the ±40 V absolute maximum ([DS] §6.1), and the meter is on a Mac-adapter port that `uhubctl` cannot cut.

### Step 8 — If the bus misbehaves

**Do not drop the speed and carry on measuring.** `SYSTem:COMMunicate:I2C:FREQuency 100000` is only good for the `SLEEP` profile: at 100 kHz, `TRACE` would need 136 % of the period and `STATE` 141 %. The right order is:

1. `SENSe:PROFile SLEEP`, `INITiate`, `ABORt` at 400 kHz and then with `SYSTem:COMMunicate:I2C:FREQuency 100000`, and compare `read_err=`. If the failures go away at 100 kHz, **it is rise time**.
2. The fix is to **shorten the `SDA`/`SCL` wires, or fit 4.7 kΩ in parallel across each of them**, not to stay at 100 kHz.
3. `SYSTem:COMMunicate:I2C:FREQuency` is rejected while a capture is running; stop it first with `ABORt`.

### Step 9 — What is left to declare before the first number

1. **`CALibration:VBUSside SOURce`**; `CALibration:VBUSside?` then answers `SOURCE`. **Only if measurement 1.3 ended in case 1, or in case 3 with the A5 jumper fitted.** In case 2, leave it at `unknown`: that is the truth.
2. **The null.** The gesture, with no possible ambiguity:

   > **Unmate the two JST pigtails**, leaving the cell plugged into its own and the other plugged into the DUT. That is: **the cell stays connected to `VIN+`**, both screws stay tight, and the only thing opened is the path from `VIN−` to the DUT's battery+. (The A4 ground tap hangs off the cell side of the connector — §3 step 8 — so it survives this.)
   >
   > Then: `SENSe:PROFile SLEEP`, `CALibration:ZERO 60`, `ABORt`. Write `sd_pv=` down.

   **Why this way and not by unplugging the cell.** Two reasons, and both of them are measurable:

   - **The common mode.** The null has to be taken at the same common mode you are going to measure at. With the cell out the common mode is 0 V and the measurement happens at 3.6–4.2 V; the stored offset does not distinguish common mode, and `q=` does not detect it.
   - **Case 2.** If measurement 1.3 gave case 2 (`VBUS` tied to `VIN−`), the pin's 3.8 µA cross the shunt during the null as well as during the measurement, and **cancel on their own** (§1.3). With the cell out they do not flow during the null, and they are counted in full during the measurement.

   And the reason the null is not left for afterwards: `CALibration:ZERO` persists its result in NVS (`console.cpp` `finishZeroRun` → `setOffset` + `save`). **Any current that flows through the `R100` during the window is stored as the offset.** At 0.1 Ω, 10 µA is 1 µV — which is exactly the part's maximum offset, and exactly what you are trying to measure. The null **is** the measurement, not a refinement.

   `CALibration:ZERO` answers nothing and returns control at once (`CALibration:ZERO:RESult?` reports `running=1 …` meanwhile); `ABORt` closes the window and `CALibration:ZERO:RESult?` then reports the null. A bad null shows up in `sd_pv=`.

   **With the relay module fitted (§8), `ROUTe:NULLpath ON` is this gesture without hands**, and with the DUT still powered: the bypass (`k2`, the Relay 2 Click since 2026-11-20) feeds it around the shunt, K1 opens the path from `VIN−` to its battery +, and the cell never leaves `VIN+`. Then `CALibration:ZERO 60`, `ABORt`, `ROUTe:NULLpath OFF`. One caution about the hand gesture as written above: on the rig as drawn (`rig.kicad_sch`, rev B onward) the mated pair J2 sits on the **cell's** side of the shunt, between pigtails 1 and 2, so unmating it takes the cell off `VIN+` — the opposite of what this step asks. The hand gesture that matches it on that rig is **unplugging pigtail 3 from the DUT's battery socket**, which is the side K1 opens.

   **The null is per range.** NVS stores one for each (`off_pv_r0` / `off_pv_r1`, `nulled_r0` / `nulled_r1`, which `CALibration:DATA?` prints). If you ever switch with **`SENSe:CURRent:RANGe:INDex 1`, repeat the `CALibration:ZERO` in that range**: the firmware will not warn you, because it looks at the live range's `nulled`. The campaign switches on its own since 2026-10-31: TD-08 and TD-16's `lsleep` and `c3` plans capture on range 1, every other row on range 0 (the Kleidos firmware repository's `docs/testing/power-campaign-tdeck.md` §1.6), and the rig leaves the instrument on range 0 when a row ends. Each row's preflight requires `nulled_r<n>=1` on every range it will use and selects it before its first null, so TD-08's two nulls are range 1's; a null whose `ABORt` line names another range than the capture's is refused. TD-08's null windows renew the other range too: with the lead open once, `SENSe:CURRent:RANGe:INDex 0`, `CALibration:ZERO`, `ABORt`, then `SENSe:CURRent:RANGe:INDex 1`, `CALibration:ZERO`, `ABORt`, so the rows that only subtract range 0's null (TD-15, TD-16's other plans, the element rows) keep a fresh one.

3. And the shunt's real resistance, **calibrated against the 100 Ω reference by `kleidos-meter calibrate`** (`calibration/README.md`, "The shunt, two points": both ADC ranges at one current, filed in the registry, then written as the working copy with `SENSe:CURRent:SHUNt <ohms>`): that is the real shunt measurement, not the one of 1.2, and it is the only defense against a parallel path (§1.2). `SENSe:CURRent:SHUNt` is bounded to 0.05…0.2 Ω. **Never type the nominal `0.1`.** The plan this step first held, two known loads three decades apart, was superseded on 2026-11-20 by that calibration (`calibration/records/2026-11-20.md`).

**Three rules that are expensive to forget:**

- **The same 3.3 V.** Take the null and the measurement on the same, already settled 3.3 V. The part's `PSRR` is ±0.05 µV/V typical and ±0.5 µV/V maximum ([DS] §6.5), which is **±0.5 µA/V typical and ±5 µA/V maximum** at 0.100 Ω. A hundred millivolts of difference between the null and the measurement moves the null by up to 0.5 µA, which is the entire margin the instrument has. That is `PSRR`, which is the **supply**.
- **The same common mode.** **Run `CALibration:ZERO` again after any change of position (A↔B) or of source voltage.** The stored null is indexed by **range** alone, and `q=stale` is judged by die temperature alone (±5 °C, `kStaleDieMilliCelsius` in `calibration.h`): going from position A's ~3.0–4.2 V to B's ~5 V silently reuses the other one's null, and `q=` will still say `cal`. This is common-mode rejection, which is the **input**: the error budget of `ina228-reference.md` §8.5 omitted it completely, and it now carries a `CMRR` row **with no figure**, pending a read of it from [DS] §6.5 — see §7, question 13. Until then this rule is what covers it.
- **The same cell voltage, if you are in case 2.** Re-nulling at 4.1 V and measuring at 3.6 V costs 0.5 V / 1 MΩ = **0.5 µA**, which doubles the floor. Re-null at the working voltage.

---

## 6. Whether there is a better setup

**There is, and the difference is not one of cost or of convenience: it is that the setup the owner prefers cannot answer the question that justifies building the instrument.**

The preferred plan — the DUT powered from the carrier's own `5V` terminal, through the shunt — is **insertion point (B)** of the Kleidos firmware repository's taxonomy (`docs/research/external-power-meter-options.md` §2), which already judged it with a measured example:

> "**useless for deep sleep**: it includes the charger, the PMIC's conversion loss, the charge current into the pack, and — on the bridge boards — the USB-UART. Lucidar measured exactly this class of error on an ESP32-S3-DevKitM-1: **0.93 mA** at the USB side against **8 µA** at the module."

A setup that reads 0.93 mA where the truth is 8 µA is not a 10 µA instrument. Averaging does not fix it, because the error is a real current, not noise.

### The comparison, concretely

| | **Position A** — in the cell's wire (recommended) | **Position B** — in the USB 5 V line (the preferred one) |
|---|---|---|
| **What it answers** | deep sleep (10–200 µA), standby locked, the *light sleep* rows, any battery-only state | the awake mA figures (PIN, HOME, BLE, SoftAP) and the charge current |
| **What is inside the reading** | exactly what the pack sees: the SoC, the PMIC's quiescent draw, every rail, every leak | all of that **plus** the charger, the PMIC's conversion, the pack's charging and the USB-UART bridge |
| **Null stability** | the meter's port powers only the meter: `VS` sits still | the DUT's current comes out of the same 5 V node that feeds the LDO that feeds the INA228. The offset moves with the load you are measuring: a **correlated** error, which averaging does not remove |
| **Current budget** | does not arise: the cell powers the DUT | worst case **~500 mA (charging) + whatever the meter draws**, through one port, one Schottky, one header pin and one screw. The carrier's USB-C receptacle advertises nothing, so a USB-C source offers it *Default USB Power*; at several hundred mA the `S4` diodes drop more and more and the rail sags exactly when the DUT draws most. The meter has its *brownout* detector active at level 7 (`CONFIG_ESP_BROWNOUT_DET_LVL_SEL_7`, ESP-IDF's default, in the generated `firmware/sdkconfig.meter`): a reset mid-capture takes the 4 MiB `TRACE` ring with it |
| **What it needs** | a pair of JST 1.25 pigtails (~€3), ten minutes, **opening nothing** on the T-Deck. Optional but recommended: the USB pass-through breakout with VBUS cut (~€5–10) **in the DUT's cable** — see recommendation 3 | nothing that position A does not need already, unless the source is terminal 21: then **bridging the `IN-OUT` jumper** — which the vendor discourages because it back-feeds the USB host — the physical VBUS cut, and a 5 V injection point on the DUT. **The T-Deck has neither of the last two** |
| **Console during the measurement** | without the pass-through, the *arm-and-cut* procedure is needed. **With the VBUS-cut pass-through in the DUT's cable, alive the whole time** on the awake rows | alive the whole time |

And the figure that closes the argument on the improvement side: the board's own internal meter **cannot** do this. Only three boards measure current on board (the AXP192 ones), at 0.5 mA/LSB; the sleep estimate from voltage sag reaches **±556 µA after 72 h** on the T-Deck's 2000 mAh cell (the Kleidos firmware repository's `docs/research/consumption-self-measurement.md` §4.2). The INA228 in position A reaches **±0.5 µA in five minutes**. That is the whole improvement, and it exists **only** in position A.

### What the preferred setup does get right, and what is kept

Powering the DUT from the carrier itself makes the two grounds common by construction. That is the right instinct, and it is what makes the reading mean anything. It is kept here, but made **explicit**: rows **A4** and **B3** of §2 carry a dedicated ground wire in both positions, instead of inheriting the ground as a side effect of the supply.

### Recommendation

1. **Position A by default**, and it is the only one that can give a sleep number. Start with the **T-Deck**: a user-fitted cell, a JST 1.25 pigtail, ten minutes, nothing to take apart.
2. **Position B only for the mA rows**, and always declared as an upper bound. Never quote a sleep number taken from there.
3. **Buy the VBUS-cut USB pass-through breakout for POSITION A, not for B.** It is this document's one assembly improvement, it costs the same €5–10 that were already budgeted, and it removes the one drawback position A has. Fitted in the **DUT's cable**, `D+`, `D−` and `GND` pass through and the 5 V is **physically absent**, so that:

   - **the DUT's console stays alive the whole time** on the awake rows, with no *arm-and-cut*;
   - **`hub_power.py` drops out of the loop** for those rows, and with it open question 9 ("does a switched-off port still carry data?"), which stops mattering there;
   - **the cell is demonstrably the only source**, which is the deterministic form of the check of §5 step 7.

   Two honest caveats, so that this is not sold as something it is not:

   - **The real sleep number still wants the *arm-and-cut*, with the cable out.** The DUT's USB-UART bridge is powered from the cell, and with a live host on `D+`/`D−` it draws. That is not a µA row.
   - **This does not move the ±0.5 µA floor**, which belongs to the null, not to the wiring.
   - **[MEASURE] Whether the DUT enumerates without VBUS is unverified, and it is per board.** An external bridge (CH9102, CP210x) usually does; the ESP32-S3's native USB may want to detect VBUS. Try it on the T-Deck before trusting it, and if it does not enumerate, that board keeps the *arm-and-cut*.

4. **If you do B anyway, do it from the hub port with that same pass-through breakout**, not from the carrier's terminal 21. The physical cut is the interlock that a port switched off in software can never be, and it keeps the carrier's `5V` terminal and its `IN-OUT` jumper out of the problem.
5. **The hub-port cut is not lost: it changes job.** In position A without the pass-through, `hub_power.py off --dut <name>` **is** the "unplug the USB" that the production sleep checklist asks for, it removes VBUS without removing the ground, and it makes the campaign unattended.

---

## 7. What is still open

### What the owner closes with a measurement

| # | Open question | What closes it |
|---|---|---|
| 1 | **Does this board tie `VBUS` to `VIN+`, to `VIN−`, or to neither?** The photo sees only the top layer, where `VBUS` leaves on a trace of its own and touches neither of the two pours — but it is a two-sided board with vias. | **Measurement 1.3.** It is the most important measurement in the whole document. |
| 2 | **What does the `V+` jumper join if it is bridged?** Not legible in the photo. It ships open. | **Measurement 1.4.** If it turns out to join `VIN+` to `VBUS`, bridging it replaces wire A5. |
| 3 | **Are the three `01C` resistors 10 kΩ, and are they on `SDA`, `SCL` and `ALE`?** `01C` is an EIA-96 code read off the part. | **Measurement 1.6.** That is where it comes out whether 400 kHz is clean with the wires you fit. |
| 4 | **The shunt's real resistance and its tolerance.** The `R100` is marked with no tolerance, no power rating and no temperature coefficient. | **Closed on 2026-11-20**: 100.116 mΩ ± 0.085 mΩ (k = 2), calibrated against the 100 Ω reference on both ADC ranges with the screws tight (`calibration/records/2026-11-20.md`; the procedure is `calibration/README.md`, "The shunt, two points"). §5 step 9. |
| 5 | **The real I²C address.** | **`DIAGnostic:I2C:SCAN?`** (§5 step 3). Closes `ina228-reference.md` §10 q2. **Closed on 2026-11-21**: `0x40`, alone in 20 scans of 0.16.5's bench acceptance (`architecture.md` §2.2). |
| 6 | **What does the carrier's terminal 21 (`5V`) read with only USB attached?** It has never been measured; it is open question 2 of `carrier-board.md` §9. | **Measurement 1.8.** Even though the recommended setup does not use it, the number closes the question. |
| 7 | **Which of the two USB-C ports is the CH343 one?** | **Measurement 1.9** (`ioreg`). Mark the right one with paint. |
| 8 | **Which pin is the RGB LED on?** The firmware blanks both candidates (GPIO48 and GPIO38) because the module's revision cannot be identified from its markings. | **`SYSTem:LED?`** plus one observation at the bench. Once it is known, narrow `kLedDataGpios` in `firmware/src/status_led.h`. |
| 9 | **Does a switched-off hub port still carry data?** The evidence points to no, but a console was never opened during the off window. | A 60 s test with a board on a charged battery: open its console, `SYS?`, `hub_power.py off --dut <n>`, `SYS?` with a 2 s *timeout*, and `on` afterwards. |
| 10 | **How much does the meter itself draw from its own port?** Unmeasured. It turns the budget of §6 from an argument into arithmetic. | A USB meter in series with the CH343 cable, at idle and during a `TRACE`. |
| 11 | **Does `CHARGE` accumulate at 10 µA, or truncate to zero?** | A known 10 µA load for 60 s: ≈ 192 counts in range 0. Every capture already reports the accumulator at both of its boundaries, so the first `SLEEP` you run answers it. |
| 12 | **Does `ALE` pulse per conversion, or does it sit at a level?** | The instrument measures that on itself at every `INITiate` and reports it as `pace=`. **Closed on 2026-11-20**: held low from a conversion's end until `DIAG_ALRT` is read (`architecture.md` §3.3). |
| 13 | **What is the part's `CMRR`, and how far does it move the null between position A (~3.0–4.2 V of common mode) and B (~5 V)?** The error budget of `ina228-reference.md` §8.5 — copied into the 0.1.0 design record, `firmware-design.md` — listed `Vos`, `dVos/dT`, `PSRR`, gain, shunt tolerance, `IB`, oscillator and noise, and **omitted common-mode rejection**; `PSRR` covers the **supply** (`VS`), not the **input**. It has its row now, but **with no figure**. | Read it from [DS] §6.5, the same table `ZVBUS`, `PSRR`, `IQ` and `Vos` already come from, and fill the row in. Until then the operating rule of §5 step 9 covers it by brute force: **re-null after any change of position or of source voltage.** |
| 14 | **Does the DUT enumerate over USB with the VBUS line physically cut?** The improvement of §6 recommendation 3 depends on it. | Fit the pass-through breakout in the T-Deck's cable with the cell connected and open its console. If it answers, the improvement holds for that board; if not, that board keeps the *arm-and-cut*. Repeat per board: an external bridge and the ESP32-S3's native USB do not behave the same. |
| 15 | **Does terminal 21 (`5V`) stay above 3.75 V with two coils pulled in?** 3.75 V is the coil's maximum pull-in voltage; the rail has never been measured even idle (question 6), and with `IN-OUT` open the vendor says USB power does not reach it. | **§8.5 step 1 (loaded, 68 Ω) and step 8 (two coils).** If it collapses, nothing ever switches — safe, since released is the measuring circuit, but not inert: `ROUTe:NULLpath ON` refuses under a DUT drawing 2 mA or more and says `check=unverified` below that. Closing `IN-OUT` is the owner's call. |
| 16 | **Is every IN LED fully dark with its pin driven high?** The arithmetic says so (5 − 3.3 = 1.7 V against about 3 V of two LEDs in series), but the module is a clone and its LEDs are unmarked parts. | **§8.5 step 4.** If one glows, one NPN per channel fixes it, and inverts the firmware's drive polarity: the owner's decision. |
| 17 | **Does the relay null equal the hand null?** The relay null is taken with two coils drawing ~145 mA from the meter's 5 V and returning through terminal 22, which the measurement never sees. | **§8.6, the validation.** **Closed on 2026-11-20**: hand − relay = +0.057 µV, inside the 0.1 µV criterion, with the bypass on the Relay 2 Click (`calibration/records/2026-11-20.md` §2), so one coil pulled in, not two. |
| 18 | **K1's real contact resistance.** The datasheet bounds it at 100 mΩ, as new; it is in series with the DUT and outside the Kelvin sense, and as a power contact carrying micro- to milliamps it can drift. | **§8.6 check 1**, with the probes of §1.1, at every session that re-nulls. |
| 19 | **The real operate and release times, with the module's flyback diode.** The settle waits (20 ms, 50 ms) are datasheet bounds plus allowances no source gives; the diode lengthens a release by an amount no source gives for this part. | **§8.6, "The real contact times"**: a `TRACE` across `LIVE` and `FORCe` orders, mark to step. Closed on 2026-11-21 with a 100 Ω load ([`relay-timing.md`](relay-timing.md)): K1 operates in 1.96 to 2.52 ms and releases, diode and all, in 11.20 ms, inside the 20 and 50 ms waits. |

### What depends on case 2, if it turns up

If measurement 1.3 says `VBUS` is tied to `VIN−`, the verdict is **not** the one it looks like. What breaks and what does not:

| Quantity | In case 2 | How much |
|---|---|---|
| **Current** (`i_na`, `i_ua`) | **Correct**, if the null is taken the way §5 step 9 requires (cell connected, DUT unmated) | The `VBUS` pin's 3.8 µA cross the shunt during the null **and** during the measurement, so `(shunt_pv − offset_pv)` subtracts them on its own: exactly 10.0 µA on a 10 µA DUT. All that survives is the **common-mode drift** between the null and the measurement: **±0.5 µA** if you null at 4.1 V and measure at 3.6 V. Re-null at the working voltage and it goes away. |
| **Voltage** (`v_mv`) | **Low**, and nothing says so | `v_dut_pv = bus_pv − (shunt_pv − offset_pv)` does not branch on `vbus_side` (`scale::dutPicovolts()`), and in case 2 `bus_pv` **already is** the delivered voltage. It subtracts the shunt drop **a second time**: **−16.0 mV at 160 mA** and **−50.0 mV at 500 mA**. |
| **Power** (`p_mw`) | **Low** by `I²R` | `0.160² × 0.100 =` **−2.56 mW** (0.42 % of the 608 mW of 0.16 A × 3.8 V); `0.500² × 0.100 =` **−25 mW**. |
| **`vbus_uv`** | **It is the load's voltage, not the source's** | §5 step 7 and the 0.1.0 design record (`firmware-design.md`) call it "the source voltage", and `architecture.md` §3.6 the source side. In case 2 it is not, and the token does not change its name. |

**And case 2 cannot even be declared.** `calibration.h` defines `enum class VbusSide { Unknown, Source }` — **there is no `Load`** — and `CALibration:VBUSside` (`Console::cmdVbusSideSet`, `console_commands.cpp`) accepts only `SOURce|UNKNown`. A case 2 stays at `unknown`, which is honest about the uncertainty but **does not stop** `v_mv` and `p_mw` going out over the stream with the error above inside them.

So if case 2 turns up, pick one of these three, and the third is the right one in the long run:

1. **Cut the tie.** That means identifying it and lifting it, and the photo does not say where it is. **It is not needed for the sleep campaign**, which is what an earlier version of this document got wrong.
2. **Do not quote `v_mv` or `p_mw` above about 10 mA** (where the voltage error already exceeds 1 mV), and leave `vbus_side=unknown`. The µA rows are not affected by any of this.
3. **[FIRMWARE PENDING]** Ask the firmware for a `CALibration:VBUSside LOAD` that adds the `Load` value to the enum and makes `v_dut = bus_pv` and `vbus_uv = bus_pv + correctedShunt`. It is two lines in `scale.h` and one branch in `console_commands.cpp`. **This document does not touch `firmware/src`**: it is noted here as a pending change, not a made one.

### What you need to have, and may not have

| For | Part | Note |
|---|---|---|
| **Position A — the DUT's power path** | **A pair of 2-pin JST 1.25 pigtails** that, plugged into each other, reproduce the T-Deck's cell connector | ~€3 a pack. It is the **only indispensable part** for the recommended setup. The other eight boards need the case or the battery base opened, so the T-Deck goes first |
| **Position A — the DUT's USB cable** (recommended, §6 rec. 3) | **A USB pass-through breakout (male-female) that brings out VBUS / D+ / D− / GND, with the VBUS line physically cut** | ~€5–10. It goes **in the DUT's cable in position A**, not in B: it leaves the console alive on the awake rows, takes `hub_power.py` out of the loop, and makes the cell demonstrably the only source. The cut is the interlock; a hand-cut cable works electrically but it is ugly, it gets mistaken for a good cable, and its splice is unshielded |
| **Position B — the DUT's power path** | The **same** part as the row above | There is no need to buy a second one: in position B it moves into the power path. If you want both at once, then it really is two |
| **The I²C bus, if measurement 1.6 gives 10 kΩ and the wires end up long** | **Two 4.7 kΩ resistors**, one in parallel across `SDA` and the other across `SCL` | They cost 1.0 mA of sink at 3.3 V, well inside the digital output's 10 mA maximum ([DS] §6.1) |
| **The measurements of §1** | A multimeter that resolves **0.1 Ω** and that has a diode range | The diode range is the one that distinguishes a real tie from an ESD protection device in measurement 1.3 |
| **Closing question 10** | An in-line USB meter | Optional |

## 8. The relay module — the null and the cold power cycle without hands

The two hands-on gestures of this rig are opening the DUT's battery lead for the null (§5 step 9) and cutting the DUT's power completely. An **HW-316 four-relay module** does both on a console order. It is four **SRD-05VDC-SL-C** relays — Form C (`COM` / `NC` / `NO`), contacts rated 10 A at 30 VDC — each behind a 1 kΩ input resistor, an IN LED, a PC817 opto, an S8050 (`J3Y`) driver and a 1N4148 flyback diode, with the `JD-VCC` jumper fitted and a six-pin header `GND · IN1 · IN2 · IN3 · IN4 · VCC`. The allocation is the owner's (2026-10-30). Since 2026-11-20 only its K1 is used, and the bypass is a MikroE Relay 2 Click (below). The drawing is `hardware/rig.kicad_sch` rev D; the carrier side, and why these pins, is `carrier-board.md` §8.1; the firmware is `firmware/src/relay.h`, and the commands, their replies and their refusals are in `scpi.md`, "ROUTe: the relay module".

| Relay | Driven by | Role | `COM` | `NC` | `NO` | Released |
|---|---|---|---|---|---|---|
| **K1** | `IN1` ← GPIO7 (left 7) | **lead**, in series | `VIN−`, the shunt's DUT end | the DUT's battery + | — | **closed**: the DUT is on the shunt |
| **K2** | `IN2` ← GPIO15 (left 8) | **bypass** until 2026-11-20, across shunt and K1 | `VIN+`, the cell's + | — | the DUT's battery + | **open**: no bypass |
| **K3** | `IN3` ← GPIO16 (left 9) | spare | — | — | — | off |
| **K4** | `IN4` ← GPIO17 (left 10) | spare | — | — | — | off |

> **As wired since 2026-11-20 (rev D, the rig as it stands).** The table above is the allocation of 2026-10-30. Today:
>
> | Part | Driven by | Role | Terminals | Released |
> |---|---|---|---|---|
> | HW-316 **K1** | `IN1` ← GPIO7 (left 7), active low | **lead**, in series | `COM` on `VIN−`, `NC` on pigtail 3 (the DUT's +) | **closed**: the DUT is on the shunt |
> | Relay 2 Click, one LCA717 | its control pin ← GPIO15 (left 8), active high (the image `meter` from 0.8.0, `meter_relay2` before) | **bypass**, across the shunt and K1 | one on `VIN+`, the other on K1 `NC` | **open**: no LED current, no bypass |
> | HW-316 K2 | `IN2` unwired | spare | — | off |
> | HW-316 K3, K4 | `IN3`, `IN4` unwired; GPIO16 (left 9) and GPIO17 (left 10) reach nothing | spare | — | off |
>
> The schematic draws the Click's **RL1** (control on mikroBUS position 16, PWM); the bench uses **RL2** (control on position 1, AN, and RL2's terminals), the same circuit (`relay2-click-wiring.md`). K1 stays a mechanical relay on purpose: closed at rest, so the DUT stays fed with the meter off, and with no PhotoMOS on-resistance in series with the DUT. The Click takes `+3.3V` and `GND` from the carrier's left 1 and 22. Of the HW-316's header only `GND`, `IN1` and `VCC` are wired (a single-relay module); from 0.13.0 the firmware never drives GPIO16 and GPIO17 (`firmware/src/relay.h` `kChannels`, `ChannelSpec::wired`): the spares read `k3=spare:unwired k4=spare:unwired`, their pads are not checked, and the `ROUTe:RELay3`/`RELay4` rows of §8.5 are refused (`-241`). From 0.16.6 both pins are parked at boot, inputs with the internal pull-down (`relay.h` `kParkedPins`, `docs/architecture.md` §9), so neither floats; the temporary load of the NW-00 night test (the Click's RL1 on GPIO16) was removed on 2026-11-21. Never wire a parked pin to an HW-316 input: the pull-down sinks current through its LEDs; mark the channel wired first. Wherever the rest of this section, and every relay reply (`k2=bypass:…`), says **K2**, read **the bypass channel**: the firmware still names it `k2`, and on this rig it is the Click's LCA717; the HW-316's own K2 is not driven. The rows R4, R10 and R11 of §8.2 are replaced by the Click's C1–C5 (`relay2-click-wiring.md` step 3), and R5 and R6 are removed.
>
> **In the panel and the pin map** (0.18.0) the relays are named by number and by no part: **Relay 1** is K1, the lead, on GPIO7 and active low by default, and **Relay 2** is the bypass, the Click's channel, on GPIO15 and active high by default. Their pins and levels are the pin map's (`SYSTem:PINS?`, [`scpi.md`, "The pin map"](scpi.md#the-pin-map)), changed in the panel's Pins view ([`panel.md`, "The pin view"](panel.md#the-pin-view)); a map that moves a relay is applied only with both at rest on the shunt, and leaves them unverified until `ROUTe:NULLpath ON` passes its check on it. Which part to fit as Relay 2 is §8.8.
>
> **As wired from 2026-10-31 to 2026-11-20.** The bypass contacts R10 and R11 moved from K2 to K3 (`COM` and `NO`), and the `IN2` and `IN3` wires were swapped at the module's header, so GPIO15 (left 8) drives K3 and the firmware still calls the channel `k2` (`relay.h` is unchanged). The move was a diagnosis, not a repair: from 04:13 that night K2 closing took almost none of the current (`ROUTe:NULLpath ON` answered `bypass check failed` and `lead check failed`), the module's supply read 4.80 V released and 4.75 V with the coil on, and K3 behaved the same, taking 7-12 % at best where K2 had taken 72 % the day before. A silver power contact closing on the few millivolts across the shunt and K1 does not break its surface film (a dry circuit), so the bypass wants a contact made for it: a low-resistance MOSFET solid-state relay or a gold-contact signal relay. K1, which only has to open, still cuts the DUT cleanly.

> **Both arrived on 2026-11-03 and the firmware is ready for them:** the bypass on the Relay 2 Click is driven by the one image (from 0.8.0; the `meter_relay2` image before) and its step-by-step guide is [`relay2-click-wiring.md`](relay2-click-wiring.md); the shunt calibration against the VPG reference is `CALibration:SHUNt` and `kleidos-meter calibrate` ([`../calibration/README.md`](../calibration/README.md)). What follows is the plan as it was written when they were ordered.

> **On order (2026-10-31); the parts' datasheets are their makers' and are not kept here:**
> - **Bypass: MikroE Relay 2 Click (MIKROE-1899)**, two IXYS LCA717 OptoMOS relays (30 V, 2 A, 0.15 ohm max on-resistance, on at 2 mA max of LED current, off below 0.1 mA, 1 uA leakage). Its mikroBUS PWM pin (16) drives RL1 and AN (1) drives RL2, straight into each LED's anode: **active high**, unlike the HW-316's low-level inputs. Wiring: board GND to the carrier's GND, PWM to GPIO15 (the bypass pin), RL1's two screw terminals to `VIN+` and K1's `NC` (the R10/R11 nodes); RL2 is a spare. The firmware change it needs: a per-channel energize level in `firmware/src/relay.h` (k2 active high, driven low at rest and at boot), `test_meter_relays_fail_safe.py` updated, and this table rewritten. With the bypass at 0.15 ohm against the shunt plus K1 (about 0.2 ohm), closing it takes at least 57 % of the current, well over the check's quarter.
> - **Shunt calibration reference: VPG Y0785100R000T9L**, a 100 ohm +-0.01 % Bulk Metal Foil resistor (S102C, +-2 ppm/C typical, 0.6 W). It replaces the DUT for one capture: cell + to `VIN+`, shunt, `VIN-` to the resistor, the resistor to the cell's -; about 41 mA flows, VBUS moved to `VIN-` reads the voltage across the resistor, and the shunt's real resistance is V_shunt x 100 ohm / V_resistor, to about 0.07 % (the INA228's +-0.05 % gain on both channels), declared with `SENSe:CURRent:SHUNt` and filed as a measured gain.

### 8.1 How it is driven, and why released is the safe state

The inputs are **low-level triggered**. With `VCC` at 5 V, an input pulled **low** sinks about 2 mA through its IN LED and the opto's LED in series and energizes the relay. An input **driven high at 3.3 V, or floating**, leaves it off: the two LEDs need about 3 V between them, and 5 − 3.3 V cannot light them.

So the state nobody commands is "every coil released", and the wiring makes that the measuring circuit: K1's `NC` keeps the DUT on the shunt and K2's `NO` keeps the bypass open. The four pins float from reset until the firmware drives them (ESP32-S3 datasheet Table 2-1: no pull-down on any of them, `carrier-board.md` §8.1), with one exception the same datasheet states in Table 2-2: at **power-up** each of them drives low for typically **60 µs**. A coil needs milliseconds of current to pull in, so no contact moves, but an IN LED may flash (step 6 below watches for it). The firmware's first act, once it has read the pin map that says where the relays are, is to write each relay's output latch to its released level (high for K1, low for the Click) **before** it enables the pad as an output, so no pin ever drives the other level on its own (`relay.h`, `Bank::begin`; `docs/architecture.md` §2.6 gives the time the read takes). A reset, a reflash, a crash or a meter with no cable therefore leaves the DUT exactly where rev B of the drawing had it.

The coils (Songle's coil data chart, 5 V, 0.36 W): **70 Ω ±10 %, 71.4 mA**, guaranteed to pull in at **75 % of 5 V = 3.75 V** at most and to release below 10 %. Operation time **10 ms max**, release time **5 ms max** (10 ms in LCSC's and JLCPCB's listings), contact resistance **100 mΩ max** as new. Those are bare-coil figures, and the module's 1N4148 across each coil slows the release by an amount no source gives for this part. So the firmware waits **20 ms** after energizing a coil (the 10 ms bound plus a 10 ms bounce margin no datasheet gives) and **50 ms** after releasing one (the larger 10 ms bound, 30 ms for the diode, the same margin), `relay.h` `kOperateSettleMs` / `kReleaseSettleMs`. `ROUTe:NULLpath` takes about 100 ms each way, its checks included. The real times are measured once, §8.6: on 2026-11-21, 1.96 to 2.52 ms to operate and 11.20 ms to release ([`relay-timing.md`](relay-timing.md)).

**Nothing on the module senses a contact.** An order the console carried out is not evidence that the contact moved. `ROUTe:NULLpath ON` therefore reads the shunt before it moves anything and after each step, and undoes the path if the shunt did not do what the step predicts (`architecture.md` §6.1, "The checks"): K2 closing must take at least a quarter of the current and leave the shunt at least a sixteenth of it (0.18.0: a share that vanishes is a lead that opened), K1 opening must leave the shunt at 50 µV or less. That needs the DUT to draw **2 mA or more**; under that the order says `check=unverified`, and only this section's bench checks stand behind it.

### 8.2 The connection table

| # | From (board, terminal) | To (board, terminal) | Signal | Why |
|---|---|---|---|---|
| **R1** | Module **`GND`** | Carrier, **left 22** (`G`) | ground | The coils' return. It is the **third** conductor in that screw, beside C5 and A4: a twin ferrule, or the same net on right-rail 1, 21 or 22 if the screw will not hold three (`carrier-board.md` §8.1). |
| **R2** | Module **`VCC`** | Carrier, **left 21** (`5V`) | 5 V | Coils and input LEDs. **Never 3.3 V**: the two LEDs in series take about 3 V of it, leaving a few tenths of a milliamp through 1 kΩ — an opto that barely conducts. **And never a supply that can be up while the meter's 3.3 V is down**: an unpowered ESP32-S3 clamps every `IN` toward ground through its protection diodes, which can energize all four coils at once. Terminal 21 is the carrier's own rail and falls with it; an external 5 V, if one is ever needed, goes into terminal 21 so that the meter runs from it too (`carrier-board.md` §6). |
| **R3** | Module **`IN1`** | Carrier, **left 7** (GPIO7) | K1, lead | The default pin map's `relay1`, `firmware/board/pins.toml`. |
| **R4** | Module **`IN2`** | Carrier, **left 8** (GPIO15) | K2, bypass | **Removed 2026-11-20**: `IN2` is unwired and GPIO15 drives the Relay 2 Click (`relay2-click-wiring.md` C2). |
| **R5** | Module **`IN3`** | Carrier, **left 9** (GPIO16) | K3, spare | **Not wired** on the rig since 2026-11-20 (the owner's record): `IN3` floats, K3 never energizes, GPIO16 reaches nothing. |
| **R6** | Module **`IN4`** | Carrier, **left 10** (GPIO17) | K4, spare | **Not wired** on the rig since 2026-11-20: `IN4` floats, K4 never energizes, GPIO17 reaches nothing. |
| **R7** | The **`JD-VCC`** jumper | **fitted**, `JD-VCC` to `VCC` | coil supply | Without it the coils have no supply and nothing switches. The opto isolation it would buy is not used here: one supply, one ground. |
| **R8** | INA228, terminal **`VIN−`** | K1, **`COM`** | `VIN−` | The shunt's DUT end. |
| **R9** | K1, **`NC`** | pigtail 3, toward the DUT's battery socket **+** | DUT + | Pigtail 3 moves here from `VIN−`. Closed when released. |
| **R10** | INA228, terminal **`VIN+`** (the screw A5 joins to pigtail 2's `VBUS` screw) | K2, **`COM`** | the cell's + | **Replaced 2026-11-20** by the Click's C4 (RL2's terminal on `VIN+`). Upstream of the shunt, so the bypass current never crosses it. With A5 fitted (case 3) the `VIN+` screw holds three conductors: ferrule them. |
| **R11** | K2, **`NO`** | K1, **`NC`** (the screw R9 lands on) | DUT + | **Replaced 2026-11-20** by the Click's C5 (RL2's other terminal on K1 `NC`). The bypass lands on the DUT side of K1, so K2 alone can feed the DUT while K1 is open. |
| — | K1 `NO`, every contact of K2 (since 2026-11-20), K3 and K4 | nothing | — | Drawn with a no-connect flag. **K1 `NO` is not dead**: it joins `VIN−` whenever K1 is energized, so a strand from it to ground is a short that only appears with the coil on (§8.6 check 5). |
| — | The negatives | **not switched** | — | A3 and A4 stay as they are. The DUT's USB ground is never cut, so a switched negative would only move the return onto the USB cable. |

**Placement.** The module beside the breakout, R8–R11 short and no thinner than the pigtails — they carry the DUT's whole current, 500 mA when it charges. Keep the R1–R6 bundle apart from `SDA`/`SCL`: it switches 70 mA coils. **The module now holds the cell's +** (K2 `COM`; since 2026-11-20 the Click's terminal on `VIN+` does, `relay2-click-wiring.md` "Placement") a few millimeters from its own `GND` pin, which is the cell's −: mount it so that no loose strand, screwdriver or metal standoff can reach across, because a bridge there is the LiPo shorted through nothing (§4, S1a).

### 8.3 The four circuits

K1 and K2 together decide how the DUT's battery + is fed; `ROUTe:STATe?` names the result as `path=`.

| K1 coil | K2 coil | `path=` | The DUT is fed by | The shunt carries | What it is for |
|---|---|---|---|---|---|
| off | off | `shunt` | the shunt, through K1 `NC` | the DUT's current | **Measuring.** Boot, reset, reflash and a dead meter all land here. |
| on | on | `bypass` | K2 | nothing | **The null**, `ROUTe:NULLpath ON`. `INITiate` refuses it unless told `INITiate:FORCe`: the shunt reads nothing of a board that is running. |
| off | on | `both` | the shunt and K2 in parallel | a fraction, set by two contact resistances | A transition only. `INITiate` refuses it unless told `INITiate:FORCe`; `CALibration:ZERO` refuses it outright. |
| on | off | `none` | nothing from the cell | nothing | **The cold power cycle**, with the DUT's hub port cut too. `ROUTe:LEAD OPEN,FORCe`. |

The orders: `ROUTe:STATe?`; `ROUTe:LEAD OPEN|CLOSe` (K1); `ROUTe:BYPass ON|OFF` (K2); `ROUTe:RELay3|RELay4 ON|OFF` (the spares); `ROUTe:NULLpath ON|OFF`, which moves K1 and K2 in **make-before-break** order — ON closes the bypass first and opens the lead second, checking each step on the shunt; OFF recloses the lead first and opens the bypass second — so neither passes through `none`. An order that would take the circuit **into** `none` is refused unless it says `FORCe`; an order that would move a contact during a capture is refused unless it says `LIVE`, and is then marked in the capture (one mark per coil, as `TRACe:MARK` makes one); one that would also take that capture's shunt off the DUT's current needs `FORCe` too; nothing moves a contact during a `CALibration:ZERO`. Every capture header (`FETCh:HEADer?`) and every `CALibration:ZERO:RESult?` report carry the relay state. The null path is held at most **120 s** with nothing open, then released by the instrument itself.

### 8.4 What the relays change in the measurement

- **The current reading: nothing.** K1 and K2 sit outside the Kelvin sense, which lands on the shunt's own pins (§2.2), so a contact's resistance never enters `i_na`.
- **The burden: up to 100 mΩ more**, in series with the DUT. With the ~55 mΩ of pigtail already there (§2.2), the DUT now sees up to about 0.255 Ω between itself and the cell: **up to 41 mV at 160 mA and 128 mV at 500 mA**, against 50 mV of `R100` alone at 500 mA. Under 50 mV at 130 mA still holds (33 mV). `v_mv` stays an upper bound, and now a looser one, by K1's contact; measure it (§8.6 check 1), and again from time to time: K1's `NC` is a power contact carrying micro- to milliamps, a "dry" circuit in which a surface film can build and raise its resistance or open it intermittently, and no source found gives this part's minimum load.
- **The null is the one §5 step 9 asks for.** With `ROUTe:NULLpath ON` the cell stays on `VIN+` and `VIN−` hangs on the shunt alone, so the common mode is the measurement's, and in case 2 the `VBUS` pin's current still crosses the shunt during the null exactly as it will during the measurement (§1.3). The DUT stays powered through K2, so the cell sits at its loaded voltage rather than its open one, which is nearer the measurement still.
- **What the null now does that the measurement does not: two coils on.** About 145 mA leave terminal 21 and return through terminal 22, the screw the INA228's `GND` shares. The arithmetic says it is negligible — were the 3.3 V to move 10 mV with the coils on, `PSRR` at its ±0.5 µV/V maximum would move the null by 5 nV, 0.05 µA — but "the same 3.3 V" is a rule of §5 step 9, so it is measured (§8.5 step 8) and the null is validated once against the hand one (§8.6).
- **What a release nobody ordered costs.** A meter reset, a panic, a brown-out, the meter's USB cable lost, or its port opened with RTS and DTR in the wrong order drops both coils at once while the null path is held: K2's `NO` can open before K1's `NC` recloses, and the DUT loses its battery for a few milliseconds. The hold bounds how long the null path stays held; a harness should still close it in a `finally`.

### 8.5 First power, with nothing to switch

**Do this with the relay contacts empty and the DUT's battery lead unplugged**: a relay that misbehaves at first power must have nothing to switch.

1. **Terminal 21, loaded.** CH343 cable in, nothing else wired: a **68 Ω, 1 W resistor** from left 21 to 22 (one coil's worth), DC volts across it. **At least 4.5 V: carry on.** Under that, or ≈ 0 V: stop. An open `IN-OUT` jumper is the likely cause (`carrier-board.md` §6); closing it is the owner's call. An unloaded reading is not this check — a 10 MΩ meter on a terminal fed only through a Schottky's leakage can read close to 5 V. Remove the resistor and unplug the cable.
2. **Wire R1 to R7**, with USB out: `GND` → 22 first, then `VCC` → 21, then `IN1`…`IN4` → 7…10. Check that the `JD-VCC` jumper is on. (The rig as it stands since 2026-11-20 wires only `GND`, `VCC` and `IN1`, R1 to R3 and R7.)
3. **Unpowered checks**, continuity / resistance:
   - each `IN` ↔ its terminal, `VCC` ↔ 21, `GND` ↔ 22: **continuity**;
   - terminal **21 ↔ 7, 8, 9 and 10**: **never ~0 Ω**. A strand there puts 5 V on a 3.3 V pad;
   - terminal **21 ↔ 1**: **open**. A bridge puts 5 V on the 3.3 V rail, and on the INA228;
   - module `VCC` ↔ `GND`: **not a short**. It reads the module's LEDs and drivers; write the number down.
4. **Plug in the CH343 cable, watching the module.** Every IN LED **fully dark** — shade them with a hand, because a faint glow counts — and **no click**. A flash of an IN LED at the instant of plugging in is the 60 µs power-up glitch of §8.1 and moves nothing; a glow that stays, or a click, is a fault. A power LED on the module, if it has one, is `VCC`, not a relay. A glowing IN LED is open question 16 of §7.
5. **`ROUTe:STATe?`** → `RELAY k1=lead:closed k2=bypass:open k3=spare:off k4=spare:off path=shunt check=- pads=ok operate_settle_ms=20 release_settle_ms=50 hold_s=- hold_released=0`. `pads=ok` means each pin reads back the level it is driven to; a channel named there instead is a pin shorted to something. Then terminals **7–10 against 22: ≈ 3.3 V** each, and write down **terminal 1 against 22** to the millivolt.
6. **A reset under watch.** Press the carrier's `RST` and watch the four IN LEDs through the reboot: **none may stay lit, no relay may click**. Then unplug and replug the cable, watching again. This is the datasheet's floating-at-reset claim checked on the bench, and the case a reflash puts the module through every time.
7. **Each relay alone**, the multimeter on that relay's contacts (`COM`–`NC` and `COM`–`NO`), one order at a time:

   | Order | Reply ends | IN LED | Sound | `COM`–`NO` | `COM`–`NC` |
   |---|---|---|---|---|---|
   | `ROUTe:RELay3 ON` | `k3=spare:on … moved=k3` | IN3 lit | one click | closed | open |
   | `ROUTe:RELay3 OFF` | `k3=spare:off … moved=k3` | dark | one click | open | closed |
   | `ROUTe:RELay4 ON` / `ROUTe:RELay4 OFF` | the same for `k4` | IN4 | one click each | | |
   | `ROUTe:BYPass ON` | `k2=bypass:closed path=both … q=invalid` | IN2 lit (until 2026-11-20) | one click | closed | open |
   | `ROUTe:BYPass OFF` | `k2=bypass:open path=shunt` | dark | one click | open | closed |
   | `ROUTe:LEAD OPEN` | `109,"Order disconnects the DUT;order disconnects the dut (k1 open, k2 open: add FORCe)"` | dark | none | — | — |
   | `ROUTe:LEAD OPEN,FORCe` | `k1=lead:open path=none` | IN1 lit | one click | closed | **open** |
   | `ROUTe:LEAD CLOSe` | `k1=lead:closed path=shunt` | dark | one click | open | closed |

   The rows above are the first power check as it was run on the four-relay rig (before 2026-11-20). On the rig as wired since then, from 0.13.0, `ROUTe:STATe?` says `k3=spare:unwired k4=spare:unwired`, the `ROUTe:RELay3`/`RELay4` rows are refused with `-241`, and the bypass rows are the Click's (`relay2-click-wiring.md`).

   The `ROUTe:LEAD OPEN` refusal is the interlock working. Two clicks for one order, or a click with the wrong LED, is a swapped `IN` wire: stop and trace R3–R6.
8. **Both relays at once.** `ROUTe:NULLpath ON` → on the rig as wired since 2026-11-20 the Click's REL2 lights first and K1 clicks after it, IN1 lit (on the four-relay rig before it, two clicks, K2's first, IN2 then IN1 lit), and the reply says `path=bypass check=unverified` — with the contacts empty nothing flows, so nothing could be seen, which is what `unverified` says. With both on: **terminal 21 against 22 at least 3.75 V** (open question 15), and **terminal 1 against 22 within a few millivolts of step 5**. Write both down. `ROUTe:NULLpath OFF` → two clicks, K1's first, `path=shunt check=-`. Then `ROUTe:NULLpath ON` again and leave it: after **120 s** two clicks come by themselves, and `ROUTe:STATe?` says `path=shunt hold_released=1` — the hold working.
9. **Unplug the CH343 cable**: every IN LED goes dark and anything still energized releases.

### 8.6 Wiring the contacts, and the checks that follow

**USB out, cell out.** Fit R8 and R9 and the bypass: pigtail 3 moves from `VIN−` to K1 `NC`, `VIN−` goes to K1 `COM`, and the bypass joins `VIN+` to K1 `NC`: since 2026-11-20 the Click's C4 and C5 (`relay2-click-wiring.md` step 3), until then R10 and R11, `VIN+` to K2 `COM` and K2 `NO` to K1 `NC`. Tighten every screw — the burden and part of the calibration are in them.

Unpowered, with the probes of §1.1 subtracted:

1. **`VIN−` ↔ the DUT end of pigtail 3**: the contact plus the wire, **under about 0.2 Ω**. Write it down: K1's real contact resistance (open question 18). Repeat it at every session that re-nulls: a dry contact drifts.
2. **`VIN+` ↔ K1 `NC`**: **open**. Anything else is R10 or R11 shorting the shunt: stop.
3. **`VIN+` ↔ `VIN−`**: **≈ 0.100 Ω** again (§1.2). Lower means a strand from R8 or R10 is across the shunt: stop.
4. **S1b** of §4 again: `VIN+` ↔ `GND` open, `VIN−` ↔ `GND` the DUT's input through K1.
5. **Every contact screw ↔ `GND`: open**, K1 `NO` above all. K1 `NO` joins `VIN−` only while K1 is energized, so a strand from it to ground is invisible to every check made with the coils released, and becomes the cell shorted through the shunt the first time `ROUTe:NULLpath ON` runs. Probe K1 `COM`, `NC`, `NO` and the bypass's two terminals (since 2026-11-20 the Click's RELAY2 block, until then K2 `COM`, `NC`, `NO`) each against terminal 22.

Powered, in the order of §3 steps 12–15, with every relay released:

6. **§5 step 7 as before.** Released, the rig is the rig of §2.2.
7. **The first `ROUTe:NULLpath ON` on a limited supply.** If a bench supply is at hand, put it in the cell's place — 3.8 V, current limit a little above what the DUT draws awake — so that a contact fault that shorts the cell's + to ground trips a limit instead of a LiPo. With the DUT awake (2 mA or more): `ROUTe:NULLpath ON` → the DUT **stays alive**, the reply says **`check=ok`** and the supply's current does not jump. Then `INITiate:FORCe`, `SYSTem:STATus?`, `ABORt`: `i_na` near zero, with `q=invalid` — forced, because `INITiate` refuses `path=bypass`, where the shunt reads nothing of a running DUT, which is exactly what this step looks for. (An idle `SYSTem:STATus?` would only repeat the last capture's reading.) `ROUTe:NULLpath OFF` → the DUT's current comes back. Then the cell back in.
8. **The deterministic single-source check, without the screwdriver.** Hub port cut, cell connected: `ROUTe:LEAD OPEN,FORCe` → **the DUT must die**; `ROUTe:LEAD CLOSe` → it starts again from the cell. It replaces loosening the `VIN−` screw, and leaves the screws you calibrate through untouched.

**The real contact times, once** (open question 19). With the DUT awake, so that the shunt carries something to watch, one `TRACE` across four `LIVE` orders, K2 held closed around K1's two so that the DUT never loses its feed:

```
SENSe:PROFile TRACE
INITiate
ROUTe:BYPass ON,FORCe,LIVE       <- marks k2:closed at sample <a>
ROUTe:LEAD OPEN,LIVE             <- marks k1:open at sample <b>
ROUTe:LEAD CLOSe,LIVE            <- marks k1:closed at sample <c>
ROUTe:BYPass OFF,LIVE            <- marks k2:open at sample <d>
ABORt
FETCh:STATistics?                <- q=invalid, by design
TRACe:DATA?                      <- the marks are its K rows
```

Only the first order needs `FORCe`: from then on the capture is already `q=invalid` and has nothing left to lose. In the dump the shunt current falls to K2's share when K2 makes (mark `a`), to zero when K1 breaks (`b`), back to the share when K1 makes (`c`) and back to all of it when K2 breaks (`d`). Samples from each mark to its step, times the 280 µs period, are that contact's real time plus its bounce: `a` and `b` are operate times, which must sit under the **20 ms** settle; `c` and `d` are releases with the module's diode, which must sit under **50 ms**. Write the four numbers down; they replace the allowances in `relay.h` if they are far below them. From 0.13.0 the host tool `kleidos-meter relay-timing` runs this procedure, one relay at a time and repeated, and finds each step in the dump; its first record, and why it resolved only K1's release with the T-Deck asleep, is [`relay-timing.md`](relay-timing.md), and so is the second, of 2026-11-21, which resolved all four with the DUT replaced by the 100 Ω reference: every time well under its wait, so `relay.h` keeps its allowances.

**The validation, once, before the relay null replaces the hand one.** With the DUT in its lowest state that still draws 2 mA, so that the check can see the contacts move while its load moves the cell as little as possible:

```
SENSe:PROFile SLEEP
ROUTe:NULLpath ON                <- check=ok
CALibration:ZERO 60
ABORt                       <- write off_pv and sd_pv down
ROUTe:NULLpath OFF
```

then the hand null — **pigtail 3 unplugged from the DUT's battery socket**, the cell left on `VIN+` (§5 step 9) — `CALibration:ZERO 60`, `ABORt`, and the pigtail back in. The two offsets must **agree within 0.1 µV**, which is 1 µA at 0.100 Ω: two nulls each good to the ±0.5 µA residual of `architecture.md` §3.7 differ by up to about 0.7 µA by chance alone. A larger gap is the coil current reaching the measurement; until it is explained, the hand null stays the reference.

### 8.7 The two gestures, as commands

**The null** (§5 step 9), with the DUT powered throughout, and drawing 2 mA or more if it can, so that the reply says `check=ok`:

```
SENSe:PROFile SLEEP
ROUTe:NULLpath ON
CALibration:ZERO 60
ABORt
ROUTe:NULLpath OFF
```

A `check=unverified` null is taken all the same, and stored if its offset is within 50 µV; a `bypass check failed` or `lead check failed` refusal has already put the circuit back on `path=shunt`, and `102` (`null not stored`) leaves the previous null in place.

**The cold power cycle.** With USB present the DUT runs from its cable whatever K1 does (position A), so the hub port is cut first:

```sh
python3 qa/tools/hub_power.py off --dut <name>
```
```
ROUTe:LEAD OPEN,FORCe            <- the DUT has no source now
ROUTe:LEAD CLOSe                 <- it starts from the cell
```

`ROUTe:LEAD OPEN` alone is refused while K2 is open, because it takes the DUT to `path=none`; that is the point of `FORCe`. From 0.13.0 the two orders can be one, timed by the instrument: `ROUTe:LEAD:CYCLe 5000,FORCe` opens K1 and drives its reclose 5,000 ms after the opening's drive. From 0.13.0 the hold applies here too: a lead left open 120 s after its last window closed is reclosed by the instrument (error `114`), which only ends a forgotten cycle.

**The null through the lead alone, while the bypass is out.** With the bypass not closing (since 2026-10-31, above) every `ROUTe:NULLpath ON` refuses its check, and the null is taken inside the cold power cycle instead: `CALibration:ZERO` accepts `path=none` ("a null with the DUT disconnected", `console.cpp` `handleZero`), and its `ABORt` applies the same 50 µV limit to it, because K1 is commanded open. Nothing in the instrument checks this path (`LEAD` is an unchecked order, `vs_pv=-`), so the host makes the check `ROUTe:NULLpath ON` would have made, by reading `VSHUNT` either side of K1's move: with the hub port cut the DUT draws from its cell through the shunt, and the reading must be 2 mA or more of discharge before (positive: a charge into the cell, negative, is a port that still powers the board) and within 50 µV after. The DUT loses every supply, so its card is unmounted first, on the DUT's console.

```
SDUNMOUNT                  <- on the DUT: OK or NOOP (reason=no_sd: no slot)
```
```sh
python3 qa/tools/hub_power.py off --dut <name>
```
```
SENSe:PROFile SLEEP
DIAGnostic:REGister? 0x04                  <- VSHUNT: 2 mA or more (8.68 mV, the T-Deck awake)
ROUTe:LEAD OPEN,FORCe            <- path=none: the DUT has no source now
DIAGnostic:REGister? 0x04                  <- within 50 µV (0.002 mV on the bench)
SENSe:CURRent:RANGe:INDex 0                    <- TD-08: the other range first, on the same open lead
CALibration:ZERO 60                    <- ... path=none check=- (ABORt to finish)
ABORt                       <- range=0 offset_pv=... path=none
SENSe:CURRent:RANGe:INDex 1                    <- TD-08's own range again
CALibration:ZERO 60                    <- ... path=none check=- (ABORt to finish)
ABORt                       <- range=1 offset_pv=... path=none (TD-08's range)
ROUTe:LEAD CLOSe                 <- path=shunt: it starts from the cell
```
```sh
python3 qa/tools/hub_power.py on --dut <name>
```

`DIAGnostic:REGister? 0x04` prints the 24-bit register; the reading is the 20-bit two's-complement field in bits 23:4, at 78.125 nV a count on range 1 and 312.5 nV on range 0: the harness decodes it on the range its preflight selected, and refuses to decode it on none. The DUT reboots each time, so this is a fallback for a row that loses nothing by it: `power_campaign.py td08 --nulls lead` runs the sequence above with three reads per reading, files the board's reset reason and uptime after it as evidence that its supply went, and orders the row around the two reboots (the Kleidos firmware repository's `docs/testing/power-campaign-tdeck.md`, TD-08). A null taken this way and checked is adopted like a `ROUTe:NULLpath` null; one whose first reading was under 2 mA is not.

Before 0.13.0 `path=none` had no hold, and nothing but an order closed K1 again; from 0.13.0 the hold recloses it 120 s after the last window closed and queues `114`. The harness sends `ROUTe:LEAD CLOSe` in a `finally` and `ROUTe:NULLpath OFF` on its way out, but a host killed inside a null leaves the DUT disconnected and its hub port off until somebody sends `LEAD CLOSE` here and `hub_power.py on`. Every row's preflight reads `ROUTe:STATe?` and refuses anything but `path=shunt pads=ok`, because `INITiate` refuses only a bypassed shunt and would capture a disconnected board as one drawing nothing.

### 8.8 Which relay to fit as Relay 2

Relay 2 closes across the shunt and Relay 1's contact, where there are only about 10 mV and little current: a dry circuit. Fit a solid-state relay (a PhotoMOS), such as the MikroE Relay 2 Click with its LCA717, and not an electromechanical one such as the HW-316: its silver contacts do not close at that voltage, and `ROUTe:NULLpath ON` then answers `bypass check failed`.

Open, a solid-state relay leaks beside the shunt. The LCA717 leaks 1 µA at most with 30 V across it, at 25 °C (IXYS, LCA717 data sheet DS-LCA717-R04, page 2, "Off-State Leakage Current"); above 25 °C the data sheet gives only a typical curve (DS-LCA717-R04, page 4). Read as an ohmic resistance, that is 30 V / 1 µA, at least 30 MΩ (derived from DS-LCA717-R04, page 2) across a branch of about 0.1 Ω, the shunt and Relay 1's contact, which sees 1 µV at 10 µA and 164 mV at the INA228's full scale: the open relay then takes 33 fA (3.3 × 10^-8 µA) and 5.5 nA, 3.3 × 10^-9 of either reading. The data sheet states no figure below 30 V: held to its 1 µA at any voltage, the worst case is 1 µA, a tenth of a 10 µA reading (DS-LCA717-R04, page 2). The open relay's leakage has not been measured on this rig, nor Relay 1's contact on its own.

The two paragraphs above are the panel's note in its Pins view, word for word, and `relay2-click-wiring.md`, "Which relay to fit as Relay 2", says them too, with where each figure comes from, what the ohmic reading assumes and what was not measured (`tests/firmware/test_relay_recommendation.py` holds the three to one text). The figures are page 2 of the data sheet, DS-LCA717-R04: its "Off-State Leakage Current" is 1 µA at most with `VL=30VP` across the output, at 25 °C.

---

## Sources

In this repository:

- `docs/ina228-reference.md` — §8.1 the span and the burden rule, §8.5 the error budget and the `VBUS` trap, §9 what is settled, §10 the open questions
- `docs/carrier-board.md` — §2 the rails, §6 the `IN-OUT` jumper quotation, §7 the two USB-C ports and the RTS-before-DTR open, §9 the open questions, §10 the pre-power checks
- `docs/architecture.md` — §3.6 the two voltages and the `VBUS` side, §3.7 the null; the checks before the first wire are in `docs/firmware-design.md`, the frozen record of the 0.1.0 design
- the owner's photographs of the two boards (not published), read at up to 6×: the terminal silkscreen, the `VBUS` trace between the two pours, the open `V+` jumper, the three `01C`, the bridged `G` strap column; both carrier rails terminal by terminal and the open `IN-OUT` jumper with its three `S4` diodes
- `firmware/board/pins.toml` — the default pin map: the INA228's three GPIOs, the relays' two, the boot bus speed; `firmware/src/pin_boot.cpp` — the bring-up on the map's bus (`bringUp()`); `firmware/src/main.cpp` — the boot's order (`app_main()`); `firmware/src/console.cpp` — UART0 (`kConsolePort`, `kUart0TxGpio`, `kUart0RxGpio`), the command table (`Console::dispatch`), `SYSTem:STATus?` (`cmdStatus`), the speed bounds (`cmdBusSet`); `firmware/src/console_commands.cpp` — `SENSe:CURRent:SHUNt` and `CALibration:VBUSside` with the shunt bounds (`cmdShuntSet`, `cmdVbusSideSet`), the capture header (`cmdFetchHeader`), the closing statistics (`cmdFetchStatistics`); `firmware/src/console.h` — the command flags (`kNeedsDevice`, `kRefusedOpen`, `kNoParameters`, `kRefusedWhileOpen`); `firmware/src/i2c_bus.cpp` — the bus's pads and their pull-ups (`begin()`); `firmware/src/ina228.cpp` — `bringUp()` the seven steps and `bringUpReason()` the wording; `firmware/src/ina228_regs.h` — the address space (`kAddressFirst`, `kAddressLast`), the register addresses (`kReg…`) and the identity constants (`kManufacturerId`, `kDieId`); `firmware/src/sampler.cpp` — the alert pin (`Sampler::begin()`) and the pace probe (`Sampler::measurePace()`); `firmware/src/sampler.h` `kConsoleBaud` the console baud; `firmware/src/status_led.h` `kLedDataGpios` the two blanked LED pins; the generated `firmware/sdkconfig.meter` the brownout detector
- `README.md` — the rig as wired and "Still open, for the bench"; the profiles, the LED and the floor are `architecture.md` §3.2, §8 and §3.7
- Kleidos firmware repository: `qa/tools/hub_power.py` — the per-port VBUS cut, its CLI and its warning that a cut proves nothing by itself
- Kleidos firmware repository: `docs/research/external-power-meter-options.md` §2 (with its table of each board's battery access) and §4.7 (the parts list's pigtails) — the three insertion points, the Lucidar measurement, the per-board battery access table and the JST 1.25 pigtail
- Kleidos firmware repository: `docs/research/consumption-self-measurement.md` §4.2 — what the on-device meter can and cannot resolve
- Kleidos firmware repository: `docs/testing/production-sleep-checklist.md` — the meter in series with the cell, USB unplugged, the window length
- Kleidos firmware repository: `docs/hardware/devices/tdeck.md` — the T-Deck's connectors, and the absence of a 5 V input among them
- `hardware/rig.kicad_sch` rev D (written by `gen_rig.py`) — K1 to K4, the HW-316 driver and its header, the `JD-VCC` jumper, the Relay 2 Click as the bypass, the `VBUS`–`VIN+` wire and the nets of §8.2; its sheet 2, `rig-calibration.kicad_sch`, the four-wire calibration against the 100 Ω reference
- `firmware/src/relay.h` — the channel table, the settle times, the four paths, the make-before-break plans, the null-path checks and the hold; `firmware/src/console.h` — the `FORCe` and `LIVE` contract; `firmware/src/console.cpp` — the refusal wordings quoted in §8.5 and §8.7
- Kleidos firmware repository: `qa/measurements/power/tdeck/` — the −6.85 µV range-0 null of 2026-10-27, the largest this rig has measured, which sizes the 50 µV residual limit

Outside this repository:

- **TI INA228 datasheet, SLYS021A** (January 2021, revised May 2022) — <https://www.ti.com/lit/ds/symlink/ina228.pdf>, cited as [DS]: Table 5-1 (pin functions), §6.1 (absolute maximum), §6.3 (recommended operating conditions), §6.5 (electrical characteristics: `ZVBUS`, `PSRR`, `IQ`, `Vos`), §6.6 (I²C timing), §7.3.1 (high-side sensing and the bus input), §7.3.2 (what is computed from what), §7.5.1 + Table 7-2 (addressing), §7.6.1.5/.8/.9/.10/.11 (the signed and unsigned output registers), §7.6.1.12 Table 7-16 (`APOL`, `MEMSTAT`), Figure 5-1 (block diagram), Figure 8-1 (input filtering), Figure 8-2 (high-side application diagram)
- **IXYS Integrated Circuits Division, LCA717 data sheet, DS-LCA717-R04** (©2012; the revision is printed at the foot of every page) — page 2, "Electrical Characteristics @ 25 °C" (off-state leakage 1 µA at most at `VL=30VP`, on-resistance 0.15 Ω at most, the switching times), cited in §8.8; page 4, "Typical Leakage vs. Temperature" (typical, no limit)
- **EIA-96 three-character resistor coding** (`01C` = 10 kΩ)
- **Songle SRD series relay datasheet** (`SRD-05VDC-SL-C`), read from <https://www.circuitbasics.com/wp-content/uploads/2015/11/SRD-05VDC-SL-C-Datasheet.pdf>: the coil data chart (05 VDC, 0.36 W: 71.4 mA, 70 Ω ±10 %, pull-in 75 % max, drop-out 10 % min) and the performance table (contact resistance 100 mΩ max, operation 10 ms max, release 5 ms max)
- **ESP32-S3 Series Datasheet v2.2**, Table 2-1 "Pin Overview", the "At Reset" column for GPIO7, XTAL_32K_P, XTAL_32K_N and GPIO17, and Table 2-2 "Power-Up Glitches on Pins", a typical 60 µs low-level glitch on all four — quoted in `carrier-board.md` §8.1
- **TE Connectivity, coil suppression and relay life** — <https://www.te.com/en/products/relays-and-contactors/electromechanical-relays/intersection/coil-suppression-relay-life.html>: a diode alone across a DC coil slows the release, the reason for the 30 ms allowance in §8.1
- **uhubctl** — <https://github.com/mvp/uhubctl>, that most hubs cut the data connection together with the port's power
