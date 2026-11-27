<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# When something is wrong

What a session at the bench meets most often, as a table: what you see, what usually causes it, the query that tells you, the fix, and where the behavior is defined. It is ordered as a session is: the port and the line first, then the refusals, the quality of a number, the relays and the calibration. Every error number on this page is a row of [scpi.md, "The error queue"](scpi.md#the-error-queue), and a test holds the two together (`tests/firmware/test_troubleshooting.py`). A session that goes as it should is [the usage guide](usage.md); the commands a session sends are [scpi.md's quick reference](scpi.md#quick-reference).

The examples below the table were run on the bench on 2026-11-24, instrument `KM-0000000000` on firmware 0.17.0; the port is written `<port>`, because its name carries the adapter's serial number.

## Contents

- [The table](#the-table)
- [What some of them look like](#what-some-of-them-look-like)

## The table

| Symptom | Likely cause | Check | Fix | Defined in |
|---|---|---|---|---|
| Every command prints `# the instrument rebooted`, and the uptime starts again | A tool that set DTR and RTS before it opened the port: pyserial applies them in an order that passes through the carrier's reset | `SYST:HEAL?`: `uptime_us=` a few hundred thousand (the reset reason reads `poweron` either way) | Open the port as the OS leaves it, then release RTS, then DTR; `kleidos-meter` and the Kleidos harness do. Anything the instrument was running is gone, its settings are the defaults | [scpi.md, "The line"](scpi.md#the-line), [carrier-board.md §7](carrier-board.md#7-the-two-usb-c-ports-which-to-flash-from-which-to-talk-to) |
| `# 'X': no response within 8 s`, exit status 1 | A message of commands alone: a command answers nothing | the same message with `;:SYST:ERR:ALL?` at its end | End every message with `;:SYST:ERR:ALL?` | [scpi.md, "Responses"](scpi.md#responses) |
| The same, on a `MEASure` or a `CAL:SHUN`, and the answer arrives on the next message | The acquisition takes longer than the wait: `SAMP:COUN?` samples at the profile's period (100 on `SLEEP` is 6.75 s) | `SAMP:COUN?`, `SENS:PROF:REP?` `period_us=` | `--timeout` above the acquisition, or fewer samples; send `SYST:ERR:ALL?` alone to read the late answer away | [scpi.md, "CONFigure, MEASure, READ, FETCh"](scpi.md#configure-measure-read-fetch) |
| `# the instrument is not answering`, exit status 1 | The meter is booting, is unpowered, or the port is not the meter's | the CH343 is plugged and is the one port at `1a86:55d3` | Find the port again; wait for the boot (its PSRAM test comes first) | [usage.md, "Find the port"](usage.md#find-the-port) |
| `kleidos-meter: error: name the port`, exit status 2 | `KLEIDOS_METER_PORT` is not set | `echo "${KLEIDOS_METER_PORT:+set}"` | Set it as [usage.md](usage.md#find-the-port) does, or pass `--port`; never paste its value | [usage.md, "Find the port"](usage.md#find-the-port) |
| A Python traceback ending in `No such file or directory`, `Could not configure port` or `*ESR? answered` | A port that does not exist, a device that is not a serial port, or a serial port that is not the meter (a board that echoes what it is sent) | the vendor and product of the port: `1a86:55d3` | Name the CH343 port; never a Kleidos board's | [carrier-board.md §7](carrier-board.md#7-the-two-usb-c-ports-which-to-flash-from-which-to-talk-to) |
| `*** meter upload refused: ...` | The upload guard: no port named, a port that does not exist, a port that is not the CH343, or a Kleidos board's port (its USB serial is in the fleet's inventory) | the message names which | Name the meter's CH343 port, plugged in; the guard is never bypassed | [README, "Flash"](../README.md#flash), `firmware/scripts/meter_upload_guard.py` |
| `<port> is held by another opener`, or `the port is held by kleidos-meter serve (pid ...)`, exit status 1 | `kleidos-meter serve`, a QA run or another tool has the port: every opener takes it exclusively | the note names a `serve` and its panel; otherwise `lsof` on the port | Stop `serve` (Ctrl-C), or reach the meter through its passthrough (`--port socket://127.0.0.1:5025` when it runs with `--scpi-passthrough`). The Kleidos harness cannot use the passthrough: stop `serve` first | [panel.md, "Start it"](panel.md#start-it), [panel.md, "The SCPI passthrough"](panel.md#the-scpi-passthrough) |
| `-221,"Settings conflict;capture running (ABORt first)"` | A capture is open: a setting, a measurement or a null is refused under it | `SYST:STAT?`: `running=1`, `sink=` | `ABOR`, then send it again | [scpi.md, "Command reference"](scpi.md#command-reference), flag R |
| `-221` `capture ended, not closed` or `112` `null window ended, not closed` | A ring filled, a capture limit or a null window's seconds ran out: the window waits for `ABORt` | `SYST:STAT?` `running=0` with `sink=` not `idle`; `CAL:ZERO:RES?` `running=1` | `ABOR`, and read its result | [scpi.md, "Command reference"](scpi.md#command-reference), flag C |
| `-221,"Settings conflict;BURST needs a faster bus ..."` on `INIT` | `BURST` at the 400 kHz default | `SYST:COMM:I2C:FREQ?` | `SYST:COMM:I2C:FREQ 1000000` before, `*RST` or 400000 after | [scpi.md, "SENSe, SAMPle, FORMat, TRIGger"](scpi.md#sense-sample-format-trigger) |
| `-221` `a ring profile takes no channels` | `SENS:FUNC:ON` left from a streamed capture, then `TRACE` or `BURST` | `SENS:FUNC:ON?` | `SENS:FUNC:ON:DEF` | [scpi.md, "Captures and the stream"](scpi.md#captures-and-the-stream) |
| `-363,"Input buffer overrun;program message longer than 255 bytes"` | A message over 255 bytes; the whole message is discarded | its length | Split it into several messages | [scpi.md, "The line"](scpi.md#the-line) |
| `-363` `receive overrun: input lost while the console was busy` | More than the receive ring holds was sent while a dump or an acquisition held the console | `SYST:ERR:ALL?` | Wait for each answer before the next message, as the host tools do | [scpi.md, "The line"](scpi.md#the-line) |
| `-230,"Data corrupt or stale;..."` | `FETC` under a running capture, before any capture, or a function the profile does not convert (voltage on `TRACE`) | `SYST:STAT?` `running=`, `profile=` | `ABOR` first; `MEAS` configures the profile, `READ` and `FETC` do not | [scpi.md, "CONFigure, MEASure, READ, FETCh"](scpi.md#configure-measure-read-fetch) |
| `101,"Shunt bypassed;..."` on `INIT` or `MEAS` | The relays are on the null path (`path=bypass`) or the bypass is across a closed lead (`path=both`) | `ROUT:STAT?` `path=` | `ROUT:NULL OFF`; a capture there only with `INIT:FORC`, and it is `q=invalid` | [scpi.md, "ROUTe: the relay module"](scpi.md#route-the-relay-module) |
| `111,"Not running;no capture or null window is open"` | `ABOR` or `TRAC:MARK` with nothing open: the window had already closed | `SYST:STAT?` `running=0 sink=idle` | Nothing; read the closed window with `FETC:STAT?` | [scpi.md, "INITiate, ABORt"](scpi.md#initiate-abort) |
| `q=uncal` | This range has no null (each range keeps its own), or the shunt was never written | `CAL:DATA?`: `nulled_r0=`, `nulled_r1=`, `shunt_measured=` | Take the null on this range; `kleidos-meter calibration verify` | [usage.md, "Is the number good"](usage.md#is-the-number-good-q-path-and-check), [architecture.md §3.7](architecture.md#37-the-offset-null-and-the-quality-token) |
| `q=stale` | The die moved more than 5 °C since the null | `SYST:STAT?` `die_mc=` against `CAL:DATA?` `off_die_mc_r<n>=` | Take the null again | [architecture.md §3.7](architecture.md#37-the-offset-null-and-the-quality-token) |
| `q=invalid` | The shunt was off the DUT's current at some moment of the window, or the INA228's arithmetic overflowed; while the null path is on it is expected | `ROUT:STAT?` `path=`; `FETC:STAT?` `flags=` | Throw the capture away; `ROUT:NULL OFF` before measuring | [architecture.md §3.7](architecture.md#37-the-offset-null-and-the-quality-token) |
| `pace=timer` on `STATE` or `SLEEP`, or `pace_fallback=1` | The `ALE` wire (terminal 6): the conversion-ready pin did not pulse, or stopped, and the timer took over | `FETC:HEAD?` `pace=`, `pace_edges=`; `FETC:STAT?` `pace_fallback=` | Check the wire and its screw before the part; the capture is still good, timed by the S3's clock | [architecture.md §3.3](architecture.md#33-pacing), [rig-wiring.md §5](rig-wiring.md#5-the-first-test) |
| `read_err=` above 0, most often on `BURST` | Transactions that failed on the I²C bus: long wires or weak pull-ups at 1 MHz | `FETC:STAT?` `read_err=`; `DIAG:I2C:SCAN?` lists `0x40` alone | Shorten `SDA` and `SCL`, or add pull-ups; compare `read_err=` at 400 kHz and 100 kHz on `SLEEP` | [rig-wiring.md §5](rig-wiring.md#5-the-first-test), [architecture.md §2.4](architecture.md#24-the-own-i2c-master-fw-13-fw-14) |
| `dropped=` above 0 | Samples the console could not write in time | `FETC:STAT?` `dropped=` | Take the capture again; a run with holes is `INCOMPLETE` | [usage.md, "What the line carries"](usage.md#what-the-line-carries) |
| A ring dump that never ends, or whose CRC-32 does not match | The 6 Mbaud line has no flow control, and a long block sometimes arrives damaged | the closing row's `crc32=` against the block | Ask again; `RAW` is a tenth of `SCALed`'s size. `kleidos-meter capture` and the panel do both | [testing.md, "The bench suite"](testing.md#the-bench-suite), [scpi.md, "The ring dump"](scpi.md#the-ring-dump) |
| The panel's line stays below the spikes, or is flatter than the samples | The line is a mean: of each pixel column, or with smoothing of the window chosen, so a pulse shorter than either is drawn lower and wider. Nothing recorded changed | the caption at the top right of the chart (`mean per pixel, 4.81 ms`, `moving average 101 ms (100 ms asked)`) and the faint band, which keeps each column's extremes | Smoothing Off and zoom in; read a peak from the band, the readout's `min` and `max` or the run's exports, never from the line | [panel.md, "The chart"](panel.md#the-chart) |
| Samples come every 18.4 ms on `STATE` instead of 1.15 ms, and the status bar says `AVG 16` | The INA228's noise reduction is on for the profile: it averages that many conversions into each sample, which changes the measurement | `SENS:PROF:REP?`: `avg_state=on`, `avg=`, `period_us=` | Turn it off on the profile's card in Settings, or `SENS:AVER OFF`; it is off after a boot and after `*RST`. A run taken with it says so in its `meta.json` | [panel.md, "Noise reduction in the INA228"](panel.md#noise-reduction-in-the-ina228), [scpi.md, "SENSe, SAMPle, FORMat, TRIGger"](scpi.md#sense-sample-format-trigger) |
| `check=unverified` after `ROUT:NULL ON` | The DUT draws under 2 mA, too little to see the contacts move; the order was carried out | `ROUT:RES?` `vs_pv=` | Nothing; take the null with the DUT awake when you can | [usage.md, "Is the number good"](usage.md#is-the-number-good-q-path-and-check) |
| `103` (bypass check failed) or `104` (lead check failed) on `ROUT:NULL ON` | The bypass took no current, or the shunt still conducts with the lead commanded open: a contact that did not move, or a wire | `ROUT:RES?` `vs_pv=`; `ROUT:STAT?` `path=shunt` again | Check the Relay 2 Click and K1's wiring; the instrument has put the relays back | [scpi.md, "ROUTe: the relay module"](scpi.md#route-the-relay-module), [architecture.md §6](architecture.md#6-the-relays) |
| `-240` `i2c ... (null-path check: ...)` | The shunt reading the check needs failed on the bus | `DIAG:I2C:SCAN?` | The bus first (the `read_err=` row above) | [scpi.md, "ROUTe: the relay module"](scpi.md#route-the-relay-module) |
| `109` or `110` on a relay order | An order that leaves the DUT with no supply, or takes the shunt off a running capture's current | `ROUT:STAT?` | Use `ROUT:NULL ON` and `OFF`; add `FORC` only when you mean it | [scpi.md, "ROUTe: the relay module"](scpi.md#route-the-relay-module) |
| `102,"Null not stored;..."` and `stored=0` | A null with K1 commanded open read beyond 50 µV: the lead may not have opened. The null stored before stands | `CAL:ZERO:RES?` `offset_pv=`, `path=` | Check K1 and its wiring; take the null again | [calibration/README.md, "The null"](../calibration/README.md#the-null-before-every-measurement-session) |
| `113`, `114` or `115` in the queue | The hold put the relays back after 120 s on a path left off the shunt: a session that ended between `ON` and `OFF`. Under a capture, its moves are marked and `hold_teardowns=` counts them | `ROUT:STAT?` `hold_released=`; `FETC:STAT?` `hold_teardowns=` | Nothing on the instrument: it is back on the shunt. A capture with `hold_teardowns=` above 0 is void | [scpi.md, "ROUTe: the relay module"](scpi.md#route-the-relay-module) |
| `116` in the queue after a boot | A panic, a watchdog or a brown-out reset found coils energized and released them | `SYST:HEAL?` `reset=`, `reset_released=` | Report a reset you did not ask for (`DIAG:WATC:TEST` asks for `task_wdt`) | [scpi.md, "The error queue"](scpi.md#the-error-queue) |
| `117,"Pin map reverted;..."` after Apply in the Pins view, which says `reverted` | The map was tried and failed its proof: nothing answered on its bus pins or address, what answered was not a sound INA228, no edge reached ALERT, or a relay pad did not read its level. The last good map is back by itself | `SYST:PINS:RES?`: `reason=` (`no_chip`, `chip`, `alert`, `pads`, ...), `mfg=`, `die=`; `SYST:PINS?` | Nothing to undo. Wire the part to the pins the map names, or choose the pins it is wired to, and apply again; Defaults (`SYST:PINS:DEF` from the console) gives the rig's map back | [panel.md, "The pin view"](panel.md#the-pin-view), [scpi.md, "The pin map"](scpi.md#the-pin-map) |
| The instrument does not answer after a pin change: no reading, `-241` or `117` in the queue, `chip=none` | The INA228 is not on the bus the live map names. An Apply proves its map and puts the last good one back by itself, so this follows a boot on a stored map whose bus does not answer: the boot falls back to the default bus (`src=fallback`) unless another role of the map holds GPIO4 or GPIO5, and then runs with no INA228 (`src=stored`, `chip=none`). Or the wires moved and the map did not | `SYST:PINS?`: `src=` and each role's pin; `SYST:INF?` `chip=`; `SYST:ERR:ALL?`, whose `117` names the stored bus | `SYST:PINS:DEF` on the meter's port (stop `serve` first, or come in through its passthrough): it needs no INA228, and no map can take the console's UART, GPIO43 and GPIO44, so the CH343 port always answers. Then wire the part to the pins the map names, or stage the pins it is wired to and apply. A console that does not answer either is the line, not the map: the `# the instrument is not answering` row | [scpi.md, "The pin map"](scpi.md#the-pin-map), [panel.md, "The pin view"](panel.md#the-pin-view), [carrier-board.md §2.2](carrier-board.md#22-which-pins-a-role-may-take) |
| `107` (sampler not running) | The sampling engine did not start, or did not acknowledge a stop | `DIAG:WATC?` `stalls=`; `*TST?` | Reboot the meter and report it | [scpi.md, "The error queue"](scpi.md#the-error-queue) |
| `-241,"Hardware missing;..."`, `chip=none` | The INA228 did not come up: its wiring | `SYST:INF?` `chip=`, `DIAG:I2C:SCAN?` | The `GND`, `VCC`, `SDA` and `SCL` wires, in that order | [rig-wiring.md §5](rig-wiring.md#5-the-first-test) |
| `calibration verify` says `working_copy_lost` | A full flash erase, or the first flash of 0.17.0 over an earlier image | `CAL:DATA?` `shunt_measured=0` | `kleidos-meter calibration restore` | [calibration/README.md, "What a verdict asks of you"](../calibration/README.md#what-a-verdict-asks-of-you) |
| `calibration verify` says anything else but `applied` | A typed shunt, a calibration that ended, another image or rig | its lines name what differs | The verdict's row in the calibration README | [calibration/README.md, "What a verdict asks of you"](../calibration/README.md#what-a-verdict-asks-of-you) |

## What some of them look like

A message of commands alone answers nothing:

```console
$ kleidos-meter --timeout 2 'SENS:PROF STAT'
# 'SENS:PROF STAT': no response within 2 s
```

A message over 255 bytes is discarded whole and answers nothing either; the next message reads the refusal. Here on the simulator, which refuses it as the instrument does (the message, 31 units and 335 bytes, is cut in the first line):

```console
$ kleidos-meter --sim --timeout 2 "$(printf 'SYST:VERS?;%.0s' $(seq 30))*IDN?" 'SYST:ERR:ALL?'
# the instrument rebooted (*ESR? power-on bit set)
# 'SYST:VERS?;SYST:VERS?;[...];*IDN?': no response within 2 s
-363,"Input buffer overrun;program message longer than 255 bytes"
```

`BURST` at the default bus, and a capture left open:

```console
$ kleidos-meter 'SENS:PROF BURS;:SYST:ERR:ALL?' 'INIT;:SYST:ERR:ALL?'
0,"No error"
-221,"Settings conflict;BURST needs a faster bus (a 100 us period needs a read under 50 us; it is 95 us at 400000 Hz)"
$ kleidos-meter 'SENS:PROF STAT;:SENS:CAPT STAT;:SYST:ERR:ALL?' 'INIT;:SYST:ERR:ALL?' 'SENS:PROF SLE;:SYST:ERR:ALL?' 'FETC:STAT?;:SYST:ERR:ALL?' 'ABOR;:SYST:ERR:ALL?' 'ABOR;:SYST:ERR:ALL?'
0,"No error"
0,"No error"
-221,"Settings conflict;capture running (ABORt first)"
-230,"Data corrupt or stale;capture running (ABORt first)"
0,"No error"
111,"Not running;no capture or null window is open"
```

The null path left on, then the hold 120 s later:

```console
$ kleidos-meter 'ROUT:NULL ON;:ROUT:RES?;:SYST:ERR:ALL?' 'INIT;:SYST:ERR:ALL?' 'MEAS:CURR?;:SYST:ERR:ALL?'
k1=lead:open k2=bypass:closed k3=spare:unwired k4=spare:unwired path=bypass check=unverified moved=k2,k1 settle_us=71989 live=0 marked=0 vs_pv=4062500,1875000,-312500 hold_s=120 q=invalid;0,"No error"
101,"Shunt bypassed;shunt bypassed (path=bypass: ROUTe:NULLpath OFF, or INITiate:FORCe)"
101,"Shunt bypassed;shunt bypassed (path=bypass: ROUTe:NULLpath OFF first)"
$ sleep 125
$ kleidos-meter 'SYST:ERR:ALL?' 'ROUT:STAT?'
113,"Null path released;the hold released the null path after 120 s (ROUTe:NULLpath ON again)"
k1=lead:closed k2=bypass:open k3=spare:unwired k4=spare:unwired path=shunt check=- pads=ok operate_settle_ms=20 release_settle_ms=50 hold_s=- hold_released=2 bypass_part=relay2click
```

A range with no null of its own: range 1 reads `q=uncal` while range 0, nulled, reads `q=cal` (the reports are cut to the keys that matter):

```console
$ kleidos-meter 'SENS:CURR:RANG:IND 1;:SYST:ERR:ALL?' 'SYST:STAT?' 'CAL:DATA?'
0,"No error"
running=0 sink=idle profile=TRACE ... range=1 ... q=uncal trig=-
shunt_uohm=100116 shunt_measured=1 off_pv_r0=-296274 off_pv_r1=0 nulled_r0=1 nulled_r1=0 ...
```

The port while `kleidos-meter serve` holds it, for the plain command, `capture` and `calibration verify` (whose last line is a traceback's):

```console
$ kleidos-meter '*IDN?'
# <port> is held by another opener (kleidos-meter serve, a QA run or another tool): close that one first
$ kleidos-meter capture --profile STATE --seconds 1
# the port is held by kleidos-meter serve (pid 25848, since 2026-11-24T07:59:25+01:00, panel http://127.0.0.1:8040/)
$ kleidos-meter calibration verify
kleidos_meter.client.PortBusyError: <port> is held by another opener (kleidos-meter serve, a QA run or another tool): close that one first
```

A tool that set both lines before the open rebooted the meter; the next command says so, and the uptime is a quarter of a second:

```console
$ kleidos-meter 'SYST:HEAL?' 'ROUT:STAT?' 'SYST:ERR:ALL?'
# the instrument rebooted (*ESR? power-on bit set)
reset=poweron uptime_us=262913 elf=0f2b6ed8b28068a4 fw=0.17.0 stack_console=7195 stack_sampler=3032 allocs_after_init=0 frees_after_init=0 hold_released=0 errors=0 reset_released=-
k1=lead:closed k2=bypass:open k3=spare:unwired k4=spare:unwired path=shunt check=- pads=ok operate_settle_ms=20 release_settle_ms=50 hold_s=- hold_released=0 bypass_part=relay2click
0,"No error"
```

The upload guard on a port that does not exist (the build runs first; the guard stops the upload before anything is written):

```console
$ pio run --project-dir firmware -e meter -t upload --upload-port /dev/cu.none
[...]
*** meter upload refused: /dev/cu.none does not exist. Is the meter's CH343 cable plugged in?
[...]
*** [upload] Explicit exit, status 1
```
