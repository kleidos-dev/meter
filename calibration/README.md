<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Calibration

**The registry in this directory is the calibration. What the instrument keeps in its flash is a working copy of it.** A full flash erase (`esptool erase_flash`) loses that copy, so nothing that must survive can live there alone: the measured shunt of each range, its uncertainty, the date and the history are kept here, versioned with the repository, and the Kleidos QA harness reads them from here at a pinned commit, never from the instrument (owner, 2026-11-20). The instrument answers two questions: which instrument it is (`*IDN?` field 3, its serial) and which working copy it holds (`CALibration:DATA?`).

What the instrument still keeps, in its flash: the **shunt resistance** of each range (`SENSe:CURRent:SHUNt`, `SENSe:CURRent:SHUNt:RANGe<n>`), the **calibration tag and date** (`CALibration:TAG`, `CALibration:DATE`), **where `VBUS` is sensed** (`CALibration:VBUSside`), and the **offset null** of each range (`CALibration:ZERO`). The first three are the working copy, written by `kleidos-meter calibration restore` from the registry; the null is session state and is never restored. From FW-16 (0.17.0) the copy is a record of the calibration record log in the `calib` partition at 0x9000 (`docs/architecture.md` §3.7); before, it was an NVS blob of the same fields. The image that brought the log did not read NVS, so the first boot of it, or of any later image over a flash that never held the log, reports `q=uncal` and `verify` answers `working_copy_lost` until `restore`. A stored shunt outside 0.05..0.2 Ω is refused when it is read, never clamped: the record before it stands, else the defaults. Every capture header repeats the shunt it converted with and the quality token `q=` (`uncal`, `stale` when the die has moved more than ±5 °C since the null, `cal`, `invalid`).

## The registry

```text
calibration/
  schemas/                       configuration, instrument and index formats (JSON Schema)
  configurations/<id>.json       a bill of the parts that set the measurement
  instruments/<serial>.json      one instrument: its configuration over time and its events
  records/                       the Markdown record and the raw SCPI log of each event
  index.json                     generated; the one file other tools read
scripts/check_calibration_registry.py     checks all of it; --write regenerates index.json
```

**Identity.** An instrument is named by its serial, `KM-` and ten hex digits: the first five bytes of `SHA-256("kleidos-meter-serial-v1" ‖ MAC)` over the ESP32-S3's factory eFuse MAC (`firmware/src/identity.h`, firmware 0.4.0 and later). It survives a flash erase, it is a pseudonym and not the MAC (which never enters a tracked file), and a replaced controller is a new serial that starts with a new calibration. The serial names the assembly; the parts that set the measurement are named by the instrument's configuration. A serial is a digest of the MAC that a search can undo, so no real one is written in this repository (owner, 2026-11-27): the bench instrument is filed, recorded, pictured and quoted as `KM-0000000000`, a placeholder of the serial's shape, and `tests/ci/test_instrument_serials.py` fails any other serial in a tracked file but the tests' own vectors. The records that name it were edited for that alone, and their hashes in the instrument file follow.

**Configurations.** A configuration (`configurations/<id>.json`) is the bill of what sets the measurement: the ADC and the INA228 ids it reports, the breakout, the shunt (its nominal, its stated tolerance or `null`, the window past which a reading is the wiring), the Kelvin wiring and the schematic revision, the relays and their pins, the controller, the firmware image, and what is expected of a calibration (the ADC gain error, the largest acceptable range split, the recalibration interval). The instrument cannot report its configuration (its flash is what an erase loses; the image knows only what it drives), so the instrument file says which configuration an instrument is in, and what the instrument does report (`image=`, `bypass_part=`, `mfg=`, `die=`) is checked against it. An image that was renamed keeps its rig: `firmware.image_aliases` lists the other names a configuration's image has had, each from its own version, and `firmware.max_version` ends a name's tenure. The Relay 2 Click rig's image was `meter_relay2` from 0.3.0 to 0.7.0 and is `meter` from 0.8.0, the instrument's one image, so `mjkdz-r100-relay2click` accepts both, and the events filed on 2026-11-20 under `meter_relay2` stay valid and unedited; `mjkdz-r100-hw316`'s `meter` ends at 0.7.0, because from 0.8.0 that name drives the Click. Two configurations with the same `sense.breakout.id` and `sense.shunt.id` are the same **sense core**: a calibration survives a change of relay, and instruments built from the same parts are compared with each other.

**Instruments and events.** `instruments/<serial>.json` is an append-only list of events: `nominal_declared`, `as_found`, `hardware_change` (with `voids_calibration` true when anything inside the Kelvin sense moved), and `calibration` with a verdict, `accepted`, `refused` or `superseded` (a refused session is filed, because it is knowledge, and is never selected). An accepted calibration carries per range the points, their mean and scatter, the nulls it was taken on, the value applied (`applied_nohm`) and its standard uncertainty (`u_ppm`); the full budget, u_c, k and U; the conditions; the working copy it wrote (`device`); and the hashes of its record and raw log, which hold those files to what was filed. Resistances are integers in nano-ohms and relative terms integers in ppm, as on the wire. A correction is a new event, never an edit.

**Which calibration holds on a date**: the newest accepted one taken on or before it, after the last voiding hardware change, and not older than its configuration's `recalibration_days`. Events of one day count in the order of their ids (`YYYY-MM-DD-NN`): a voiding change filed after a calibration ends it that day, and a change filed after it is not the configuration it was taken in. The index has the windows resolved (`valid_from`, `valid_until`).

**The check** (`python3 scripts/check_calibration_registry.py`, run by pre-commit, `scripts/run_tests.sh` and CI): every file against its schema; each accepted calibration's arithmetic recomputed (the means and their scatter, the split, every budget term from its limit and distribution, u_c as their root sum of squares, U = k × u_c, the applied value between the two range means); each range inside its configuration's window and the split under its limit; the hashes; two successive calibrations of one instrument by their normalized error `E = |R_2 − R_1| / √(U_1² + U_2²)` (past 1 the newer one needs an `acknowledged` note, and the index reports the drift in ppm a year); instruments of one sense core by membership (from three on, one further than three scaled median absolute deviations from the group's median is flagged, the scale never below the largest expanded uncertainty among them, and the group's mean offset from nominal is reported); and that `index.json` is what the files give.

**Someone with another INA228 board** adds a configuration describing their breakout, shunt and relays, calibrates with `kleidos-meter calibrate --configuration <their id>` and their own reference, runs the check, and sends the files in one pull request. If their breakout and shunt ids match an existing sense core, the check compares their instrument with ours from then on. The firmware and the QA harness still assume a 0.100 Ω shunt (`calmath.h` `kNominalShuntNanoOhm` and its ±2 % window, `SENSe:CURRent:SHUNt` 0.05..0.2 Ω, the harness's `BENCH_SHUNT_MOHM`), and a different bypass part or pin map is a new firmware image: both are named in the configuration (`nominal_nohm`, `firmware.image`), so that work has a place to read from.

## The working copy

```sh
kleidos-meter calibration verify  --port "$KLEIDOS_METER_PORT"        # the instrument against the registry
kleidos-meter calibration restore --port "$KLEIDOS_METER_PORT"        # the registry's working copy, written back
kleidos-meter calibration verify  --port "$KLEIDOS_METER_PORT" --rev <commit>   # at a pinned commit
```

`verify` reads `*IDN?`, `SYSTem:INFormation?` and `CALibration:DATA?` and answers one status: `applied` (the working copy is the registry's), `working_copy_lost` (the instrument at its defaults, `shunt_measured=0`: after a full erase, or the first boot of the image that brought the flash log), `working_copy_differs` (an unfiled calibration, a typed value, or a field the instrument's firmware did not have yet, such as `cal_date` before 0.4.0), `expired`, `configuration_mismatch` (the image, the bypass part or the INA228 is not the configuration the registry files this instrument under), or `unregistered`. It exits 0 only on `applied`, and warns from 30 days before a calibration ends. `restore` writes `SENSe:CURRent:SHUNt` (and range 1's own value when it differs), `CALibration:TAG`, `CALibration:DATE` and `CALibration:VBUSside`, reads them back and verifies; it never writes a null, and refuses an instrument the registry does not know or does not recognize. An app flash keeps the working copy (it writes the bootloader, the partition table and the image, never the `calib` partition; the 2026-11-19 record shows it for NVS), so the copy survives a firmware update; a full erase does not, and `restore` brings it back. The one exception was the flash that brought the record log (FW-16), which left NVS behind and started from no record.

### What a verdict asks of you

`kleidos-meter calibration verify` exits 0 on `applied` and 3 on every other verdict, and its lines below the first say what differs (`-`) and what to see to (`!`, from 30 days before the calibration ends). The Kleidos QA harness judges the same instrument against the registry at the commit it pins (`qa/meter-registry.lock.json` in the Kleidos firmware repository), with two verdicts of its own, and stops a row before anything is measured on any verdict but `applied` and `unregistered`, which the harness gives only to an instrument that reports no serial. The two vocabularies part on one case: a serial the registry does not know is `unregistered` here and `uncalibrated` to the harness, which stops the row.

| Verdict | Means | Do |
|---|---|---|
| `applied` | the instrument holds the registry's working copy, and the calibration holds today | measure |
| `working_copy_lost` | the instrument is at its defaults (`shunt_measured=0`): a full flash erase, or the first flash of 0.17.0 over an earlier image | `kleidos-meter calibration restore` |
| `working_copy_differs` | a shunt, a tag, a date or the `VBUS` side is not the registry's: a value typed by hand, a session not filed | `kleidos-meter calibration restore`. A calibration that was taken and not filed is not a working copy: run `kleidos-meter calibrate` and file it |
| `expired` | the newest calibration ended (`recalibration_days` after it was taken) | calibrate again ([below](#the-shunt-two-points)); `restore` refuses it |
| `configuration_mismatch` | the image, the bypass part or the INA228 is not the configuration the registry files the instrument under | flash the image the configuration names, or, when the rig changed, file the `hardware_change` and calibrate again; `restore` refuses it |
| `unregistered` | no serial (firmware before 0.4.0), or a serial the registry does not know. The Kleidos harness measures on the first, and nothing it measures is quotable; the second is its `uncalibrated`, and stops the row | for no serial, flash a current image; for an unknown serial, a new instrument's first calibration names its configuration (`kleidos-meter calibrate --configuration <id>`) |
| `uncalibrated` (the Kleidos harness) | a serial the registry does not know, or holds no calibration for on the day: `kleidos-meter` says `unregistered` for the first | calibrate it |
| `registry_unreadable` (the Kleidos harness) | no meter checkout at `KLEIDOS_METER_REPO` (or a sibling `../meter`), or none that holds the pinned commit and its index | clone or fetch this repository there. `KLEIDOS_CALIBRATION_UNQUOTABLE=1` runs a row anyway, and none of its figures is quotable |

On the simulator, whose serial no registry knows:

```console
$ kleidos-meter calibration verify --sim
KM-EE529C1B03: unregistered
  - KM-EE529C1B03 has no calibration in the registry
```

## The null, before every measurement session

A null is taken with **no current crossing the shunt** and on the range it will serve; each range keeps its own. The null drifts within a session (2026-11-20: 1.9 → 4.6 µV over about two hours, thermal EMF in the sense loop), so a null belongs to its session: take one before every session, and the QA harness brackets every capture with its own.

- **Through the relays** (no hands): `ROUTe:NULLpath ON` (expect `check=ok` with the DUT drawing 2 mA or more), `CALibration:ZERO 60`, wait the 60 s, `ABORt`, `CALibration:ZERO:RESult?` (`stored=1`), `ROUTe:NULLpath OFF`. Validated against the hand null on 2026-11-20 (`records/2026-11-20.md` §2).
- **Through the lead relay alone**, while the bypass cannot close: cut the DUT's other supplies, `ROUTe:LEAD OPEN,FORCe` (`path=none`), `CALibration:ZERO 60`, wait, `ABORt`, `ROUTe:LEAD CLOSe`. The Kleidos QA harness does this itself (`power_campaign.py td08 --nulls lead`).
- **By hand**: open the DUT's battery lead, `CALibration:ZERO 60`, wait, `ABORt`, refit the lead.

`ABORt` ends the window early: wait out its seconds first. A null closed with K1 commanded open that reads beyond 50 µV is refused (error 102): the lead did not open, and storing it would subtract the DUT's own current from every later reading.

## The shunt, two points

The `R100` part is marked only with its value; until a calibration is filed, every figure carries its 1 % marking as the gain term. The reference is the **VPG Y0785100R000T9L, 100 Ω ±0.01 %** foil resistor (S102C: TCR ±2 ppm/°C, load life ±0.005 %, `hardware/bom.md`). It replaces the DUT, so one current crosses both: the INA228 reads that current's drop across the shunt on its shunt channel and, with the `VBUS` lead on the reference's top lead, the drop across the reference on its bus channel. The same current makes both, so

    R_shunt = V_shunt × R_ref / V_ref

and neither the cell's voltage nor its drift enters (`firmware/src/calmath.h`). The **two points are the two ADC ranges** at that one current (about 41 mA from a 4.1 V cell, 4.1 mV across the shunt, inside both ranges): each range has its own gain error, so two figures that agree are one resistance seen twice rather than one range's error seen once.

**Run it guided:** `kleidos-meter calibrate --port "$KLEIDOS_METER_PORT"` (`host/kleidos_meter/calibrate.py`). It reads the serial, finds the instrument and its configuration in the registry (an instrument's first calibration names it, `--configuration <id>`), refuses when the image, the bypass part, the INA228 or the firmware contradict the configuration, and takes the acceptance window from the configuration. It then walks through the steps below, judges the gates, writes the working copy only if every gate holds and you confirm, and files the event: the instrument file, `records/<serial>/<event>.md` and `.log` (the session's SCPI exchange), and the index. A refused session writes nothing to the instrument and is filed as `refused`. It never commits: commit it here, have it merged into `main`, then move the QA harness's pin ([below](#after-it-is-filed-the-qa-harness-pins-it)). `kleidos-meter calibrate --sim --yes --registry <a copy>` runs the flow against the simulated instrument.

The bench, step by step (the lessons of 2026-11-20 are in it):

1. **Everything off.** Unplug pigtail 3 from the DUT's battery socket and cut the DUT's hub port.
2. **Fit the reference** between pigtail 3 (the DUT + node, after K1's `NC`) and the INA228's `GND` screw, its ground lead **in that same screw**: the 41 mA return then shares no wire with the sense ground. Its top lead goes to pigtail 3.
3. **Move the cell and `VBUS`.** On this rig the cell's + feeds the **`VBUS` screw**, and a `VBUS`–`VIN+` jumper carries it to `VIN+`. For the calibration, move the cell's + to **`VIN+`** and the `VBUS` wire to the **reference's top lead itself**: on the resistor's own lead, not on `VIN−`, so that K1's contact and the pigtail (about 0.15 Ω) are not in the reading, and so that `VBUS` senses the reference and nothing else.
4. **Screw every joint.** Both reference joints go into screw terminals: **never twisted, never clipped**. On 2026-11-20 twisted and clipped joints read 100.04 mΩ, then drifted to 100.21 mΩ within 40 minutes, a contact of about 0.17 Ω in the reference's `VBUS` path; screwed, 20 points over five minutes showed no trend.
5. **The opening nulls, hands-free:** `SENSe:PROFile SLEep`, then on each range (`SENSe:CURRent:RANGe:INDex 0`, then `1`) `ROUTe:LEAD OPEN,FORCe`, `CALibration:ZERO 60`, wait, `ABORt`, `CALibration:ZERO:RESult?` (`stored=1`), `ROUTe:LEAD CLOSe`. With K1 open no current crosses the reference or the shunt.
6. **The points**, on the `SLEEP` profile (4120 µs conversions, 16 averages): `CALibration:SHUNt:REFerence 100.0` (the value on the part's certificate if it has one), `SAMPle:COUNt 600`, then four points per block, `CALibration:SHUNt` and `CALibration:SHUNt:RESult?` → `range= r_nohm= r_uohm= dev_ppm= i_na= vsh_nv= vref_uv= ref_uohm= n= die_mc= check=`, in blocks in the order **range 0, range 1, range 1, range 0**. The mirrored order takes a steady drift out of the range split, which taking one range after the other does not (`records/2026-11-20.md` §5).
7. **The closing nulls**, as in step 5.
8. **Judge** (the gates below), then, if they hold: `SENSe:CURRent:SHUNt <ohms>` with the mean of the two range means rounded to the micro-ohm, `CALibration:TAG "R100_<yyyymmdd>"`, `CALibration:DATE <yyyy>,<m>,<d>`.
9. **Restore the bench:** the `VBUS` wire back to its screw, the cell's + back to the `VBUS` screw, the reference out, pigtail 3 back into the DUT; `CALibration:VBUSside SOURce`.

### After it is filed: the QA harness pins it

A new calibration reaches the measurements only when the Kleidos QA harness pins the registry commit that holds it. The harness reads the index at that commit, never this checkout's working tree and never the instrument.

1. Commit the event's files here (the instrument file, the record and its log, `index.json`), with `python3 scripts/check_calibration_registry.py` passing, and have the commit merged into `main` and pushed: the pin takes `origin/main` and nothing else.
2. In the Kleidos firmware repository, from `qa/` with its virtual environment active, move the pin. It fetches the meter checkout it reads (`KLEIDOS_METER_REPO`, or the sibling `../meter`), writes the Kleidos firmware repository's `qa/meter-registry.lock.json` with the new commit and the index's SHA-256, and prints each calibration that came, went or changed: that list is the review of the new calibration. On 2026-11-24, with nothing new filed:

   ```console
   $ python3 -m qa_lib.power meter-lock
   meter-registry.lock.json: kleidos-dev/meter at 094d662ad3f3b95478d302b39ae1a6724198cdb5
     no calibration changed
   ```

3. Commit the moved lock file there (the Kleidos firmware repository's `qa/meter-registry.lock.json`), with the lines it printed in the commit's body, by that repository's rules.
4. `kleidos-meter calibration restore` (or the harness's own preflight, which names the command) writes the new working copy to the instrument, and `calibration verify` answers `applied`.

### Acceptance gates

| Gate | Value | Why |
|---|---|---|
| The instrument's own verdict on each point | `check=ok` | `no_samples`, `reversed`, `reference_low` (VBUS not on the reference: under 1 V), `signal_low` (under 1 mV across the shunt) or `out_of_window` are each a wiring fault, named. |
| Each point from nominal | inside the configuration's window: the shunt's stated tolerance, or ±2 % when it states none | anything further is the wiring, not the part. |
| First and last block of each range | within 200 ppm | the bench did not move. A joint drifting as the twisted ones did (about 40 ppm a minute) is 600 ppm apart by then; the old two-take repeat inside two minutes let it through. |
| Each range's closing null against its opening one | within 0.5 µV | the null still stands; the difference is the budget's null term. |
| The two ranges' means | within the configuration's `range_split_max_ppm` (1000 ppm on this rig) | the INA228's shunt and bus gain errors are ±0.05 % each at most (TI SBOSA20 §6.5), so one range's ratio can be 0.1 % from the truth; more than that between ranges is not one shunt. |
| Die temperature at each point against its range's opening null | within ±5 °C | the null's own staleness window (`calibration.h` `kStaleDieMilliCelsius`). |

### The uncertainty budget

Each term is a limit and a distribution; its standard uncertainty is the limit over √3 for a rectangular limit and the figure itself for a normal or measured one (GUM 4.3.7), and u_c is their root sum of squares, U = 2 u_c:

| Term | Limit | Distribution |
|---|---|---|
| reference tolerance | the part's, ±100 ppm | rectangular |
| reference TCR | its TCR over a ±20 °C bound on its temperature (the bench plus 0.16 W of self-heating) | rectangular |
| INA228 bus gain | the configuration's `adc_gain_error_ppm`, ±500 ppm | rectangular |
| range split | half the split between the range means | normal |
| null drift | the larger null closure over the signal across the shunt | rectangular |
| repeatability | the scatter between blocks of one range, with fresh nulls around them | measured |

One value is applied to both ranges, with half the split as a term of its own, until a calibration shows the split is a stable property of the instrument; then each range may apply its own mean (`SENSe:CURRent:SHUNt:RANGe1`), and the registry and the QA harness need no change. The calibration's u, not the marking's 1 %, becomes the gain term of every figure the QA harness files, entered as a standard uncertainty.

## VBUS

After the continuity check of `docs/rig-wiring.md` §1 shows `VBUS` tied to the source side of the shunt: `CALibration:VBUSside SOURce`. Otherwise leave it `UNKNown`, and the voltage and the power are not the DUT's.

## Records

`records/` holds the record of each event: what `CALibration:DATA?` said before and after, how each number was taken, and the raw SCPI exchange beside it. A record is never edited after it is filed (its hash is in the instrument file), but for the placeholder above; a correction is a new record and a new event. The records before the registry (`2026-11-19.md`, `2026-11-20.md`) are dated files; the guided calibration files under `records/<serial>/<event>.md`. Two records characterize the instrument rather than calibrate it, and are filed under no event: `radio-ab-2026-11-21.md`, the NW-00 radio A/B, which failed and made the meter serial-only, and `radio-tx-sweep-2026-11-21.md`, its addendum, the same worst case at five transmit powers.
