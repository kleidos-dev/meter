<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# The bench meter's documentation

The bench meter measures what each Kleidos board draws: an ESP32-S3 reading a TI INA228 across a 0.1 Ω shunt in the board's battery lead, with a relay pair that takes the offset null without hands. It speaks SCPI over its serial line and nothing else, and a host package on the PC (`kleidos-meter`) gives it a command line, a Python interface and a panel in the browser. These pages are its documentation, the usage and the firmware's reference together, in English. Each is a Markdown file of the repository, and reads the same there.

## Reading order

1. **Start**: what the instrument is and the quick start on the simulated instrument, in [the repository's README](../README.md#quick-start); then a measuring session in order, in [the usage guide](usage.md).
2. **The panel**: [the panel](panel.md), `kleidos-meter serve`, every view with its image.
3. **The command line**: [every command of `kleidos-meter`](cli.md), what it takes and exits with.
4. **The HTTP API**: [the panel's routes](http-api.md), for a script that drives the panel.
5. **SCPI**: [the protocol](scpi.md), every command, report key, error number and stream record.
6. **The rig and its wiring**: [the schematic](../hardware/README.md), normative for the wiring, then [the wiring guide](rig-wiring.md), [the carrier board](carrier-board.md), [the Relay 2 Click](relay2-click-wiring.md) and [the parts](../hardware/bom.md).
7. **Calibration**: [what the calibration is](../calibration/README.md), the null and the shunt's two points.
8. **Troubleshooting**: [what a wrong answer, a refusal or an error number usually means](troubleshooting.md).
9. **Testing**: [every test, the report and its gates](testing.md), and the bench suite.
10. **The firmware's reference**: [the firmware](firmware.dox), its boot, its two tasks and every symbol by topic, with [its design](architecture.md).
11. **The Python host package**: [its public interface](python-api.md), class by class, and [how the package is made](../host/README.md).

The rest, by what you are doing:

## Use the meter

| Document | What it answers |
|---|---|
| [usage.md](usage.md) | How a measuring session goes: the port, the line, the calibration, the null, a measurement, a capture, its export, the relays, the end state |
| [panel.md](panel.md) | The panel (`kleidos-meter serve`): starting it, every view with its image, the chart (the mean line, the band, smoothing, the readout, the view's exports), the pin view (what each mark means, how a change is applied, proved and undone from the console), the INA228's noise reduction, what each sequence moves, exports, the passthrough, and what happens when something dies |
| [cli.md](cli.md) | Every command of `kleidos-meter`: what it takes, what it exits with, an example. Generated |
| [python-api.md](python-api.md) | The host package from Python: every public class, method and function. Generated |
| [http-api.md](http-api.md) | The panel's HTTP API, route by route: what each takes, answers and sends. Generated |
| [troubleshooting.md](troubleshooting.md) | What a wrong answer, a refusal or an error number usually means, the panel's chart, its noise reduction and a reverted pin map included: the query that checks it, the fix, and where it is defined |
| [scpi.md](scpi.md) | What each command, report key, error number and stream record is, and a quick reference of the ones a session sends. The protocol, normative |
| [../calibration/README.md](../calibration/README.md) | What the calibration is, what each verdict asks of you, the normative null, the two-point shunt calibration, its uncertainty, and how the QA harness's pin follows it |
| [../host/README.md](../host/README.md) | The host package and how it is made: the Python client, the panel's process, its recordings, the simulated instrument |
| [../CHANGELOG.md](../CHANGELOG.md) | What each version changed on the wire, and what the Kleidos QA harness must know of it |

The panel's images, every view dark and light at a desktop's width and dark at a phone's, are in [images/panel/](images/panel/), taken from `kleidos-meter serve --sim` by `scripts/take_panel_shots.py`; `tests/firmware/test_panel_images.py` names the one to take again when its view changes. Under [images/panel/bench/](images/panel/bench/), the panel over the cable to the meter, each image with its date and the image's `elf=` ([panel.md](panel.md#on-the-bench)), filed by `scripts/file_bench_shots.py`.

## Build the rig

| Document | What it answers |
|---|---|
| [rig-wiring.md](rig-wiring.md) | What to measure before wiring, every wire as a row, the checks before power, the first test |
| [carrier-board.md](carrier-board.md) | Which terminal of the ESP32-S3 carrier is which pin, which to use, and which USB port is which |
| [relay2-click-wiring.md](relay2-click-wiring.md) | How the Relay 2 Click is fitted as the shunt bypass |
| [../hardware/README.md](../hardware/README.md) | The schematic, normative for the wiring, and how it is generated |
| [../hardware/bom.md](../hardware/bom.md) | The parts, and which are on hand |
| [../README.md](../README.md) | What the instrument is, and how it is built and flashed |

## Develop the firmware

| Document | What it answers |
|---|---|
| [architecture.md](architecture.md) | The design as it is: the sensor, what is measured and how, the tasks, the hot path, the relays |
| [ina228-reference.md](ina228-reference.md) | The INA228's registers and scaling, as this instrument uses them |
| [testing.md](testing.md) | Every test, the report and its gates, and the bench suite |
| [misra.md](misra.md) | What the firmware holds of MISRA C++:2023, where it departs, and what allocates before `app_main()` |
| [relay-timing.md](relay-timing.md) | How long the relays' contacts take, as measured, against the settle waits |
| [../AGENTS.md](../AGENTS.md) | The rules of this repository, for a person or an agent changing it |

This site (`mkdir -p build && doxygen Doxyfile`, into `build/doxygen/html`) is these pages and the firmware's reference, which documents every symbol of `firmware/src`; the reference opens on [its front page](firmware.dox) (`docs/firmware.dox`) and lists [its topics](groups.dox) (`docs/groups.dox`). As it is published, in the Kleidos design, it is built by `python3 scripts/build_reference.py --out <dir>` ([testing.md](testing.md#the-documentation-site)).

## History

Records of their date, kept as they were written: nothing here describes the instrument as it is now.

| Document | What it records |
|---|---|
| [firmware-design.md](firmware-design.md) | The design of firmware 0.1.0, frozen on 2026-11-22 |
| [../calibration/records/2026-11-19.md](../calibration/records/2026-11-19.md), [2026-11-20.md](../calibration/records/2026-11-20.md) | The calibrations before the registry |
| [../calibration/records/radio-ab-2026-11-21.md](../calibration/records/radio-ab-2026-11-21.md), [radio-tx-sweep-2026-11-21.md](../calibration/records/radio-tx-sweep-2026-11-21.md) | The radio test that failed and made the meter serial-only, and its addendum |
