<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: CERN-OHL-S-2.0 -->

# The bench rig's schematic

`rig.kicad_sch` is the **normative** wiring of the INA228 bench rig: what is soldered to what, which terminal carries which signal, and why the sense point is where it is. Its second sheet, `rig-calibration.kicad_sch`, draws the same parts rewired for the shunt's two-point calibration against the 100 Ω reference. Both are generated: edit `gen_rig.py` and re-run it, never the schematic itself, which the next run would overwrite.

```sh
python3 hardware/gen_rig.py       # write rig.kicad_sch, rig-calibration.kicad_sch, rig.kicad_pro, rig.kicad_sym, sym-lib-table
python3 hardware/export_rig.py    # ERC, the netlist check, rig.pdf, rig.svg, rig-calibration.svg (kicad-cli 10.0.8, pypdf)
cd hardware
kicad-cli sch export bom --output bom.csv \
  --fields 'Reference,Value,Footprint,${QUANTITY},Description' --group-by Value rig.kicad_sch
```

The generator is deterministic: run twice, it writes the same bytes. `rig.pdf` (both sheets, a bookmark each), `rig.svg` (sheet 1) and `rig-calibration.svg` (sheet 2) are committed renders, exported in KiCad 10.0.8's container (`kicad/kicad:10.0.8`, as CI does) in the same commit as any change to `gen_rig.py`, so the drawing can be read without KiCad; CI's `schematic` job fails when the generator does not give back the committed files or a fresh export is not byte for byte the committed renders. `bom.csv` is the schematic's own parts list and [`bom.md`](bom.md) what a buyer needs; the parts sheet 2 draws again are left out of it (they are sheet 1's), so R4, the reference, is the one part it adds. The netlist stays out of git: it is the check, not a source.

## The Kleidos KiCad style

The drawing follows the style every Kleidos schematic is drawn to, the boards' included, owned by the brand repository (its `kicad/README.md` holds the rules) and copied here as `kicad/`: the drawing sheet, whose title block carries the Kleidos mark, the license (`CERN-OHL-S-2.0`) and the source address from the project's text variables; the dark theme; and `style.json`, from which `gen_rig.py` reads every number the style fixes (the 1.27 mm grid, pins 2.54 mm long on a 2.54 mm pitch, the 2.54 mm stub, the text sizes, the one 6 mil line width). `kicad/KIT.json` records the brand commit the copy was taken at and each file's SHA-256, and `tests/hardware/test_rig_schematic.py` holds the copy to it: never edit a file there; change brand, run its `tools/sync_kicad.py --copy <this checkout>/hardware/kicad`, then generate and export again.

The parts are this project's own symbols (`rig.kicad_sym`, written by the generator; no KiCad library is read): boxes with their pins on the pitch and their names inside, the boards' resistor, capacitor, ground and power flag, the relay drawn released, the Relay 2 Click a box with its RL1 drawn open between its two terminals. Where the wire is the point (the current path, the Kelvin sense, the leads a relay opens) the drawing connects by wire; where a net only has to reach another block it connects by a global label of its name (`+3V3`, `SDA`, `SCL`, `ALERT`, `BYPASS`, `JD-VCC`, `K1_DRV` to `K4_DRV`); the nets of the current path are named on their wires (`VBUS`, `VIN+`, `VIN-`, `DUT+`, `5V`, and sheet 2's `REF+`); the grounds are ground symbols, one with the power flag ERC needs. A remark is text, never a label. The notes and the revision history are panels on the page.

The renders carry the organization's metadata profile (`scripts/media_meta.py`, held by `scripts/check_metadata.py`): the PDF's Info dictionary and XMP stream (title, author, description, keywords, license, the credit of the title block's mark and its trademark note, the title block's date, a file identifier derived from the sources), each SVG's SPDX comment, accessible name, title, description and Dublin Core block in place of KiCad's head, which held the export's clock. The PDF's record describes both sheets; each SVG has its own.

## What the drawing is for

A wiring guide in prose can drift from the bench without anyone noticing, and this one did: it described one pigtail where the rig has three, and put the shunt inside the INA228 block as if it were a property of the part rather than the component the whole measurement rests on. The schematic exists so that a disagreement between the drawing and the bench is visible rather than implicit.

Two things it records that a photograph would not:

- **R100 is the measurement.** The part is marked only `R100`; its value is measured against the VPG 100 Ω reference (sheet 2) and filed in the calibration registry (`calibration/`, `calibration/README.md`), which the Kleidos QA harness reads. The drawing names the part and how it is calibrated; it never states the value, so a new calibration changes the registry and nothing here.
- **Where the voltage is sensed**, which decides what the reported voltage means. It is not the same node as the cell, and the difference is the pigtails.

## Rev C: the relay module

Rev C (2026-10-30) added the HW-316 four-relay module the meter drives (`docs/rig-wiring.md` §8, `carrier-board.md` §8.1): K1, the lead, in series between the shunt's DUT end (`VIN-`) and pigtail 3; K2, the bypass, from the `VIN+` node to the same `DUT+` node; K3 and K4 spare; the module's inputs straight across to GPIO7, GPIO15, GPIO16 and GPIO17. Rev C1 (2026-11-19) redrew that wiring to the Kleidos KiCad style. The HW-316's silver contacts never closed the ~10 mV dry circuit across the shunt and K1 reliably (`docs/rig-wiring.md` §8), so rev D moves the bypass to a PhotoMOS relay.

## Rev D: the rig as it stands (2026-11-20)

Rev D draws the bench as verified on 2026-11-20 (`calibration/records/2026-11-20.md`):

- **The bypass is U4, a MikroE Relay 2 Click (MIKROE-1899)**: two IXYS LCA717 PhotoMOS relays, normally open, 0.15 Ω at most when on (0.083 Ω typical, at 1 A) and 3 ms at most to turn on or off at 5 mA of LED current (IXYS DS-LCA717-R04). The drawing shows **RL1**: its terminals from the `VIN+` node to K1's `NC` (across the shunt and K1 together), its control on the mikroBUS **PWM** position (16), which is relay 1 in the MikroE SDK (`relay2.c`) and `RL1` on the Click's silkscreen, driven by GPIO15 (T8), active high. The Click's `+3.3V` (7) and `GND` (8) go to the carrier's 3V3 and G. RL2, its control on the **AN** position (1), is drawn no-connect.
- **The bench uses RL2 instead**: the GPIO15 wire on the AN-position pin, the bypass on RL2's terminals. The two channels are the same circuit, so the netlist is the same; the sheet's notes say so, and the registry's configuration `mjkdz-r100-relay2click` names the channel fitted. The owner's second Click is not used (no soldering on the boards under test).
- **The HW-316 keeps only K1**, the lead, and keeps it mechanical: its `NC` is closed at rest, so the DUT stays powered when the meter is off, and no PhotoMOS on-resistance sits in series with the DUT. It is used as a single-relay module: only `IN1` is wired, to GPIO7; `IN2`, `IN3` and `IN4` are not wired at all (a floating input is off), so K2, K3 and K4 never energize and every one of their contacts is flagged no-connect. The carrier's GPIO16 (T9) and GPIO17 (T10) reach nothing: the firmware never drives them (the spares `k3` and `k4` read `unwired`, `firmware/src/relay.h` `kChannels`) and from 0.16.6 parks both as inputs with the internal pull-down (`kParkedPins`). The sheet's note still says the firmware drives them, from before 0.13.0; it changes with the next export of the drawing.
- **Pigtail 2 lands on the breakout's `VBUS` screw**, and JP2, a short wire, joins `VBUS` to `VIN+`, so `VBUS` reads the cell's side (`CALibration:VBUSside SOURce`).
- **The null path** (`ROUTe:NULLpath ON`, firmware image `meter` from 0.8.0, `meter_relay2` before) closes RL1, then opens K1; `OFF` recloses K1, then opens RL1. With every relay released the drawing is still rev B: K1's `NC` keeps the DUT on the shunt and RL1 leaves nothing across it.

**Sheet 2, the calibration** (`rig-calibration.kicad_sch`): sheet 1's parts, rewired as `calibration/README.md` "The shunt, two points" has it, and numbered +100 there (U101 is U1, R200 is R100, K101 is K1, U104 is U4), because KiCad's annotation wants every reference once in a project; they stay out of the bill of materials and the board. R4, the Vishay VPG Y0785100R000T9L (100 Ω ±0.01 %), takes the DUT's place, four-wire: the cell's + on `VIN+` with JP2 off, the current from pigtail 3 (K1's `NC`) onto R4's top lead, the `VBUS` sense wire on a separate point of the same lead, R4's bottom lead to the INA228's `GND` with its own wire. Its notes carry what the netlist cannot: every joint of the reference path screwed (twisted and clipped joints gave about 0.17 Ω, a 7 mV error), the `SLEEP` profile (4120 µs, 16 averages), the offset nulls taken with K1 open before and after the points and again every session (the null drifts with thermal EMF), and where the procedure and the results live.

## Where the parts sit, which the drawing does not show

The schematic draws connections, not placement, and one placement matters: the INA228 breakout (U1) sits **about 5 cm from the ESP32-S3 carrier (U2)**, beside it and never stacked under the module's PCB antenna. Stacked, as it was until 2026-11-21, it picked up the meter's own radio on a radio image (an impulse of about −25 µV on every SoftAP beacon, the floor's sd doubled, the null moved by −2.97 µV under traffic); 5 cm away none of that could be seen. Moving it changed no wire, so the drawing and its revision stay as they are. The measurements and the procedure are in `docs/rig-wiring.md` §3, "Where the breakout sits".

## How the drawing is checked

By export, not by looking: the first version of this drawing looked right and connected nothing. `export_rig.py` runs ERC on both sheets (the style allows no message, error or warning, and the project ignores no rule) and compares the drawn netlist with `EXPECTED` in the same script, the rig as wired:

- **50 nets and 118 pins.** Sheet 2's named nets carry its name (`/calibration/VIN+`); the global nets (`GND`, `+3V3`, `SDA`, `SCL`, `ALERT`, `BYPASS`, `JD-VCC`, `K1_DRV`) are one net across both sheets, as they are on the bench, so sheet 2's U101, U104 and K101 sit on them beside U1, U4 and K1. The single-pin nets are exactly the pins flagged no-connect: K1 `NO`, U4's AN-position pin and RL2's two terminals (and the same three of U104 and K101's `NO` on sheet 2), every contact of K2, K3 and K4, U3 `IN2`, `IN3` and `IN4`, and U2's GPIO16 (T9) and GPIO17 (T10).
- **The measurement path:** `VBUS` = RW2.2, JP2.1, U1 `VBUS`; `VIN+` = JP2.2, R100.1, U1 `IN+`, U4 RL1 A; `VIN-` = R100.2, U1 `IN-`, K1.COM; `DUT+` = K1.NC, RW3.1, U4 RL1 B.
- **The calibration:** `/calibration/VIN+` = RW102.2, R200.1, U101 `IN+`, U104 RL1 A; `/calibration/VIN-` = R200.2, U101 `IN-`, K101.COM; `/calibration/DUT+` = K101.NC, RW103.1, U104 RL1 B; `/calibration/REF+` = RW103.2, R4.1, U101 `VBUS`; R4.2 on `GND`.
- **The control:** `BYPASS` = U2 GPIO15 and the Click's PWM position (16); GPIO7 to U3 `IN1`, the one input wired; `5V` = U2 `5V (T21)`, U3 `VCC`, JP1.1; `JD-VCC` = JP1.2, U3 `JD-VCC`, the `A1` of every relay; `K<n>_DRV` = U3 `DRV<n>` and K*n*'s `A2`.

`tests/hardware/test_rig_schematic.py` runs the same check where kicad-cli is installed, and holds the generator, the kit, the title block, the two sheets and the renders' metadata everywhere.
