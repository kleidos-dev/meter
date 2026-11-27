<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: CERN-OHL-S-2.0 -->

# Bill of materials

What the bench meter is built from. `bom.csv` is the schematic's own list (`kicad-cli sch export bom`); this page adds what a buyer needs. No datasheet is stored here: each line says where its maker publishes it.

| Ref | Part | Qty | Role | Notes |
|---|---|---|---|---|
| U2 | ESP32-S3-N16R8 module on a DSX063-1 screw-terminal carrier (WCH CH343P USB bridge) | 1 | the meter's MCU | 16 MB flash, 8 MB octal PSRAM; the console is UART0 behind the CH343P (`docs/carrier-board.md`) |
| U1 | TI INA228 on a generic breakout (marked MJKDZ) | 1 | current, bus voltage, power, charge | address 0x40; three `01C` 10 kΩ pull-ups on the breakout. Mount it about 5 cm from U2, never stacked under the module's antenna: stacked, it picked up the meter's own radio (`docs/rig-wiring.md` §3, "Where the breakout sits"). Datasheet: TI SBOSA20 |
| R100 | 0.100 Ω shunt, marked `R100` | 1 | the measurement | its tolerance is unmarked; its measured value and uncertainty are the calibration registry's (`calibration/`; the first, 2026-11-20: 100.116 mΩ ± 0.085 mΩ, k = 2, `calibration/records/2026-11-20.md`), never the schematic's |
| U3, K1–K4 | HW-316 four-relay module (SRD-05VDC-SL-C, PC817, low-level triggered, `JD-VCC` jumper) | 1 | K1 the DUT lead; K2–K4 spare | since rev D (2026-11-20) a single-relay module: only `IN1` (GPIO7) is wired, `IN2`–`IN4` are not, K2–K4 never energize. K1 stays mechanical, closed at rest, so the DUT is fed with the meter off |
| U4 | MikroE Relay 2 Click (MIKROE-1899): 2 × IXYS LCA717 PhotoMOS, normally open, 0.15 Ω max on (AC/DC configuration, 1 A), 3 ms max on and off at 5 mA | 1 | the shunt bypass, across the shunt and K1 | active high, driven by GPIO15 from the `meter_relay2` image; the schematic draws RL1 (mikroBUS PWM position), the bench uses RL2 (AN position), the same circuit (`docs/relay2-click-wiring.md`). The owner's second Click is not used |
| JP2 | a short wire, `VBUS` to `VIN+` on the breakout | 1 | `VBUS` reads the cell's side | pigtail 2 lands on the `VBUS` screw; taken off for the calibration (schematic sheet 2) |
| R4 | VPG Y0785100R000T9L, 100 Ω ±0.01 % Bulk Metal Foil resistor (S102C, ±2 ppm/°C typical) | 1 | the calibration reference for the shunt | in the DUT's place only for the two-point calibration, four-wire (schematic sheet 2, `calibration/README.md`, guided by `kleidos-meter calibrate`, `CALibration:SHUNt`) |
| BT1, J1, J2 | the DUT's cell and its JST 1.25 mm pigtails (T-Deck Plus) | 1 | what is measured | three 10 cm pigtails (RW1–RW3) |
| C1 | 0.1 µF | 1 | INA228 decoupling | |

Makers' pages: Texas Instruments (INA228), Espressif (ESP32-S3), WCH (CH343P), MikroElektronika (Relay 2 Click), IXYS/Littelfuse (LCA717), Vishay Precision Group (Y0785).
