<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Fitting the Relay 2 Click as the shunt bypass — the guide you follow at the bench

The HW-316's silver contacts (K2, and K3 when it was tried) do not close the ~10 mV dry circuit across the shunt and K1, so `ROUTe:NULLpath ON` answers `bypass check failed` (`docs/rig-wiring.md` §8). This guide moves the bypass to the **MikroE Relay 2 Click (MIKROE-1899)** that has been on hand since 2026-11-03 (`hardware/bom.md`): each of its two channels, **RL1** and **RL2**, is an IXYS **LCA717** OptoMOS (PhotoMOS) relay, single-pole, normally open: 0.15 Ω at most when on (0.083 Ω typical, at 1 A, two-terminal AC/DC configuration), no contact film to break, turn-on 3 ms at most (0.92 ms typical) and turn-off 3 ms at most (0.44 ms typical) at 5 mA of LED current, on at 2 mA of LED current at most, off below 0.1 mA, 1 µA of leakage at most when off, with 30 V across it (IXYS Integrated Circuits Division, LCA717 data sheet DS-LCA717-R04, page 2, "Electrical Characteristics @ 25 °C"). What that leakage costs a reading is [below](#which-relay-to-fit-as-relay-2). Either channel serves; **the bench uses RL2** (fitted 2026-11-20, `calibration/records/2026-11-20.md`), and this guide is written for it. K1 stays the lead on the HW-316, mechanical and closed at rest, and is the only relay of the module that is wired: of its header only `GND`, `IN1` and `VCC` are connected, so K2, K3 and K4 never energize. The owner's second Click is not used.

**The Click's names**, from MikroE's own material (the Relay 2 Click product page, <https://www.mikroe.com/relay-2-click>, its pinout table and board photographs). The pinout table: **RL2** on mikroBUS pin 1 (the AN position), **RL1** on pin 16 (the PWM position), **+3.3V** on pin 7, **GND** on pins 8 and 9, every other pin not connected. The silkscreen agrees: `RL2` at the first pin of the left header (the AN side), `RL1` at the first pin of the right header (the PWM side); the output terminal blocks are printed **RELAY2** (left) and **RELAY1** (right); LD1 (`REL1`) lights while SSR 1 is closed, LD2 (`REL2`) while SSR 2 is, LD3 is `PWR`. MikroE's SDK says the same (`relay2.c` in `mikrosdk_click_v2`: `relay2_relay1_control()` drives the `pwm` pin, `relay2_relay2_control()` the `an` pin). This guide uses those names only: "the RL2 control pin" is mikroBUS pin 1 (AN), "RL2's terminals" are the RELAY2 block, "RL2's LED" is REL2.

**As fitted on the bench (2026-11-20).** The bypass is the Click's **RL2**: GPIO15 drives the RL2 control pin (AN, 1), and RL2's terminals carry the bypass between `VIN+` and K1 `NC`. RL1 is a spare, its control pin open. The registry records this as configuration `mjkdz-r100-relay2click` (`calibration/configurations/`). **The schematic, rev D, draws RL1** (its control on the PWM position, 16, and RL1's terminals) and says in a note that the bench uses RL2: the two channels are the same part on the same circuit, so the drawing and the bench are one netlist; whichever channel is fitted, the registry's configuration names it. The first fitting had the GPIO15 wire on the wrong control pin and K1's GPIO7 wire off; both were found by the null-path check (`lead check failed`) and corrected.

**The firmware moves with the wiring.** The Click's input is **active high** (a pin sourcing current into the LCA717's LED) where the HW-316's is active low. From 0.8.0 the instrument has one image, `meter`, and it drives the Click: GPIO15 low at boot and high to close the bypass. Before 0.8.0 that image was `meter_relay2` (0.3.0 to 0.7.0) and the plain `meter` image drove the HW-316 the opposite way; an image older than 0.8.0 named `meter` must not run on this rig. Never leave the HW-316 bypass wired to GPIO15 with this firmware on the meter: its K2 (or K3) would close at boot and put the bypass across the shunt. `ROUTe:STATe?` and `SYSTem:INFormation?` say `bypass_part=relay2click`.

What does not change: K1 and its wires (R8, R9), GPIO7, the null-path checks (closing the Click's channel against the shunt and K1 takes most of the current: on 2026-11-20 the bypass branch measured about 34 mΩ against the shunt's 100, about 75 %, well over the check's quarter), every SCPI command, and every released state: a meter that is off, booting or reset leaves the channel open, because an LED with no current is an open PhotoMOS.

**In the panel and the pin map** (0.18.0) the two relays are named by number and by no part: **Relay 1** is K1, the DUT's battery lead, on GPIO7 and active low by default, and **Relay 2** is the bypass, this guide's RL2, on GPIO15 and active high by default. Their pins and levels are the pin map's (`SYSTem:PINS?`, [scpi.md, "The pin map"](scpi.md#the-pin-map)), changed in the panel's Pins view ([panel.md, "The pin view"](panel.md#the-pin-view)) or with `SYSTem:PINS:STAGe` and `:APPLy`; a map is applied only with the relays at rest on the shunt, and one that moves a relay leaves both unverified (`verified=0`) until the null-path check of step 7 passes on it again. The replies keep the wire's names, `k1=` and `k2=`.

## Which relay to fit as Relay 2

Relay 2 closes across the shunt and Relay 1's contact, where there are only about 10 mV and little current: a dry circuit. Fit a solid-state relay (a PhotoMOS), such as the MikroE Relay 2 Click with its LCA717, and not an electromechanical one such as the HW-316: its silver contacts do not close at that voltage, and `ROUTe:NULLpath ON` then answers `bypass check failed`.

Open, a solid-state relay leaks beside the shunt. The LCA717 leaks 1 µA at most with 30 V across it, at 25 °C (IXYS, LCA717 data sheet DS-LCA717-R04, page 2, "Off-State Leakage Current"); above 25 °C the data sheet gives only a typical curve (DS-LCA717-R04, page 4). Read as an ohmic resistance, that is 30 V / 1 µA, at least 30 MΩ (derived from DS-LCA717-R04, page 2) across a branch of about 0.1 Ω, the shunt and Relay 1's contact, which sees 1 µV at 10 µA and 164 mV at the INA228's full scale: the open relay then takes 33 fA (3.3 × 10^-8 µA) and 5.5 nA, 3.3 × 10^-9 of either reading. The data sheet states no figure below 30 V: held to its 1 µA at any voltage, the worst case is 1 µA, a tenth of a 10 µA reading (DS-LCA717-R04, page 2). The open relay's leakage has not been measured on this rig, nor Relay 1's contact on its own.

The two paragraphs above are the panel's note in its Pins view, word for word (`tests/firmware/test_relay_recommendation.py` holds the panel, this guide and `rig-wiring.md` §8.8 to one text). What they leave to the reader:

- **Where the figures come from.** The 1 µA, the 30 V and the 25 °C are the data sheet's, page 2, the maximum of "Off-State Leakage Current" (`ILEAK`, `VL=30VP`); page 4's "Typical Leakage vs. Temperature, Measured Across Pins 4&6 (VL=30V)" is a typical curve, not a limit, and no figure is read off it here. The 0.1 Ω is the shunt's 0.100 Ω, with Relay 1's closed contact added and not known on its own; 164 mV is the INA228's shunt full scale at `ADCRANGE` 0, 163.84 mV (1.64 A through the shunt). The leakage takes current around the shunt, so a reading is low by it; at the full scale the fraction is the same, 3.3 × 10^-9, and the 1 µA worst case is 6 × 10^-7 of a 1.64 A reading.
- **What the ohmic reading assumes.** A semiconductor's off-state leakage is not ohmic near 0 V: a junction's current rises with the voltage over the first tens of millivolts and then saturates. If the data sheet's 1 µA at 30 V is such a saturated current, its slope near 0 V is that current over the thermal voltage, 25.7 mV at 25 °C, about 25.7 kΩ, and at the 1 µV of a 10 µA reading the open relay takes 39 pA, 3.9 × 10^-6 of the reading; at 164 mV it is close to the whole 1 µA. That is an estimate from the data sheet's figure and a junction's physics, not a figure of the data sheet, and it lies between the two readings above.
- **What was not measured.** The open RL2's leakage, at any voltage or temperature: step 4's multimeter check reads only "open" on its megohm range, and no figure of it is recorded. Relay 1's contact resistance on its own: the null-path check of 2026-11-20 measured the bypass branch, about 34 mΩ against the shunt's 100. Measuring the first would replace the two bounds by one figure.
- **Another part as Relay 2.** The pin map sets its pin and its level, not its timing: the firmware waits 10 ms each way after the bypass moves (`relay.h` `kSsrOperateSettleMs`, `kSsrReleaseSettleMs`, sized from the LCA717's 3 ms at most), so a part slower than that needs those constants changed in the firmware, and an electromechanical one is what this note advises against.

## What you need

- The Relay 2 Click, and its pin map (the Click's own page). Its two control pins are named for the relay each drives: the **RL1** pin (mikroBUS pin 16, PWM) and the **RL2** pin (mikroBUS pin 1, AN); **GND (8 or 9)**; **+3.3V (7)**. Use the Click's own names, RL1 and RL2, when you wire: the bench uses the RL2 pin and the RELAY2 terminals.
- Three short wires for the logic (GND, the RL2 control pin and +3.3V), two short wires no thinner than the pigtails for the RELAY2 terminal block (they carry the DUT's current while the null path is held).
- The multimeter, a small screwdriver, and the CH343 cable.

**The drawing to wire from** is `hardware/rig.svg`, the render of `hardware/rig.kicad_sch` rev D (2026-11-20), the rig as wired; this guide has no photographs, and needs none. The Click is **U4** there: `+3.3V` (7) and `GND` (8), the RL1 control on the PWM position (16), RL2's on the AN position (1) drawn no-connect, and RL1's two terminals. The sheet draws RL1 where the bench uses RL2, the same circuit (the sheet's own note, and `hardware/README.md`, "Rev D"), so read RL2 wherever the drawing says RL1.

## Step 1 — Everything off

Unplug the meter's CH343 cable, unplug the DUT's battery lead (pigtail 3) from the DUT, and cut the DUT's hub port. Nothing may be powered while wires move.

## Step 2 — Take the HW-316 out of the bypass

1. Remove the bypass contact wires **R10** (`VIN+` → the HW-316 relay's `COM`) and **R11** (that relay's `NO` → K1 `NC`). Since 2026-10-31 they sit on **K3**, not K2 (`docs/rig-wiring.md` §8 "As wired since 2026-10-31"). Leave R8 and R9 (K1) exactly as they are.
2. At the HW-316 header leave only `GND`, `VCC` and **IN1** (GPIO7, left 7) wired: **IN2, IN3 and IN4 unconnected** (a floating HW-316 input is off), as the rig stands since 2026-11-20. GPIO15's wire (left 8) is now free; it goes to the Click in step 3. GPIO16 and GPIO17 (left 9 and 10) reach no HW-316 input: from 0.13.0 the firmware never drives them (the spares `k3` and `k4` read `unwired`), and from 0.16.6 it parks both as inputs with the internal pull-down (`docs/architecture.md` §9), so neither floats. The temporary load relay NW-00 wired to GPIO16 (the Click's RL1) was removed on 2026-11-21.
3. Check, unpowered: `VIN+` ↔ K1 `NC` **open** (nothing bypasses the shunt any more), `VIN+` ↔ `VIN−` **≈ 0.100 Ω**.

The header as this step leaves it is drawn in `hardware/rig.svg` (rev D), in the block "Meter carrier and HW-316 relay module: only IN1 and K1, the lead, are wired": the HW-316 is **U3**, its `IN1` on GPIO7 (T7), its `IN2`, `IN3` and `IN4` and every contact of K2, K3 and K4 flagged no-connect, and the carrier's GPIO16 (T9) and GPIO17 (T10) reach nothing.

## Step 3 — Wire the Click

| # | From | To | Signal | Notes |
|---|---|---|---|---|
| **C1** | Click **GND** (mikroBUS 8 or 9) | Carrier **left 22** (`G`) or right-rail GND | ground | The LCA717 LED's cathode return. Same ground as the INA228 and the HW-316. |
| **C2** | Click **RL2** control pin (mikroBUS AN, 1) | Carrier **left 8** (GPIO15) | RL2 drive | The wire that was R4 (HW-316 IN2/IN3). Active high: ~3.3 V closes RL2. |
| **C3** | Click **+3.3V** (mikroBUS 7) | Carrier **left 1** (3V3) | 3.3 V | The Click's supply pin (its PWR LED, LD3, shows it). Fitted on the bench and drawn in rev D. **Never 5 V.** |
| **C4** | Click **RELAY2** terminal A | INA228 **`VIN+`** (the screw that holds pigtail 2) | the cell's + | Replaces R10. Upstream of the shunt, so the bypass current never crosses it. |
| **C5** | Click **RELAY2** terminal B | K1 **`NC`** (the screw R9 lands on) | DUT + | Replaces R11. The bypass lands on the DUT side of K1. |
| — | Click **RL1** control pin (mikroBUS PWM, 16), **RELAY1** terminals | nothing | — | RL1 is a spare. Its control pin left open: with no current its LED is dark and RL1 open. |

Every wire of this table is in `hardware/rig.svg` (rev D): C1 and C3 are U4's `GND` (8) and `+3.3V` (7) on the carrier's G and 3V3, C2 is the `BYPASS` net from GPIO15 (T8) to the channel's control pin, and C4 and C5 are the channel's terminals, A on the `VIN+` node and B on `DUT+`, K1's `NC` (`hardware/README.md`, "How the drawing is checked", lists each net's pins). The sheet's channel is RL1, on the PWM position (16); on the bench it is RL2, on the AN position (1).

Each relay has two output terminals, the LCA717's AC/DC configuration (load on its pins 4 and 6, DS-LCA717-R04 "Pin Configuration"), so RL2's two terminals have no polarity: either one on `VIN+`. Wiring RL1 instead (its control pin on GPIO15, its terminals as C4/C5) works the same; whichever is fitted, the registry's configuration names it.

**Placement.** RL2's terminals now hold the cell's + (C4): mount the Click so that no strand, screwdriver or standoff can reach from C4 to any ground — the board's GND pins are a few millimeters away. Keep C4/C5 short, and C2 away from `SDA`/`SCL`.

## Step 4 — Unpowered checks

With the multimeter, every probe zeroed:

1. **`VIN+` ↔ K1 `NC`: open** (MΩ). RL2 is off; anything lower is a wiring fault across the shunt.
2. **`VIN+` ↔ `VIN−`: ≈ 0.100 Ω.** Lower means a strand across the shunt.
3. **C4 and C5 ↔ GND: open.** Either one shorted is the cell shorted the first time RL2 closes.
4. **Click RL2 control pin ↔ carrier left 8: continuity**; **Click GND ↔ left 22: continuity**.
5. **Carrier left 8 ↔ left 21 (5 V): never ~0 Ω.**

## Step 5 — Flash the image

From the Kleidos firmware checkout, with `KLEIDOS_METER_PORT` naming the CH343 port:

```sh
scripts/pio-job run --project-dir <this repo>/firmware -e meter -t upload --upload-port "$KLEIDOS_METER_PORT"
```

The upload guard still refuses any other port.

## Step 6 — First power, contacts loaded but the DUT unplugged

1. Plug the CH343 cable. The Click's **REL2 LED (LD2) stays dark** (RL2 open) and the HW-316's IN LEDs stay dark.
2. `ROUTe:STATe?` → `… k2=bypass:open … path=shunt check=- pads=ok … bypass_part=relay2click`. `pads=ok` is GPIO15 reading back low.
3. `ROUTe:BYPass ON` → REL2 lit, `k2=bypass:closed path=both`. Measure **`VIN+` ↔ K1 `NC`: ≤ 0.15 Ω** (RL2's on-resistance, 0.15 Ω at most by the data sheet, plus the wires). Write the figure down. On 2026-11-20 the null-path check put the whole bypass branch at about 34 mΩ.
4. `ROUTe:BYPass OFF` → REL2 dark, `VIN+` ↔ K1 `NC` open again.
5. Press the carrier's `RST` and watch REL2: it must **not** light through the reboot. Unplug and replug the cable: the same.

What each of these states closes is in the notes of `hardware/rig.svg` (rev D): "Released (meter off, booting, reset)" is items 1, 4 and 5, the channel open with no LED current and nothing across the shunt; GPIO15 high, item 3, closes the channel from `VIN+` to `DUT+`, across the shunt and K1 together.

## Step 7 — The null path on the DUT

Plug pigtail 3 back into the DUT and power it so that it draws **2 mA or more**:

1. `ROUTe:NULLpath ON` → the DUT **stays alive**, the reply says **`check=ok`** and `ROUTe:RESult?` shows `moved=k2,k1` with three `vs_pv` readings: the second a fraction of the first, the third under 50 µV.
2. `ROUTe:NULLpath OFF` → `moved=k1,k2`, the DUT still alive, `path=shunt`.
3. The validation of `docs/rig-wiring.md` §8.6, once: a relay null (`CALibration:ZERO 60` through `ROUTe:NULLpath ON`) and a hand null must agree within 0.1 µV.
4. The real switching times, once (`docs/rig-wiring.md` §8.6, "The real contact times"): RL2's operate and release (3 ms at most each at 5 mA of LED current, DS-LCA717-R04) must sit under the 10 ms the firmware waits (`relay.h` `kSsrOperateSettleMs`, `kSsrReleaseSettleMs`). Write them in a calibration record. Done on 2026-11-21 with a 100 Ω load (`docs/relay-timing.md`): RL2 turned on in 0.56 to 1.12 ms and off in 0.56 to 0.84 ms.

If step 1 answers `bypass check failed`, RL2 is not across the shunt: recheck C4/C5 and that REL2 lights. If it answers `lead check failed`, K1 did not open: that is the HW-316, not the Click.

## Step 8 — Record it

Write a record in `calibration/records/` (a new file, never an edit of an old one): the date, `ROUTe:STATe?` after step 6, the on-resistance of step 6.3, the `ROUTe:RESult?` of step 7.1, the two nulls of step 7.3 and the times of step 7.4. Then tell the firmware side: the Kleidos QA harness's `power_campaign.py --nulls relay` can go back to the relay null once step 7.3 agrees.

## Going back

No image from 0.8.0 on drives the HW-316 bypass: going back to it means an image older than 0.8.0 named `meter` (0.7.0 or earlier), whose drive is the opposite of the Click's. Unplug the Click's RL2 control wire **before** flashing such an image, then rewire GPIO15 to the HW-316. That image drives GPIO15 **high** at rest, as the HW-316 wants; on the Click that lights REL2 and closes the bypass across the shunt while the firmware reports `path=shunt`, so every reading would be a fraction of the DUT's current. The rig as wired since 2026-11-20 never goes back: the HW-316's silver contacts are why the bypass moved.

## Sources

- **MikroE, Relay 2 Click (MIKROE-1899)**, product page <https://www.mikroe.com/relay-2-click>: the pinout table (RL2 on mikroBUS pin 1, AN; RL1 on pin 16, PWM; +3.3V on 7; GND on 8 and 9; every other pin NC), the LED list (LD1 REL1, LD2 REL2, LD3 PWR) and the board photographs (the `RL2`/`RL1` silkscreen at the headers' first pins, the RELAY2 and RELAY1 terminal blocks). MikroE's photographs and its 2D/3D package are not copied here: their license is not stated.
- **IXYS Integrated Circuits Division, LCA717 data sheet, DS-LCA717-R04** (©2012; the revision is printed at the foot of every page): page 2, "Electrical Characteristics @ 25 °C" (on-resistance 0.083 Ω typical, 0.15 Ω maximum in the AC/DC configuration at 1 A; turn-on 0.92 ms typical, 3 ms maximum, and turn-off 0.44 ms typical, 3 ms maximum, at 5 mA and 10 V; 2 mA maximum of LED current to activate at 1 A, 0.1 mA minimum to deactivate; 1 µA maximum off-state leakage at 30 V); page 1, "Pin Configuration" (the AC/DC configuration's load on pins 4 and 6); page 4, "Typical Leakage vs. Temperature" (a typical curve at 30 V, no limit).
- **MikroE, `mikrosdk_click_v2`, `clicks/relay2/lib_relay2/src/relay2.c`**: `relay2_relay1_control()` drives the `pwm` pin, `relay2_relay2_control()` the `an` pin.
