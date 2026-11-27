<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# The meter's SCPI command set

The instrument speaks SCPI-1999 over its CH343 serial line: 6,000,000 baud, 8N1, no flow control, one program message per line. This page is the protocol: the grammar, every command, every response, the error numbers, the stream records, the deviations from SCPI-1999, and how each command of the console protocol that came before it maps to the new one. The firmware that implements it is `firmware/src/console.cpp`, `firmware/src/console_commands.cpp` and the parser `firmware/src/scpi.h`; the reference client is `host/kleidos_meter/`. Which version brought which command or key, and the rule a change follows, are in [`CHANGELOG.md`](../CHANGELOG.md).

## Contents

- [Quick reference](#quick-reference)
- [The line](#the-line)
- [Grammar](#grammar)
- [Responses](#responses)
- [Errors and status](#errors-and-status)
- [Command reference](#command-reference)
- [Captures and the stream](#captures-and-the-stream)
- [Deviations from SCPI-1999](#deviations-from-scpi-1999)
- [Old console command to SCPI](#old-console-command-to-scpi)

## Quick reference

What a QA session sends, in the order it sends it. Every message a host sends ends in `;:SYST:ERR:ALL?`, which is left out of the table: it answers `0,"No error"`, or the reason of each refusal (["The error queue"](#the-error-queue)), and it is the only answer a message of commands alone gives. Each header is in its short form; [Command reference](#command-reference) has it whole, with its flags. What to do when an answer is not the one here is in [the troubleshooting page](troubleshooting.md).

| To | Send | Comes back |
|---|---|---|
| check the line | `\n`, then `*ESR?` | `0`; with bit 7 (128) set, the instrument rebooted since it was last asked ([The line](#the-line)) |
| start with an empty queue | `*CLS` | nothing |
| know the instrument | `*IDN?`, `SYST:INF?` | its serial and version; `chip=ina228`, the `image=` and its `elf=`, `q=` |
| know that it is well | `SYST:HEAL?`, `*TST?` | `reset=`, `uptime_us=`, the counts that must be 0; `0` |
| know what calibration it holds | `CAL:DATA?` | the shunt and the null of each range, the tag and the date ([CALibration](#calibration)) |
| know the pins it drives | `SYST:PINS?` | each role's GPIO, where the map came from, the board table's digest ([The pin map](#the-pin-map)) |
| know the circuit | `ROUT:STAT?` | `path=shunt`, the one a capture measures on ([ROUTe](#route-the-relay-module)) |
| choose a profile | `SENS:PROF SLE`, `SENS:PROF:REP?` | nothing; its period and its conversions |
| choose a range | `SENS:CURR:RANG:IND 0`, `SENS:CURR:RANG:REP?` | nothing; its ceiling and the null's `q=` there |
| take the null | `ROUT:NULL ON`, `ROUT:RES?` | nothing; `path=bypass`, `check=` |
| | `CAL:ZERO 60` | nothing: the window is open, and the 60 s are the host's to wait |
| | `ABOR`, `CAL:ZERO:RES?` | nothing; `offset_pv=`, `stored=1` |
| | `ROUT:NULL OFF`, `ROUT:RES?` | nothing; `path=shunt` |
| take one figure | `MEAS:CURR?`, `MEAS:VOLT?`, `MEAS:POW?` | amperes, volts, watts, the mean of `SAMP:COUN?` samples |
| choose what is streamed | `SENS:FUNC:ON "CURR","VOLT"` | nothing; `SENS:FUNC:ON:DEF` goes back to the profile's own |
| keep the statistics alone | `SENS:CAPT STAT` | nothing; `SENS:CAPT DATA` streams or fills the ring again |
| allow `BURST` | `SYST:COMM:I2C:FREQ 1000000` | nothing; `*RST` puts the pin map's boot clock back, 400 kHz by default |
| start a capture | `INIT`, `FETC:HEAD?` | nothing; what the samples are converted with ([The capture header](#the-capture-header)) |
| look while it runs | `SYST:STAT?` | `samples=`, `dropped=`, `read_err=`, the last reading |
| mark a moment | `TRAC:MARK "wake"` | nothing but the mark's record in the stream |
| end it | `ABOR`, `FETC:STAT?` | nothing; the figures of the whole window, and its `q=` |
| read a ring out | `TRAC:POIN?`, `TRAC:DATA? RAW` | how many samples it holds; the block, held to its CRC-32 ([The ring dump](#the-ring-dump)) |
| cut the DUT's battery for a moment | `ROUT:LEAD:CYCL 2000,FORC` | nothing; the instrument recloses it |
| leave it at rest | `*RST`, `ROUT:STAT?`, `SYST:STAT?` | nothing; `path=shunt`; `running=0 sink=idle` |

### A worked session

The bench, 2026-11-24, firmware 0.17.0, a T-Deck asleep on its battery as the DUT: the check, a null, a `STATE` capture with a mark, the end state. `>` is what the host sent and `<` what it read, one line each; ` ... ` inside an answer and a `...` line stand for what is left out, and the host waited 61 s between `CAL:ZERO` and its `ABOR`. `check=unverified` is what a DUT under 2 mA gives. A report that ends in ` ...` is one to which a later version appended keys (0.18.0: the pin map's and the averaging override's). The messages run on the simulator too, whose answers have these keys (`host/tests/test_scpi_doc.py`).

```text
> *ESR?
< 0
> *CLS;:SYST:INF?;:SYST:HEAL?;:SYST:ERR:ALL?
< model=kleidos-meter fw=0.17.0 chip=ina228 addr=0x40 mfg=0x5449 die=0x228 rev=0x1 shunt_uohm=100116 cal_tag=R100_20261120_HR q=cal relays=1 bypass_part=relay2click serial=KM-0000000000 image=meter elf=0f2b6ed8b28068a4 ...;reset=poweron uptime_us=19850732 elf=0f2b6ed8b28068a4 fw=0.17.0 stack_console=7000 stack_sampler=3032 allocs_after_init=0 frees_after_init=0 hold_released=0 errors=0 reset_released=- ...;0,"No error"
> *TST?;:SYST:ERR:ALL?
< 0;0,"No error"
> SENS:PROF SLE;:SENS:CURR:RANG:IND 0;:SYST:ERR:ALL?
< 0,"No error"
> ROUT:NULL ON;:ROUT:RES?;:SYST:ERR:ALL?
< k1=lead:open k2=bypass:closed k3=spare:unwired k4=spare:unwired path=bypass check=unverified moved=k2,k1 settle_us=71987 live=0 marked=0 vs_pv=4062500,1250000,-937500 hold_s=120 q=invalid;0,"No error"
> CAL:ZERO 60;:SYST:ERR:ALL?
< 0,"No error"
> ABOR;:CAL:ZERO:RES?;:SYST:ERR:ALL?
< running=0 range=0 offset_pv=-123873 sd_pv=263437 over_s=60 die_mc=21695 n=888 k1=lead:open k2=bypass:closed k3=spare:unwired k4=spare:unwired path=bypass check=unverified stored=1;0,"No error"
> ROUT:NULL OFF;:ROUT:RES?;:SYST:ERR:ALL?
< k1=lead:closed k2=bypass:open k3=spare:unwired k4=spare:unwired path=shunt check=- moved=k1,k2 settle_us=61591 live=0 marked=0 vs_pv=- hold_s=- q=cal;0,"No error"
> SENS:PROF STAT;:SENS:PROF:REP?;:SYST:ERR:ALL?
< profile=STATE adc_config=0xF140 period_us=1152 vshct_us=1052 avg=1 ...;0,"No error"
> SENS:FUNC:ON "CURR","VOLT";:SYST:ERR:ALL?
< 0,"No error"
> INIT;:FETC:HEAD?;:SYST:ERR:ALL?
< fw=0.17.0 chip=ina228 ... period_us=1152 pace=alert pace_edges=8 pace_expect=8 ... shunt_uohm=100116 shunt_measured=1 offset_pv=-123873 off_die_mc=21695 vbus_side=source cal_tag=R100_20261120_HR q=cal die_mc_start=21593 ... sink=stream ... t0_us=81999130 format=text relays=1 k1=lead:closed k2=bypass:open k3=spare:unwired k4=spare:unwired path=shunt check=- ... limit=0;0,"No error"
< METER t=81999969 ch=i_ua v=32.451
< METER t=81999969 ch=v_mv v=4106.832
...
> SYST:STAT?;:SYST:ERR:ALL?
< running=1 sink=stream profile=STATE period_us=1152 pace=alert pace_fallback=0 range=0 samples=1780 dropped=0 read_err=0 i_na=60543 vbus_uv=4106835 die_mc=21093 die_age_us=679 flags=0xC003 scl_hz=400000 q=cal trig=-;0,"No error"
...
> TRAC:MARK "wake";:SYST:ERR:ALL?
< 0,"No error"
...
< METER_MARK mark=wake n=2723 t=85136802
< METER t=85137236 ch=i_ua v=32.451
< METER t=85137236 ch=v_mv v=4106.832
...
> ABOR;:FETC:STAT?;:SYST:ERR:ALL?
...
< METER t=87198400 ch=i_ua v=7.480
< METER t=87198400 ch=v_mv v=4122.069
< samples=4513 dropped=0 read_err=0 pace_fallback=0 pace_err_ppm=116 pace_unmet=0 dur_us=5199270 mean_na=42126.647 min_na=-29976 max_na=122971 die_mc_end=22601 flags=0xC003 charge_raw_start=234 charge_raw_end=301 osc_tol_ppm=10000 q=cal ... hold_teardowns=0;0,"No error"
> *RST;:ROUT:STAT?;:SYST:STAT?;:SYST:ERR:ALL?
< k1=lead:closed k2=bypass:open k3=spare:unwired k4=spare:unwired path=shunt check=- pads=ok operate_settle_ms=20 release_settle_ms=50 hold_s=- hold_released=0 bypass_part=relay2click ...;running=0 sink=idle profile=TRACE period_us=0 pace=alert pace_fallback=0 range=0 samples=4513 dropped=0 read_err=0 i_na=7480 vbus_uv=4122070 die_mc=22601 die_age_us=50311 flags=0xC003 scl_hz=400000 q=cal trig=-;0,"No error"
```

## The line

- Open the port with the lines as the OS leaves them, **then release RTS, then DTR**: the carrier's two transistors pull EN low for one line state only, DTR released while RTS is still asserted (`docs/carrier-board.md` §7). Setting both lines before the open does not keep that order: pyserial 3.5 applies DTR first and RTS second inside `open()`, which passes through the reset. A client that reboots the instrument on open finds `*ESR?` reporting 128 (power on).
- **Check the line before the first message.** Send a bare `\n` (it ends any partial line and is no message), drain what the line holds, then ask `*ESR?` with short timeouts until it answers: a booting instrument discards everything it receives, and its boot runs a PSRAM test first. Bit 7 of the answer says the instrument rebooted since the register was last read; `*ESR?` clears it, so ask it once, before `*CLS`. Drain again after the answer, so that a late reply to an earlier attempt is not read as the next response. The reference client does this in `Meter.wait_ready()`, and its command line before any message.
- Every program message ends in `\n` (`\r` is accepted too). At most 255 bytes; a longer one is discarded to its newline and queues `-363`. A message holds printable ASCII and tabs only: a byte below 0x20 (other than a tab) or above 0x7E discards the whole message to its newline and queues `-101`, which names the byte in hex and never echoes it (0.6.0 and later).
- While a command holds the console (a dump, an acquisition, a relay settle), what the host sends waits in a 1,024-byte receive ring and the UART's 128-byte receive FIFO behind it, 1,152 bytes, and in the console's own 1,024-byte inbox during an acquisition. Input the ring and the FIFO could not hold is lost: the console queues `-363` (receive overrun) and discards the message the loss fell in (0.6.0 and later), one error for that message however many times the FIFO overflowed before it ended (0.17.0 and later; until then a dump, which looks for a loss at every row, queued one for each overflow it found).
- Nothing is printed at boot. A reboot shows as the power-on bit of `*ESR?`, and any bring-up failure as an error in the queue.

## Grammar

A **program message** is one or more **units** separated by `;`. A unit is a header, optional whitespace, and parameters separated by `,`.

```text
SENS:PROF STAT;:SENS:CURR:RANG:IND 1;:SYST:ERR:ALL?
```

- **Mnemonics** are case-insensitive and take the long form or the short form (the upper-case part in this page): `SENSe:PROFile` is `SENS:PROF`, `sense:profile` or `SENSE:PROF`.
- **Optional nodes** are in square brackets: `MEASure[:SCALar]:CURRent[:DC]?` answers to `MEAS:CURR?` and to `MEAS:SCAL:CURR:DC?`.
- **Numeric suffixes**: `ROUTe:RELay3` addresses relay 3. A missing suffix is 1.
- **Paths**: a header that starts with `:` starts from the root. One that does not, after a `;`, continues from the path of the previous unit (SCPI-1999 vol. 1 §6.2.4): `SENS:CURR:RANG:IND 1;REP?` is `SENS:CURR:RANG:IND 1;:SENS:CURR:RANG:REP?`, and `SENS:CURR:RANG 1;INDex?` is `SENS:CURR:RANG 1;:SENS:CURR:INDex?`, which is no command (`-113`): the path is the header less its last node. Common commands (`*IDN?`) never change the path. A client that is not sure writes the leading `:` on every unit after the first.
- **Parameters**:
  - numbers in NR1, NR2 or NR3 (`60`, `0.1`, `1.0E-1`), decoded exactly into the integer unit the firmware works in, never through a float; integers also as `#H40`, `#Q100`, `#B1000000`;
  - booleans `ON`, `OFF`, `1`, `0`;
  - character data, long or short form: `TRACe`, `TRAC`;
  - strings in `"..."` or `'...'`, a doubled quote standing for one inside.
- Every unit of a message runs, in order, **even after one is refused**: the refused unit queues an error and changes nothing, and the next unit still runs. That is what lets a client end every message with `SYSTem:ERRor:ALL?` and always read its verdict.

## Responses

- A **command** (no `?`) produces no response. The one output a command makes is a stream record: `TRACe:MARK` and a `LIVE` relay move write a mark record (below), which is not a response unit.
- A **query** produces exactly one response unit. The units of one message are written as one line, joined by `;` and terminated by `\n`.
- Numeric responses are NR1 or NR2 in the SI base unit: amperes, volts, watts, joules, coulombs, ohms, seconds, degrees Celsius. They are written from the device's own integers with no float and no locale, trailing zeros trimmed: `MEAS:CURR?` answers `0.088066019`.
- **Report** responses (`SYSTem:STATus?`, `FETCh:STATistics?`, `FETCh:HEADer?`, `CALibration:DATA?` and the other `...?` queries this page calls reports) are a list of `key=value` tokens separated by single spaces, in which no value ever contains a space, a `;` or a `"`. Their values keep the integer units the device computes in, named in the key: `_na` nanoamps, `_pv` picovolts, `_uohm` micro-ohms, `_mc` milli-degrees Celsius, `_us` microseconds. A value that does not exist yet is `-`. Hosts parse them with `\b(\w+)=(\S+)`.
- A **block** response (`TRACe:DATA?`) is an IEEE 488.2 definite-length arbitrary block: `#`, one digit giving the length of the byte count, the byte count, then exactly that many bytes. The bytes are text rows, each ending in `\n`. The message's `\n` follows the block, after any further units.
- **Stream records** (samples, marks) are lines of their own and are never written inside a response. A client tells them apart by their first characters (see [the stream](#captures-and-the-stream)).

## Errors and status

### The error queue

`SYSTem:ERRor[:NEXT]?` returns the oldest error, `SYSTem:ERRor:ALL?` all of them oldest first, comma-separated, and `SYSTem:ERRor:COUNt?` how many are queued. An empty queue answers `0,"No error"`. Each error is `<code>,"<standard text>;<detail>"`: the detail is the firmware's reason and, where there is one, the command that releases it.

```text
-221,"Settings conflict;capture running (ABORt first)"
```

The queue holds 10. On overflow the newest entry is replaced by `-350,"Queue overflow"`.

| Code | Text | When |
|---|---|---|
| 0 | No error | |
| -101 | Invalid character | a byte that is not printable ASCII or a tab; the message is discarded |
| -102 | Syntax error | an empty parameter (`1,,2`) |
| -104 | Data type error | a number where a word was expected, or the reverse |
| -108 | Parameter not allowed | more parameters than the command takes (the detail says how many it does: `SENS:PROF takes at most 1 parameter`); nothing is changed. From 0.17.0 for every command; before, only a command that takes none, `CALibration:DATE` and a unit of more than 8 refused one, and the rest ignored what they did not take |
| -109 | Missing parameter | a parameter the command cannot do without; nothing is changed |
| -113 | Undefined header | no such command, or a query form a command does not have |
| -114 | Header suffix out of range | `ROUTe:RELay1`, `ROUTe:RELay5` |
| -120 | Numeric data error | a malformed number |
| -150 | String data error | an unterminated or overlong string |
| -221 | Settings conflict | a capture is running; a ring profile given channels; a sampler refusal |
| -222 | Data out of range | a value outside its window |
| -224 | Illegal parameter value | a word that is not one of the choices |
| -230 | Data corrupt or stale | nothing to fetch yet; an acquisition cut short |
| -240 | Hardware error | an I2C transaction failed; from 0.18.0 also `SYSTem:PINS:DEFault` whose record the flash did not take (`the default map is live and was not saved (a reboot loads the stored one)`) |
| -241 | Hardware missing | the INA228 did not come up (the detail names why); an order for a relay the rig does not wire (0.13.0) |
| -350 | Queue overflow | |
| -363 | Input buffer overrun | a program message longer than 255 bytes; input lost while the console was busy |
| 100 | Relays unavailable | the relay inputs could not be configured at boot |
| 101 | Shunt bypassed | a capture, a measurement or a null on a circuit the shunt does not carry whole |
| 102 | Null not stored | a relay null beyond 50 uV: K1 may not have opened; the stored null stands |
| 103 | Bypass check failed | `ROUTe:NULLpath ON` saw K2 take no current, or, from 0.18.0, saw the shunt lose all of the DUT's current at K2's step (a lead that opened, not a bypass that closed); the path is released |
| 104 | Lead check failed | `ROUTe:NULLpath ON` saw the shunt still conduct after K1 opened, or, from 0.18.0, before it moved anything with K1 already open; the path is released |
| 105 | Marks full | 16 marks per capture (`TRACe:MARK` and `LIVE` moves; from 0.16.0 two more slots are kept for the hold's own marks, which this never refuses) |
| 106 | Calibration not saved | the calibration record was not written to the flash (a write that failed or did not read back, or a shunt outside 0.05..0.2 Ω, which is never stored): the value is live until reboot. The detail reads `calibration write failed (live until reboot)` from FW-16 (`nvs write failed` before) |
| 107 | Sampler not running | the sampling engine failed to start; the sampler task did not acknowledge a stop within 500 ms (`ABORt`, an acquisition): the window is closed with no result, and a new one is refused with `107` until the task acknowledges; asynchronous from 0.14.0: a running capture took no sample for 2 s ("the sampler took no sample for <n> ms"), once a capture, with QUEStionable TIME set |
| 108 | Null took no samples | |
| 109 | Order disconnects the DUT | K1 open and K2 open, without `FORCe` |
| 110 | Order bypasses the shunt | under a running capture, without `FORCe` |
| 111 | Not running | `ABORt` or `TRACe:MARK` with no window open |
| 112 | Null window running | a relay order or a reconfiguration while `CALibration:ZERO` runs, or (flag **C**) before a null window that ended by itself is closed |
| 113 | Null path released | asynchronous (0.12.0): the hold released a null path left standing (`path=bypass`) 120 s after the order that left it or its last window closed; the DUT is back on its shunt (`ROUTe:NULLpath ON` to take it again). From 0.16.0 also under a capture, which no longer suspends the hold: the detail then says `under a capture, marked hold:` and each move is a mark record ("Stream records") |
| 114 | Lead reclosed | asynchronous (0.13.0): the hold reclosed a lead left open (`path=none`) 120 s after the order that left it or its last window closed; the DUT has its battery again. From 0.16.0 also under a capture, marked as for `113` |
| 115 | Bypass opened | asynchronous (0.16.0): the hold opened a bypass left closed across a closed lead (`path=both`) 120 s after the order that left it or its last window closed; the shunt carries the DUT's whole current again (`ROUTe:BYPass ON` to close it again). Under a capture, marked as for `113` |
| 116 | Relays released by reset | asynchronous (0.16.0), queued at boot: the reset before it (a panic, a watchdog, `esp_restart()`, a brown-out: any reset that keeps RTC memory) found coils energized and released them all at once, as every boot does. The detail names the reset, the coils and the path they made (`a reset (task_wdt) released k1,k2 (path=bypass)`): from `path=bypass` the DUT may have lost its battery for a release time, from `path=none` it had none. A power-on reports nothing. `SYSTem:HEALth?` keeps it as `reset_released` |
| 117 | Pin map reverted | (0.18.0) at boot, asynchronous: the INA228 did not answer on a stored pin map's bus. The detail names that bus and what took its place: `the stored bus (sda=18 scl=21 addr=auto) did not answer: the default bus is back (the record is kept)` (`SYSTem:PINS?` reads `src=fallback`), or `... did not answer and a role holds the default bus pins: no INA228` (`src=stored`, `chip=none`; it stands in the place of the `-241` the boot would queue). `SYSTem:PINS:DEFault` is the way back from either. From `SYSTem:PINS:APPLy`, the one error of a map that was put back: `the INA228 did not answer on sda=18 scl=5 addr=auto: the last good map is back`, `no ALERT edge on GPIO10: the last good map is back`, `the relays at rest took the dut's current off the shunt (vs_pv=<readings>): the last good map is back` (`rest`, below), or `the map failed its proof (<reason>): the last good map is back` with `SYSTem:PINS:RESult?`'s `reason`; `; it fails too (tst=<bits>)` ends it when the map put back does not pass either |

### Status registers

| Register | Bits |
|---|---|
| Status Byte (`*STB?`, `*SRE`) | 2 error queue not empty, 3 questionable summary, 5 event summary (ESR and ESE), 6 master summary, 7 operation summary |
| Standard Event (`*ESR?`, `*ESE`) | 0 operation complete, 2 query error, 3 device-dependent error (-3xx and every positive code), 4 execution error (-2xx), 5 command error (-1xx), 7 power on |
| Operation (`STATus:OPERation`) | 0 calibrating (a null window is open), 4 measuring (a capture is running) |
| Questionable (`STATus:QUEStionable`) | 1 current (MATHOF or a shunt limit flag set), 2 time (0.12.0: the open or last capture's pace fell back to the timer, a read failed, or `pace_unmet`; from 0.14.0 also a running capture that took no sample for 2 s), 8 calibration (the live quality is not `cal`), 9 data lost (0.12.0: samples of the open or last capture did not reach the host, `dropped=`), 10 hold (0.16.0: the hold put the relays back to rest under the open or last capture while it sampled, `hold_teardowns=`) |

`:CONDition?` reads a register as it stands now; `[:EVENt]?` reads the bits that changed through the transition filters since it was last read, and clears them; `:ENABle` selects which event bits reach the Status Byte. `:PTRansition <mask>` and `:NTRansition <mask>` (0.12.0, 0..32767, both registers) choose which edges of a condition bit set its event bit: a rise through PTR (default every bit), a fall through NTR (default none); before 0.12.0 every rise and no fall did. `STATus:PRESet` clears both enable masks and sets both filters to their defaults; `*CLS` clears the queue, the ESR and both event registers.

## Command reference

Flags: **D** needs the INA228 (refused with `-241` otherwise); **R** refused while a capture or a null window runs (`-221`, or `112` for a null window); **C** also refused while a window that ended by itself (a filled ring, a null window's full count) is not yet closed by `ABORt`, because the window's closing reads rest on what these commands change: the range, the profile, the CHARGE accumulator, the latched `DIAG_ALRT` flags (0.6.0 and later; before, `SENSe:CHARge:RESet` on a filled ring was accepted and zeroed the accumulator between the window's two boundary reads).

How the **Command** column is written:

- A header is written as the firmware's command table holds it (`kCommands` in `firmware/src/console.cpp`), with the parameters it takes; one written without parameters takes none. `<n>` in a header is its numeric suffix.
- ` / ` joins a setting and its query. The flags are the setting's; the query has none.
- ` · ` joins headers that share the row, its flags included.
- A header that starts with `:` continues the one before it from that one's last required node: after `CONFigure[:SCALar]:VOLTage[:DC]`, `:POWer[:DC]` is `CONFigure[:SCALar]:POWer[:DC]`.
- `<function>` in a header is `CURRent[:DC]`, `VOLTage[:DC]`, `POWer[:DC]`, `ENERgy` or `TEMPerature`.

`tests/firmware/test_scpi_doc_matches_table.py` reads the column by these rules and holds it to the table, header for header, with each one's flags and how many parameters it takes.

### IEEE 488.2 common commands

| Command | Flags | Does |
|---|---|---|
| `*IDN?` | | `KLEIDOS,KLEIDOS-METER,<serial>,<firmware version>`. The serial (0.4.0 and later) is `KM-` and ten hex digits, a digest of the factory eFuse MAC (`firmware/src/identity.h`): the instrument's name in the calibration registry (`calibration/README.md`), the same after a full flash erase. `0` when the eFuse cannot be read, and before 0.4.0 |
| `*RST` | | closes any window without storing it, profile `TRACE`, range index 0, the I2C clock at the pin map's `scl_hz` (400 kHz by default; before 0.18.0 always 400 kHz), stream format `TEXT`, function `CURR`, default channels, `SAMPle:COUNt 100`, `SENSe:CAPTure DATA`, the current threshold `OFF` and its hysteresis 0 (0.10.0), `TRIGger:SOURce IMMediate`, `TRIGger:LEVel 0`, `TRIGger:SLOPe POSitive`, `TRIGger:PRETrigger 0`, `SENSe:CAPTure:LIMit OFF`, threshold marks off (0.11.0), relays to rest (make-before-break: K1 recloses before K2 opens; the spares are not wired from 0.13.0, and a pending `ROUTe:LEAD:CYCLe` is cancelled). Calibration is untouched, and so are the pin map and its staging (0.18.0: the map is the wiring, not a setting) |
| `*CLS` | | clears the error queue, the ESR and both event registers |
| `*ESE <n>` / `*ESE?` | | event enable, 0..255 |
| `*ESR?` | | reads and clears the Standard Event register |
| `*SRE <n>` / `*SRE?` | | service request enable, 0..255 |
| `*STB?` | | the Status Byte |
| `*OPC` / `*OPC?` | | `*OPC` sets bit 0 of the ESR when the operation it waits for completes: a window that ends by itself (a ring filling, a triggered ring's post-trigger samples, a capture `SENSe:CAPTure:LIMit` bounds, a null window) when one is running, from 0.11.0, and at once otherwise; `*CLS` and `*RST` forget a pending one. `*OPC?` answers `1` at once (below, "Deviations") |
| `*WAI` | | accepted, and immediate: a blocking wait could not be interrupted by `ABORt` over this line |
| `*TST?` | R C | a self-test, `0` when every part passes; otherwise the sum of 1 for the INA228 (it did not come up), 2 for the sampling engine, 4 for the relay inputs, and from 0.12.0 8 for the INA228's identity (`MANUFACTURER_ID`, `DEVICE_ID`) or `SHUNT_CAL` reading back other than at bring-up, 16 for its trim memory (`MEMSTAT` clear), 32 for the `ALE` wire (STATE's pacing probe, 8 conversions with a `DIAG_ALRT` read after each, counted other than 7 to 9 edges), 64 for a relay pad that does not read back its level, 128 for a stored calibration the flash does not give back (from FW-16: a read of the calibration record log that fails, or a whole record the instrument refuses that is not older than the one it holds; none stored is a pass), 256 for a task's stack that has kept fewer than 512 bytes free. It reads the device, so it is refused while a window is open (`-221`, `112`), and it leaves the INA228 converting with STATE's word, which every capture replaces with its own. Before 0.12.0 it reported only the first three bits, from the bring-up |

### SYSTem

| Command | Flags | Does |
|---|---|---|
| `SYSTem:ERRor[:NEXT]?` | | the oldest error |
| `SYSTem:ERRor:ALL?` | | every error, oldest first |
| `SYSTem:ERRor:COUNt?` | | how many are queued |
| `SYSTem:VERSion?` | | `1999.0` |
| `SYSTem:INFormation?` | | report: `model fw chip addr mfg die rev shunt_uohm cal_tag q relays bypass_part serial image elf pins_src pins_table`. `bypass_part` is which part the image drives as the bypass (`docs/relay2-click-wiring.md`): `relay2click` from 0.8.0, which has one image; before it `hw316` for the `meter` image and `relay2click` for `meter_relay2`; `serial` is `*IDN?`'s; `image` is the PlatformIO environment the image was built as (0.4.0 and later): `meter` from 0.8.0, and before it `meter` (the HW-316 bypass) or `meter_relay2` (the Relay 2 Click); `elf` is the first 8 bytes of the running image's ELF SHA-256 as 16 lower-case hex digits, the digest esptool writes into the image's description, so that which build is on the bench is read from the instrument and compared with the build's `firmware.elf` (`shasum -a 256`), `-` when the image has none (0.5.0 and later). From 0.18.0 `pins_src` and `pins_table` are `SYSTem:PINS?`'s `src` and `table` ([The pin map](#the-pin-map)). The registry checks `image`, `bypass_part`, `mfg` and `die` against the instrument's configuration |
| `SYSTem:HEALth?` | | report (0.12.0): `reset uptime_us elf fw stack_console stack_sampler allocs_after_init frees_after_init hold_released errors reset_released pins_src pins_table`: why the chip last reset (`poweron`, `external`, `software`, `panic`, `int_wdt`, `task_wdt`, `wdt`, `brownout`, `usb`, ... as ESP-IDF's `esp_reset_reason()` names it; `unknown` otherwise), microseconds since boot, the image's ELF hash and version, the fewest bytes each task's stack has kept free (`stack_console` is ESP-IDF's main task's from FW-17, the task the console runs on, 8,192 bytes, which also held the boot), `DIAGnostic:HEAP?`'s two counts, the releases the hold made, the errors queued, and from 0.16.0 `reset_released`, the coils the last reset found energized and released (`k1,k2`, or `-`: none, or a power-on; error `116`), and from 0.18.0 the pin map's source and the board table's digest, as `SYSTem:INFormation?` names them. Reads no register: one query that tells a healthy instrument from one that rebooted, runs another image or is short of stack |
| `SYSTem:STATus?` | | report: `running sink profile period_us pace pace_fallback range samples dropped read_err i_na vbus_uv die_mc die_age_us flags scl_hz q trig`. Reads no register, so it is safe under any capture |
| `SYSTem:COMMunicate:I2C:FREQuency <hz>` | D R | the INA228 bus, 50000..1000000. `BURST` needs 1000000 |
| `SYSTem:COMMunicate:I2C:FREQuency?` | | |
| `SYSTem:LED?` | | report: the carrier's LED, held dark (`blanked pixels held` and the bring-up report) |
| `SYSTem:PINS?` | | report (0.18.0): the live pin map, `sda scl alert addr scl_hz pull_sda pull_scl pull_alert relay1 relay1_active relay2 relay2_active src verified table` ([The pin map](#the-pin-map)) |
| `SYSTem:PINS:PENDing?` | | report: the staged map, the same twelve keys, then `valid problem at`: `valid=1 problem=- at=-` for a map that keeps every rule, else `valid=0`, the first rule it breaks and the key it breaks it at |
| `SYSTem:PINS:STAGe SDA\|SCL\|ALERt\|RELay1\|RELay2,<gpio>\|NONE[,HIGH\|LOW\|UP\|OFF]` | | stages one role on a GPIO (0..63) or on none; the option is a relay's active level (`HIGH`, `LOW`) or a bus or alert pin's internal pull (`UP`, `OFF`), and a role given none keeps its own. `REL1` and `REL2` are the relays' short forms. Changes nothing on the board |
| `SYSTem:PINS:ADDRess AUTO\|<address>` | | stages the INA228's address: `AUTO` (a scan of the strap space at boot) or a number, 1..255 (`#H41` or `65`), which validate() then holds to `#H40..#H4F` |
| `SYSTem:PINS:CLOCk <hz>` | | stages the bus clock at boot, any 32-bit number, which validate() then holds to 50000..1000000 |
| `SYSTem:PINS:CLEar` | | the staged map becomes the live one again |
| `SYSTem:PINS:APPLy` | R C | makes the staged map the live one, proves the INA228 on it and saves it, or puts the last good map back with one `117` (below); `-221` with a relay off its rest or a staged map that breaks a rule |
| `SYSTem:PINS:DEFault` | R C | stages and applies the default map, with APPLy's checks; it needs no INA228 and is kept whatever its proof finds |
| `SYSTem:PINS:RESult?` | | report: the last apply since the boot, `result reason mfg die tst saved`; `result` is `none` (none has run), `ok`, `reverted` or `refused`, and `reason` what failed (below) |

`SYSTem:STATus?`'s `vbus_uv` is the bus voltage on the **source** side of the shunt; the stream's `v_mv` is the voltage the DUT sees, the same less the burden voltage (`docs/architecture.md` §3.6). `sink` is `idle`, `stream`, `ring` (a TRACE or BURST capture, finished or not, until `ABORt`), `null` or `stats` (a statistics-only capture, 0.10.0). 0.11.0 appends `trig`: `armed` while an open capture's level trigger waits, `fired` once it has, `-` otherwise.

#### The pin map

From 0.18.0 the instrument reports which GPIO each of its five roles takes, the INA228's SDA, SCL and ALERT, Relay 1 (the DUT's battery lead, GPIO7, active low) and Relay 2 (the shunt bypass, GPIO15, active high), and lets a host stage another map and have it judged. `SYSTem:PINS?` answers the live map: each role's GPIO, `addr` (`auto`, or the pinned address as `0x40`..`0x4F`), `scl_hz` (the bus clock at boot), the three pulls (`up`, `off`), each relay's active level (`high`, `low`), `src` (`default`, `stored` or `fallback`: where the map came from), `verified` (1 once the relays' wiring has passed a check on this map) and `table` (`0x` and the CRC-32 of the board's pin table, `firmware/board/pins.toml`, which `host/kleidos_meter/board_pins.json` carries too, so a host can tell whether its copy of the board is the instrument's).

Staging changes nothing on the board: `SYSTem:PINS:STAGe`, `:ADDRess` and `:CLOCk` each change one field of the staged map, which starts as the live one and becomes it again after `SYSTem:PINS:CLEar`; each refused unit queues one error and stages nothing. `SYSTem:PINS:PENDing?` reports the staged map and `pinmap::validate()`'s verdict on it (`firmware/src/pinmap.h`), the first problem met, in this order, role by role (`sda scl alert relay1 relay2`): `missing` (the role has no pin), `no_pin` (no GPIO of the chip), `reserved` (a strapping, flash or PSRAM, USB, UART0 or LED line), `too_high` (ALERT must be below GPIO32), `shared` (an earlier role holds the pin, named at the later one); then `address` at `addr` and `clock` at `scl_hz`. `SYST:PINS:STAG SDA,45` reads `valid=0 problem=reserved at=sda`, `SYST:PINS:STAG SDA,5` `valid=0 problem=shared at=scl`, `SYST:PINS:STAG ALER,39` `valid=0 problem=too_high at=alert`. The simulator and the panel hold their own ports of the rules to `firmware/test/native/golden/pinmap.vectors`, which the firmware's host tests print from validate().

`SYSTem:PINS:APPLy` makes the staged map the live one in one checked change. It is refused, and nothing moves, under a window that runs or is not closed (its flags), while a relay is energized or a null path held (`-221`, `a null path is held (path=bypass): ROUTe:NULLpath OFF first`, or `the relays are not at rest (path=both): put them back first` with any other path but `shunt`), and for a staged map validate() refuses (`-221`, `the staged map breaks a rule: reserved at sda (SYSTem:PINS:PENDing?)`). Otherwise, in order: the relays move, each new pin latched at its released level before it is driven and a pin a relay leaves parked toward the level that released it (the pull-up for an active-low relay, the pull-down for an active-high one, until the next boot); ALERT is rebound; the pins no role holds any longer are parked and the bus is opened on its new pins at the map's clock, and the INA228 brought up on it, which resets it: the range in force is put back, and the CHARGE accumulator starts a new window. The map is proved by that bring-up (an address answers, its ID registers, its reset values, its trim memory) and by `*TST?`'s parts 1, 2, 4, 8, 16, 32 (the ALERT wire) and 64 (the relay pads). A map that passes is saved, the record read back, and is the live one: `src=stored`, or `default` for the default map itself, which a reboot then says too. Any failure puts the last good map back the same way, with the bus clock that was running, proves it, and queues exactly one `117`; nothing is saved, the staged map stays as it was refused, and `SYSTem:PINS:RESult?` reads `result=reverted`. An apply takes 50 to 60 ms on the rig, a revert included (2026-11-25).

`SYSTem:PINS:RESult?` reports the last apply: `reason` is `-`, or `no_chip` (nothing answers on the map's bus), `chip` (what answers is not a sound INA228), `alert` (no edge reaches the ALERT pin), `pads` (a relay pad is not driven or does not read its level), `engine` (the sampling engine is not running), `save` (the flash did not take the record), `rest` (relays that moved took the DUT's current off the shunt at rest, below), `core` (the sampler's core refused its half of the move, so the bus did not move), and for a refusal `relays` or `invalid`; `mfg` and `die` are the ID registers as the bring-up read them, `-` when nothing answered; `tst` the self-test's bits on the map that was tried, 0 for a pass, `-` for a refusal; `saved` 1 when the map is the newest record. A refusal by the flags changes none of it.

`SYSTem:PINS:DEFault` stages and applies the default map. It is the way back from the console alone, which no map can take away (no role may take GPIO43 or GPIO44, the console's UART), so it is kept whatever its proof finds: with no INA228 anywhere it answers `result=ok reason=no_chip mfg=- die=- tst=1 saved=1`, and the next boot runs the defaults. Two things it does not hide: a record the flash does not take leaves the defaults live with one `-240` (`saved=0`: a reboot loads the stored map), and a move the sampler's core refuses is put back as APPLy's is (`core`).

**A relay pin has no proof but its pad**, which reads the level it is driven to whatever it is wired to. So after an apply that moved a relay, `ROUTe:STATe?` says `check=-` and `SYSTem:PINS?` `verified=0`, the rig's own pins included, until a `ROUTe:NULLpath ON` ends with `check=ok`, which needs a DUT that draws 2 mA or more; one that fails its check (`103`, `104`) puts `verified` back to 0. A boot reads `verified=1` for the default map's relays and 0 for any others.

**A wrong active level acts at rest.** A relay staged with the level that energizes its part (Relay 1 `HIGH` on the rig's active-low part, Relay 2 `LOW` on its active-high one) is driven to that level as its released one: Relay 1 then opens the DUT's battery lead and Relay 2 closes the shunt bypass at rest, with `path=shunt` reported, and a saved map does it again on every boot. So an apply that moves a relay reads the shunt before the move and again after it, 50 ms after the proof, and when the DUT drew 2 mA or more and the shunt kept no more than three quarters of its share (the bypass's part of it, or all of it) the map is put back with one `117` (`reason=rest`, the two readings in `vs_pv=`); the DUT is without its battery, or its shunt, for the moment the map was tried. Under the 2 mA floor nothing tells a wrong level from a right one: the apply is kept with `verified=0`, the DUT stays unfed (or bypassed) at rest and across reboots, and `SYSTem:PINS:DEFault` is the way back. `SYSTem:PINS:DEFault` itself is kept whatever this check finds, as it is kept whatever its proof finds.

Under the 2 mA floor `ROUTe:NULLpath ON` does not judge its steps either and opens K1 whether K2 closed or not, so a Relay 2 staged on a pin it is not wired to leaves the DUT without its battery for as long as the path stands (until `ROUTe:NULLpath OFF`, or the hold's 120 s); with such a DUT, only a Relay 2 on its own wire may take a null path (the bench's pin map case therefore asks for none on a moved Relay 2 under the floor). From 2 mA its first step must leave the shunt between a sixteenth and three quarters of its share: relays swapped onto each other's wires with matching levels rest released and pass the apply, and the null path's first step then opens the lead instead of closing the bypass, which leaves the shunt nothing and is refused with `103` (`the shunt lost all of the dut's current`), the path released make-before-break.

**At boot** the instrument reads the stored map before it drives a pin, releases the relays where it puts them, and brings the INA228 up on its bus. `src=default` is the default map (no stored record, or one this image does not take), `stored` the record's, and `fallback` the record's with the default bus in its own bus's place: a stored bus the INA228 does not answer on falls back to SDA on GPIO4, SCL on GPIO5, both pulls, the address scanned and 400 kHz, when no other role of the map holds those pins, with one error `117` and the record left as it was. When one does, nothing falls back: `src=stored`, `chip=none`, the same error in other words, and the console up. ALERT and the relays never fall back by themselves, since nothing tells a wrong pin from an absent part; a part that answers and is not a sound INA228 is reported as the boot always has (`-241`).

### STATus

| Command | Flags | Does |
|---|---|---|
| `STATus:OPERation[:EVENt]?` · `STATus:QUEStionable[:EVENt]?` | | reads and clears the register's event bits (above, "Status registers") |
| `STATus:OPERation:CONDition?` · `STATus:QUEStionable:CONDition?` | | the register as it stands now |
| `STATus:OPERation:ENABle <mask>` / `STATus:OPERation:ENABle?` | | the event bits that reach the Status Byte's operation summary, 0..32767 |
| `STATus:QUEStionable:ENABle <mask>` / `STATus:QUEStionable:ENABle?` | | the event bits that reach its questionable summary, 0..32767 |
| `STATus:OPERation:PTRansition <mask>` / `STATus:OPERation:PTRansition?` | | the condition bits whose rise sets the event bit, 0..32767 (0.12.0) |
| `STATus:OPERation:NTRansition <mask>` / `STATus:OPERation:NTRansition?` | | the condition bits whose fall sets it, 0..32767 (0.12.0) |
| `STATus:QUEStionable:PTRansition <mask>` / `STATus:QUEStionable:PTRansition?` | | as the OPERation register's, 0..32767 (0.12.0) |
| `STATus:QUEStionable:NTRansition <mask>` / `STATus:QUEStionable:NTRansition?` | | as the OPERation register's, 0..32767 (0.12.0) |
| `STATus:PRESet` | | clears both enable masks and sets both registers' filters to their defaults |

### SENSe, SAMPle, FORMat, TRIGger

| Command | Flags | Does |
|---|---|---|
| `SENSe:PROFile TRACe\|STATe\|SLEep\|BURSt` | D R C | selects the sampling profile (below); resets the channel choice to the profile's default |
| `SENSe:PROFile?` | | `TRACE`, `STATE`, `SLEEP` or `BURST` |
| `SENSe:PROFile:REPort?` | | report: `profile adc_config period_us vshct_us avg avg_state vbusct_us`; from 0.18.0 `avg_state` is the averaging override (`off`: the profile's own `ADC_CONFIG`, as above; `on`: `SENSe:AVERage` below), and `vbusct_us` the bus conversion time, 0 for a profile whose mode does not convert the bus |
| `SENSe:CURRent:RANGe[:UPPer] <amps>\|MIN\|MAX\|DEF` | D R C | the smallest range that holds the value: up to 0.4096 A is index 1, up to 1.6384 A index 0. `MIN` is index 1, `MAX` and `DEF` index 0 |
| `SENSe:CURRent:RANGe[:UPPer]?` | | the range's full scale in amperes at the nominal shunt |
| `SENSe:CURRent:RANGe:INDex 0\|1` | D R C | the INA228's `ADCRANGE` directly: 0 is ±163.84 mV, 1 is ±40.96 mV |
| `SENSe:CURRent:RANGe:INDex?` | | |
| `SENSe:CURRent:RANGe:REPort?` | | report: `range current_lsb_pa ceiling_ma rstacc discard_us cnvrf q`; the last three describe the last switch |
| `SENSe:CURRent:SHUNt <ohms>` | R | the measured shunt resistance of both ranges, 0.05..0.2 Ω, stored in flash (the calibration record log, from FW-16; NVS before); drops any value a range had of its own and marks the shunt measured. A working copy of the registry's calibration (`calibration/README.md`) |
| `SENSe:CURRent:SHUNt?` | | the common value, in ohms |
| `SENSe:CURRent:SHUNt:RANGe<n> <ohms>` | R | range `n`'s own shunt value (`n` is 0 or 1 and must be written: `-114` otherwise), 0.05..0.2 Ω, stored in flash; marks the shunt measured. A capture on that range converts with it, and its header's `shunt_uohm` says so |
| `SENSe:CURRent:SHUNt:RANGe<n>?` | | the value range `n` converts with: its own, or the common one |
| `SENSe:CURRent:APERture <seconds>` / `SENSe:CURRent:APERture?` | D R C | the live profile's shunt conversion time, seconds: the setting (0.18.0) is the averaging override's, one of the INA228's eight (0.00005, 0.000084, 0.00015, 0.00028, 0.00054, 0.001052, 0.002074, 0.00412) to the microsecond, `-224` otherwise; the query answers the time the profile converts with |
| `SENSe:VOLTage:APERture?` | | the bus conversion time |
| `SENSe:TEMPerature:APERture?` | | the die-temperature conversion time |
| `SENSe:AVERage:COUNt <n>` / `SENSe:AVERage:COUNt?` | D R C | the INA228's hardware averaging: the setting (0.18.0) is the averaging override's count, 1, 4, 16, 64, 128, 256, 512 or 1024, `-224` otherwise; the query answers the count the profile converts with |
| `SENSe:AVERage[:STATe] ON\|OFF` / `SENSe:AVERage[:STATe]?` | D R C | the averaging override of the live profile (0.18.0), a measurement setting, off by default, on every boot and after `*RST`: off is the profile's own `ADC_CONFIG`; on, its count and shunt conversion time are the two settings above and the period follows. The query answers `1` or `0`; `SENSe:PROFile:REPort?`'s and the capture header's `avg_state` say the same ("The averaging override" below) |
| `SENSe:FUNCtion:ON "CURR"[,"VOLT"][,"POW"][,"TEMP"]` | R | the channels `INITiate` streams (strings or bare words) |
| `SENSe:FUNCtion:ON?` | | the channels it will stream, e.g. `"CURR","VOLT"`; `""` for a ring profile |
| `SENSe:FUNCtion:ON:DEFault` | R | back to the profile's default channels |
| `SENSe:CHARge?` | D R C | report: the hardware charge accumulator, `raw nc_nom dur_us of shunt_uohm osc_tol_ppm` |
| `SENSe:CHARge:RESet` | D R C | clears the accumulators and starts a new window |
| `SENSe:CAPTure DATA\|STATistics` / `SENSe:CAPTure?` | R | what `INITiate` keeps (0.10.0): `DATA` (the default) streams or fills the ring as the profile does; `STATistics` keeps the statistics only, on any profile for any duration: no stream line, no ring sample, no line budget, and `FETCh:STATistics?` at `ABORt`. The query answers `DATA` or `STAT` |
| `SENSe:CURRent:THReshold <amps>\|OFF` / `SENSe:CURRent:THReshold?` | R | a current level the next captures count against (0.10.0), -1.6384..1.6384 A, or `OFF` (the default); the query answers the level in amperes or `OFF` |
| `SENSe:CURRent:THReshold:HYSTeresis <amps>` / `:HYSTeresis?` | R | how far below the level the count falls back, 0..1.6384 A, default 0 (0.10.0). The count rises when a sample reaches the level and falls when one is below the level less the hysteresis |
| `SAMPle:TIMer?` | | the profile's sample period, seconds |
| `SAMPle:COUNt <n>` / `SAMPle:COUNt?` | R | samples one `MEASure`/`READ`/`CALibration:SHUNt` acquisition takes, 1..1000000, default 100. The acquisition itself is refused (`-222`) when the count times the profile's period exceeds 600 s (0.6.0 and later) |
| `FORMat[:DATA] TEXT\|CSV` / `FORMat[:DATA]?` | R | the stream record format |
| `TRIGger:SOURce IMMediate\|LEVel` / `TRIGger:SOURce?` | R | what starts a ring capture's record (0.11.0; `IMMediate` was the only source before): `IMMediate`, `INITiate` itself, the ring filling once from the start; `LEVel`, the current crossing `TRIGger:LEVel`, the ring running circular from `INITiate` until it does (below). A level needs a ring profile (`TRACE`, `BURST`) and `SENSe:CAPTure DATA`: `INITiate` is refused otherwise (`-221`). `MEASure`, `READ` and `CALibration` take no trigger. The query answers `IMM` or `LEV` |
| `TRIGger:LEVel <amps>` / `TRIGger:LEVel?` | R | the level, -1.6384..1.6384 A, default 0 (0.11.0) |
| `TRIGger:SLOPe POSitive\|NEGative` / `TRIGger:SLOPe?` | R | upwards or downwards through the level, default `POSitive`; the query answers `POS` or `NEG` (0.11.0). The current must first be on the other side of the level: one already beyond it at `INITiate` fires nothing until it has come back |
| `TRIGger:PRETrigger <samples>` / `TRIGger:PRETrigger?` | R | samples kept from before the crossing, 0 to the ring's size less one (`TRACe:POINts:MAXimum?`), default 0 (0.11.0). The ring then records its size less these after the crossing, and the capture ends by itself. A crossing among the capture's first `<samples>` keeps only the samples taken before it: the trigger's place in the dump is then `trig_n - first`, below `<samples>` |
| `SENSe:CAPTure:LIMit ON\|OFF` / `SENSe:CAPTure:LIMit?` | R | `ON`: `INITiate` ends the capture by itself after `SAMPle:COUNt` samples, on a stream or a statistics-only capture (a ring ends at its own size), and `*OPC` waits for it (0.11.0). It then reads `running=0` until `ABORt` collects it. Default `OFF`; the query answers `1` or `0` |
| `SENSe:CURRent:THReshold:MARK ON\|OFF` / `:MARK?` | R | `ON`: a streamed capture writes a mark record each time the current rises through the threshold (`thr_rise`) and falls below it less its hysteresis (`thr_fall`), behind the sample it was seen at, at most 1,024 a capture (0.11.0). Default `OFF` |

Each profile is a measured combination of averaging and conversion times (`docs/architecture.md` §3.2), and that is what it converts with unless its averaging override is on.

| Profile | Job | `ADC_CONFIG` | Period | Sink |
|---|---|---|---|---|
| `BURST` | resolve a transient | `0xA000` | 100 µs | ring, shunt only; needs `SYSTem:COMMunicate:I2C:FREQuency 1000000` |
| `TRACE` | shape a burst | `0xA0C0` | 280 µs | ring, shunt only (1,048,576 samples) |
| `STATE` | measure a state | `0xF140` | 1152 µs | stream, bus + shunt + temperature |
| `SLEEP` | measure deep sleep | `0xF1C2` | 67.52 ms | stream, AVG 16 |

**The averaging override** (0.18.0). `SENSe:AVERage` gives the live profile another averaging count and shunt conversion time, in place of its own two and nothing else: the mode and the bus and die conversion times stay, and the `ADC_CONFIG` word and the period follow, `(bus + shunt + die conversion times the mode converts) × count` microseconds a sample. It **changes the measurement** and is off by default, on every boot and after `*RST`; nothing of it is stored. Each profile has its own. A count or a time set while it is off is kept and converts nothing until `SENSe:AVERage ON`; `*RST` sets both back to the profile's own. `SENSe:PROFile:REPort?`, `SAMPle:TIMer?`, the apertures, `SENSe:AVERage:COUNt?` and a null's or an acquisition's sample count follow it at once; a capture runs on what it was when `INITiate` started it, and its header, its statistics and its ring's dump say that one whatever is set afterwards.

A setting is refused, `-221`, and changes nothing, when:

| Refused | Because | The text names |
|---|---|---|
| any of the three on `BURST`, `OFF` included | its 50 µs conversion is its definition and its loop already takes every second one | `BURSt has no averaging override: its 50 us conversion is its definition` |
| an override that is on, with a period no longer than twice the bus time a sample's reads take: 1051 µs on `STATE` and `SLEEP` (four registers a sample) and 191 µs on `TRACE` at 400 kHz, 421 µs and 77 µs at 1 MHz | the pacing has to serve a sample before the next conversion ends | `averaging <n> x <t> us gives <p> us a sample, under the <limit> us its reads need at <hz> Hz` |
| the same on `TRACE`, under 100 µs whatever the clock | the shortest period the timer-paced loop has been shown to keep (`BURST`'s) | the same, with 100 |
| an override that is on, with a period over 200,000 µs | a stop (500 ms), the pacing probe's eight periods and the watchdog's check-in are all sized to a sample | `averaging <n> x <t> us gives <p> us a sample, over the 200000 us its pacing allows` |
| any of the three while a window is open | as any setting that shapes a capture | `capture running (ABORt first)` |

So the longest samples are 194.56 ms (`STATE`, `SLEEP`: 512 × 280 µs) and `SLEEP` goes to AVG 64 with a conversion of 2,074 µs or less. A combination is judged when the override is on or is turned on, and the period limit again at `INITiate`, because `SYSTem:COMMunicate:I2C:FREQuency` changes it: an override accepted at 1 MHz can be refused there at 400 kHz. `STATE` and `SLEEP` stay paced by the ALERT pin, one edge an average, and `TRACE` by the timer at the new period. `*TST?` probes ALERT with `STATE`'s own word whatever the override. The count leaves the mean alone and the conversion time does not: 50 µs reads 15 µA low under a 40 µA DUT, 84 µs 2 µA, and a null taken at another time does not correct it (`docs/architecture.md` §3.2).

### CONFigure, MEASure, READ, FETCh

The SCPI measurement functions. `CURRent`, `VOLTage`, `POWer` and `ENERgy` are the DUT's (the voltage after the shunt); `TEMPerature` is the INA228's die; `CHARge` the hardware accumulator over the last window.

| Command | Flags | Does |
|---|---|---|
| `CONFigure[:SCALar]:CURRent[:DC] [<range>]` | D R C | the function `READ?`/`FETCh?` report, the stream channel it implies, and the range when given |
| `CONFigure[:SCALar]:VOLTage[:DC]` · `:POWer[:DC]` · `:ENERgy` · `:TEMPerature` | D R | the same for the other functions. Voltage, power and energy need the bus converted: on a profile that does not (`TRACE`, `BURST`) they select `STATE`, from 0.12.0, as SCPI's `CONFigure` sets the instrument up for its function; a profile that converts it (`STATE`, `SLEEP`) is kept |
| `CONFigure?` | | the configured function: `"CURR"`, `"VOLT"`, `"POW"`, `"ENER"`, `"TEMP"` |
| `MEASure[:SCALar]:CURRent[:DC]? [<range>]` | D R | configures, takes `SAMPle:COUNt` samples on the current profile without streaming them, and answers their mean in amperes |
| `MEASure[:SCALar]:VOLTage[:DC]?` · `:POWer[:DC]?` · `:ENERgy?` · `:TEMPerature?` | D R | the same: volts, watts, joules over the window, degrees. They configure first, so after a boot or `*RST` (profile `TRACE`) `MEASure:VOLTage?` selects `STATE` and answers (0.12.0; it queued `-230`, "the acquisition converted no bus voltage", before) |
| `READ?` · `READ[:SCALar]:<function>?` | D R | one acquisition on the present configuration, then the function's figure; it configures nothing, so `READ:VOLTage?` on a ring profile still queues `-230` |
| `FETCh?` · `FETCh[:SCALar]:<function>?` · `FETCh[:SCALar]:CHARge?` | | the figure of the last acquisition or capture, without a new one |
| `FETCh:STATistics?` | | report: the last closed capture, `samples dropped read_err pace_fallback pace_err_ppm pace_unmet dur_us mean_na min_na max_na die_mc_end flags charge_raw_start charge_raw_end osc_tol_ppm q stale_edges sd_na max_n max_t_us vmin_uv vmax_uv pmin_uw pmax_uw mean_charge_na charge_us thr_na above_n above_us rises batches batch_samples trig_n trig_t_us thr_marks rebased hold_teardowns`. The keys from `sd_na` on are 0.10.0's, computed in the sampling loop from every sample taken, streamed or not: `sd_na` the sample standard deviation of the current (`-` below two samples); `max_n` and `max_t_us` the index and timestamp of the first sample at `max_na`; `vmin_uv vmax_uv pmin_uw pmax_uw` the DUT's lowest and highest voltage (microvolts) and power (microwatts), `-` on a profile that converts no bus; `mean_charge_na` the CHARGE pair's mean current, corrected for the measured shunt but not for the null (the INA228 integrates its own `CURRENT`, offset and all: it reads `offset_pv / shunt_uohm` above `mean_na`), over `charge_us`, the microseconds between its two boundary reads (a few milliseconds more than `dur_us`); `thr_na` the threshold the capture counted against (nanoamps), `above_n` the samples taken while above it, `above_us` that count times `period_us`, `rises` its rising crossings, all four `-` with the threshold `OFF`; `batches` and `batch_samples` how many complete batches of how many samples `FETCh:STATistics:BATCh?` holds. 0.11.0 appends `trig_n trig_t_us` (the sample index and timestamp a level trigger fired at, `-` with none) and `thr_marks` (threshold marks written), and from 0.14.0 `rebased`: how many times a timer-paced loop came more than a period late and its deadline was re-based to the arrival, rather than reading the same conversion back to back to catch up (a ring's implicit timestamps are then off until the next anchor; 0 on a capture that kept its deadlines). From 0.16.0 `hold_teardowns`: how many times the hold put the relays back to rest while the capture sampled ("ROUTe", the hold), each time with its moves marked (`hold:k1:closed`, `hold:k2:open`) and its error queued; 0 on a capture whose circuit only the host moved. `stale_edges` (0.9.0): edges of an alert-paced capture whose `DIAG_ALRT` read found no conversion ready; none takes a sample, so `samples / (samples + stale_edges)` is the fresh-sample ratio, and it is 0 on a timer-paced capture. `pace_err_ppm` is the achieved period against the nominal one: on `pace=alert` it is the INA228's oscillator, on `pace=timer` whether the loop kept its deadline. From 0.15.0 the achieved period is the span from the first sample to the last over the intervals between them; before, it was `dur_us / samples`, whose span starts at the capture's start, up to a period before the first sample, so it read that phase as an error of up to `1 / samples` (296 `SLEEP` samples read +14 ppm where their own intervals gave +102) |
| `FETCh:STATistics:BATCh?` | | report (0.10.0): `batches batch_samples means_na`, the last closed capture's batch means in nanoamps, comma-separated, oldest first (`-` with none). A batch starts as one sample and doubles whenever 64 are complete, so a capture of 32 samples or more has 32 to 63 of them, of equal size; a partial last batch is left out. From their spread a host estimates the effective sample size of the mean |
| `FETCh:HEADer?` | | report: the last started capture's facts, one unit (below) |

The current, the voltage, the power and the temperature are means over every sample the sampler took (from 0.10.0 the voltage, power and temperature too; before, over the samples that reached the console); the energy is the mean power times the capture's own `dur_us` (0.10.0; before, times the samples the console saw, so a dropped sample shortened it). The voltage, power and energy exist only on a profile that converts the bus (`STATE`, `SLEEP`): on a ring profile those queries queue `-230`. The charge is `charge_raw_end - charge_raw_start` in coulombs, corrected for the measured shunt; it integrates between the two boundary reads, which bracket the window by a few milliseconds, so on a short window it is the larger of the two. `FETCh` while a capture runs queues `-230` (ABORt first).

A measurement is refused on a circuit the shunt does not carry whole (`101`). An acquisition is bounded: `SAMPle:COUNt` times the profile's period may not exceed 600 s (`-222`, nothing started; a longer record is a capture), and it takes at most twice its nominal window plus a second; one that took fewer samples queues `-230` and answers nothing. While it runs the console reads only one thing: a program message whose first unit is `ABORt` ends it within a sample period, queues `-230` (`aborted after <n> of <count> samples`) and answers nothing; that `ABORt` unit is consumed, the rest of its message runs after, and every message that arrived meanwhile runs after in its order. The acquisition looks for it in the console's 1,024-byte inbox only: an `ABORt` sent behind more than that of other messages waits in the receive ring until the acquisition ends by itself. A sampler that does not acknowledge the stop queues `107` (0.6.0 and later).

### INITiate, ABORt

| Command | Flags | Does |
|---|---|---|
| `INITiate[:IMMediate]` | D R | starts a capture on the current profile, streaming `SENSe:FUNCtion:ON`'s channels (or capturing to the ring) |
| `INITiate:FORCe` | D R | the same on a bypassed shunt (`path=both` or `bypass`): every report on it says `q=invalid` |
| `ABORt` | D | closes the open window, the one way any window closes. A capture's samples still queued are written before the message's responses; its result is `FETCh:STATistics?`. A null window computes its null; its result is `CALibration:ZERO:RESult?`. With no window open it queues `111`. When the sampler task does not acknowledge the stop within 500 ms it queues `107` and closes the window with no result (0.6.0 and later). During a `MEASure`/`READ`/`CALibration:SHUNt` acquisition it ends the acquisition instead (above) |

### TRACe

| Command | Flags | Does |
|---|---|---|
| `TRACe:MARK "<label>"` | | marks the current sample index of the running capture: 1..15 printable characters, no space, comma, quote, `;` or `=`. The instrument answers with a mark record on the stream: on a streamed capture, from 0.11.0, behind every sample of a lower index (it travels the stream's queue; before, it could overtake samples still queued), and at once on a full queue or a ring capture. 16 per capture, the hold's own two apart (0.16.0) |
| `TRACe:DATA? [SCALed\|RAW][,<from>[,<count>]]` | R | the ring as a block: `SCALed` (default) one `METER t= ch=i_ua v=` row per sample, `RAW` sixteen raw 20-bit counts per `R` row, with the drift anchors `A`, the marks `K` and the trigger `T` (below). `<from>` is a place among the samples the ring holds, 0 the oldest; every index in the rows is the capture's own sample index, which is the same thing unless a level trigger let the ring run circular (0.11.0) |
| `TRACe:POINts?` | | samples in the ring |
| `TRACe:POINts:MAXimum?` | | the ring's capacity, 1048576; known from boot (0.7.0 and later: before, 0 until the first ring capture) |

### CALibration

| Command | Flags | Does |
|---|---|---|
| `CALibration:ZERO <seconds>` | D R | opens a null window of 1..600 s on the current range and profile, and returns. Refused on `path=both` (`101`), and from 0.15.0 under a pending `ROUTe:LEAD:CYCLe` (`-221`): a null window refuses every contact move, so it would refuse the cycle's reclose |
| `CALibration:ZERO:RESult?` | | report: while the window is open, `running=1 profile over_s n_expect samples <relay tokens>`; after `ABORt`, `running=0 range offset_pv sd_pv over_s die_mc n <relay tokens> stored` |
| `CALibration:TAG "<text>"` / `CALibration:TAG?` | R | an opaque calibration tag, at most 23 characters, stored in flash |
| `CALibration:DATE <year>,<month>,<day>` / `CALibration:DATE?` | R | the calibration's date, the host's (the instrument has no clock): a calendar date in 2026..2099 (`-222` otherwise), stored in flash, echoed, never compared with anything. The query answers `0,0,0` when none is stored |
| `CALibration:VBUSside SOURce\|UNKNown` / `CALibration:VBUSside?` | R | where the `VBUS` sense is wired; `SOURce` after the continuity check |
| `CALibration:DATA?` | | report: `shunt_uohm shunt_measured off_pv_r0 off_pv_r1 nulled_r0 nulled_r1 off_die_mc_r0 off_die_mc_r1 vbus_side cal_tag shunt_uohm_r0 shunt_uohm_r1 cal_date`. The last three (0.4.0 and later) are the value each range converts with and the date, `yyyy-mm-dd` or `-`. `kleidos-meter calibration verify` compares this report with the registry |
| `CALibration:SHUNt:REFerence <ohms>` / `CALibration:SHUNt:REFerence?` | R | the reference resistor of the shunt calibration, 1 to 4000 Ω; 100.0 at boot and after `*RST` (the VPG foil part); not stored |
| `CALibration:SHUNt[:MEASure]` | D R | one acquisition of `SAMPle:COUNt` samples on the current profile (STATe or SLEep: the bus channel must convert) with the reference in place of the DUT and `VBUS` on its top lead; the live range must hold a null of its own (else -221). Computes the shunt from the two drops (`calibration/README.md`); stores nothing |
| `CALibration:SHUNt:RESult?` | | report: `range r_nohm r_uohm dev_ppm i_na vsh_nv vref_uv ref_uohm n die_mc check`; `r_uohm` is what `SENSe:CURRent:SHUNt` would store; `check` is `ok`, `no_samples`, `reversed`, `reference_low`, `signal_low` or `out_of_window` (±2 % of 0.100 Ω). -230 before the first point |

A null closed with K1 commanded open that reads beyond 50 µV is not stored (`102`, `stored=0`): a lead that did not open would subtract the DUT's own current from every later reading. The relay tokens are the circuit the window was opened in.

### DIAGnostic

| Command | Flags | Does |
|---|---|---|
| `DIAGnostic:ALERt?` | D R C | report: `DIAG_ALRT`, `raw memstat mathof energyof chargeof cnvrf` |
| `DIAGnostic:I2C:SCAN?` | R | report: `expected found n`: every address of `0x40`…`0x4F` that acknowledged a probe. The probes run on the I2C interrupt's core (0.16.4; every transaction does from 0.16.5, `docs/architecture.md` §2.2); should that core refuse the call, nothing is probed and the report is `found=- n=0` |
| `DIAGnostic:REGister? <addr>` | D R C | report: one INA228 register, `addr bytes raw`; a reserved address is refused |
| `DIAGnostic:WATChdog?` | | report (0.14.0): `console sampler timeout_s panic stalls`: whether each task's watchdog subscription was accepted (1) or not (0), the watchdog's timeout, whether a starved task reboots the instrument (1 on every image from 0.14.0), and the stalls the console has reported since boot (`107`, above). From FW-11 the watchdog is the firmware's own on the timer ESP-IDF's task watchdog used (`docs/architecture.md` §5.1): a starved task resets the chip rather than panicking, with the same `reset=task_wdt`, and both subscriptions read 0 when the watchdog could not start |
| `DIAGnostic:WATChdog:TEST` | R C | command (0.14.0): the console stops checking in with the watchdog, and within its timeout the instrument resets (it panicked until FW-11): every relay releases to the measuring circuit and `SYSTem:HEALth?` then says `reset=task_wdt`. Nothing answers in between. Refused while a window is open (`-221`, `112`) |
| `DIAGnostic:HEAP?` | | report (0.7.0 and later): `armed allocs_after_init frees_after_init maint_allocs maint_frees free_internal min_free_internal largest_internal free_spiram pre_main_allocs pre_main_frees allocs_since_main frees_since_main`. The first four counts are every heap allocation and free the image made, whatever made it, since the steady state began: until FW-17 a quarter of a second after boot (`armed=0` until then), and from FW-17 at `app_main()`'s first line, so `armed` reads 1 whenever the firmware answers and `allocs_after_init` and `frees_after_init` read what `allocs_since_main` and `frees_since_main` read, the same window; until FW-16 a calibration save allocated inside ESP-IDF (NVS) and was counted apart as `maint_` (until FW-14 an I2C clock change did too), and from FW-16 nothing is maintenance: both `maint_` keys keep their places and read 0. A healthy instrument reports `allocs_after_init=0 frees_after_init=0` (`docs/misra.md`), and from FW-17 `allocs_since_main=0 frees_since_main=0` too. The next four are bytes: internal RAM free now, its lowest since boot, its largest free block, and PSRAM free (from FW-18 PSRAM is in the memory map only and no heap, so `free_spiram` reads 0; the TRACE ring is placed there at link time). The last four (0.17.0 and later) count from the first allocation of ESP-IDF's startup, split at `app_main()`'s first line: what ESP-IDF allocated and freed before it, and every allocation and free since, maintenance included (39, 4, 79 and 31 on 0.16.6's image once the boot has settled, and 76 and 31 since `app_main()` from FW-09, 75 and 30 when NVS has a page fewer to load, falling with each driver the firmware replaced, 23 and 8 from FW-14, and 31 and 4 before, 0 and 2 since from FW-16, the two frees being the idle task's of the main task, 0 and 0 since from FW-17, which keeps the main task as the console, and 16 and 4 before from FW-18, every one of them a row of `docs/misra.md` D1; `tests/bench/heap_expectations.toml` holds them and what each command family adds) |
| `DIAGnostic:HEAP:CENSus?` | | a census build's alone (0.17.0; the image built with `METER_HEAP_CENSUS` defined, `firmware/src/heap_monitor.h`; every other image answers `-113`): report `capacity frames events first`, the ring's slots, the call sites kept per event, the events recorded since the first allocation of ESP-IDF's startup, and the oldest still held |
| `DIAGnostic:HEAP:CENSus:EVENt? <n>` | | a census build's alone, as above: report `n op phase size caps block callers`, event `n`: `op` is `alloc` or `free`, `phase` `pre` (before `app_main()`) or `main`, `size` the bytes asked for and `caps` their capabilities in hex (both 0 for a free), `block` the block's address, and `callers` the call sites above the heap hook, innermost first, comma-separated (`-` with none). `-222` when the event is not held (not yet recorded, or overwritten); `scripts/heap_census.py` reads them all and names each site from the build's ELF |

### ROUTe: the relay module

K1 is the DUT's battery lead, in series with the shunt (energized: open); K2 the bypass across it (energized: closed); K3 and K4 are spare and, on the rig since 2026-11-20, not wired: from 0.13.0 the firmware never drives GPIO16 and GPIO17, and from 0.16.6 it parks both at boot as inputs with the internal pull-down (`docs/architecture.md` §9), so neither floats; their tokens read `k3=spare:unwired k4=spare:unwired`, and their pads are not checked (`docs/rig-wiring.md` §8).

| Command | Does |
|---|---|
| `ROUTe:LEAD OPEN\|CLOSe[,FORCe][,LIVE]` | K1. An explicit order cancels a pending lead cycle |
| `ROUTe:LEAD:CYCLe <ms>,FORCe[,LIVE]` | K1 opened, held `<ms>` (1..60000) and reclosed by the instrument, from `path=shunt` only (`-221` otherwise), with `ROUTe:LEAD OPEN`'s interlocks: `FORCe` is required (the DUT loses its battery for the cycle, `109` without it) and `LIVE` under a capture, which marks both moves. The reclose runs on the console's loop, late by its own work only (at most a few milliseconds), and under a capture started meanwhile it is marked as a `LIVE` move rather than refused. `ROUTe:RESult?` describes the reclose once it has run (0.13.0). While a cycle is pending `CALibration:ZERO` is refused (0.15.0) |
| `ROUTe:BYPass ON\|OFF[,FORCe][,LIVE]` | K2 |
| `ROUTe:NULLpath ON\|OFF[,LIVE[,FORCe]]` | both, make-before-break: `ON` closes K2 then opens K1, reading the shunt before and after each step and releasing the path when a step was not seen to work (`103`, `104`); `OFF` recloses K1 then opens K2 |
| `ROUTe:RELay<n> ON\|OFF[,LIVE]` | the spares, `RELay3` and `RELay4`; `RELay1` and `RELay2` are refused by name (`-114`). Neither spare is wired on the rig: from 0.13.0 the order is refused (`-241`, "k3 is not wired on this rig") and nothing is driven; before, it answered and no relay moved |
| `ROUTe:STATe?` | report: `k1 k2 k3 k4 path check pads operate_settle_ms release_settle_ms hold_s hold_released bypass_part relay1_gpio relay1_active relay2_gpio relay2_active`; from 0.18.0 the last four are the live pin map's relay pins and the level that energizes each ([The pin map](#the-pin-map)); the two settle figures are the longest any channel takes (K1's, an HW-316 relay in every build); `hold_s` the seconds the hold has left (below), `hold_released` its releases since boot |
| `ROUTe:RESult?` | report: the circuit and the last completed order, `k1 k2 k3 k4 path check moved settle_us live marked vs_pv hold_s q` (`moved=-` after a refused order) |

The interlocks: an order that would move a contact while a capture runs needs `LIVE`, and is marked in the capture (each move a mark record); one that would take the shunt off a running capture's current, or leave the DUT with no supply (`path=none`), needs `FORCe` as well. Nothing moves under a null window (`112`).

**The hold** is the instrument's teardown for a host that went away with the relays off the measuring circuit. Every path but `shunt` is held 120 s (`hold_s`, the seconds left, `-` when nothing is held): armed by the order that left it, armed again by any relay order, one that moves nothing included (a host that keeps a path long sends its order again), and re-armed whenever a capture or a null window closes. A null window that is sampling suspends it, and the hold gets its whole time again once the window stops sampling, by itself or by `ABORt`; nothing else does. When it runs out the instrument puts every coil back to rest, make-before-break (K1 recloses before K2 opens), and queues the release: `113` from `path=bypass` (0.12.0), `114` from `path=none` (0.13.0), `115` from `path=both` (0.16.0). `ROUTe:STATe?` counts the releases in `hold_released`. Before 0.16.0 an open capture suspended the hold, so a host that died under a capture left the DUT without its battery, or the bypass closed, until somebody came. From 0.16.0 the release runs under it: each move is a mark record of the capture (`hold:k1:closed`, `hold:k2:open`), the error's detail says `under a capture, marked hold:`, the capture's statistics count it (`hold_teardowns`) and QUEStionable bit 10 is set for it; a capture that has ended by itself (a ring, a limit) is not marked, since no sample follows the move. A pending `ROUTe:LEAD:CYCLe` is part of the release (its reclose has then been done).

**A reset** releases every coil at once, as every boot does, which leaves the relays on the measuring circuit but can open K2 before K1 recloses. The commanded coils are kept in RTC memory as they are driven, so the boot after a reset that keeps that memory (a panic, a watchdog, `esp_restart()`, a brown-out) queues `116` with what it released and `SYSTem:HEALth?` says `reset_released` (0.16.0); the capture it fell in ends with the stream. `*RST` is an order, not a failure: it closes the open window and then releases the relays make-before-break, queuing nothing. `path` is `shunt` (K1 closed, K2 open), `bypass` (the null path), `both` (K2 across a closed K1) or `none` (the DUT disconnected).

**The refusals' details.** A relay order that is refused changes nothing and queues one error, `<code>,"<standard text>;<detail>"`. The details of the relay orders, and of the commands that the relays make refuse, as `firmware/src/console.cpp` and `console_commands.cpp` compose them (`tests/firmware/test_scpi_refusal_details.py` holds each to those sources):

```text
-224 use ROUTe:LEAD OPEN|CLOSe[,FORCe][,LIVE]
-224 use ROUTe:BYPass ON|OFF[,FORCe][,LIVE]
-224 use ROUTe:NULLpath ON|OFF[,LIVE[,FORCe]]
-224 use ROUTe:LEAD:CYCLe <ms>,FORCe[,LIVE] (1..60000 ms)
-222 use ROUTe:LEAD:CYCLe <ms>,FORCe[,LIVE] (1..60000 ms)
-221 a lead cycle starts from path=shunt (path=<path>)
-224 use ROUTe:RELay3|RELay4 ON|OFF[,LIVE]
-114 use ROUTe:RELay3|RELay4 ON|OFF[,LIVE]
-114 k1 is the lead (use ROUTe:LEAD OPEN|CLOSe)
-114 k2 is the bypass (use ROUTe:BYPass ON|OFF)
-241 <k3|k4> is not wired on this rig (docs/rig-wiring.md)
 109 order disconnects the dut (k1 open, k2 open: add FORCe)
-221 capture running (ABORt first, or add LIVE)
 112 null window running (ABORt first)
 110 order bypasses the shunt under a capture (add FORCe: the capture becomes q=invalid)
 103 bypass check failed (k2 took no current: vs_pv=<readings>; released to path=<path>)
 103 bypass check failed (the shunt lost all of the dut's current: vs_pv=<readings>; released to path=<path>)
 104 lead check failed (the shunt still conducts: vs_pv=<readings>; released to path=<path>)
-240 i2c <esp_err_name> (null-path check: nothing moved)
-240 i2c <esp_err_name> (null-path check: released to path=<path>)
 101 shunt bypassed (path=both: ROUTe:BYPass OFF, or INITiate:FORCe)
 101 shunt bypassed (path=bypass: ROUTe:NULLpath OFF, or INITiate:FORCe)
 101 shunt bypassed (path=both: ROUTe:BYPass OFF or ROUTe:NULLpath ON first)
 101 shunt bypassed (path=<path>: ROUTe:NULLpath OFF first)
-221 a lead cycle is pending (wait for its reclose, or ROUTe:LEAD CLOSe)
 102 null not stored (a relay null beyond 50 uV: k1 may not have opened; the stored null stands)
 100 relays unavailable (gpio config failed at boot)
```

A `use …` detail comes with the code of what was wrong with the parameters: `-224` for a word the order does not take, `-109` for one that is missing. `<readings>` are the shunt's readings in picovolts, comma-separated, as `vs_pv=` has them in `ROUTe:RESult?`, and the `<path>` a failed check releases to is `shunt`. Of the four `101` details the first two are `INITiate`'s, the third `CALibration:ZERO`'s and the last an acquisition's (`MEASure`, `READ`, `CALibration:SHUNt`); the pending lead cycle is `CALibration:ZERO`'s too; `102` closes a null window whose offset is beyond 50 µV with K1 commanded open; and `100` is queued at boot, and answers every `ROUTe` order and query, when the relay pins could not be configured.

## Captures and the stream

A capture is `INITiate`, whatever the host asks meanwhile, and `ABORt`: the samples and the marks are records between the responses, and the one that answers a message is the first line that is not a record. [A worked session](#a-worked-session) shows one, with a verdict on every message.

### Stream records

| Format | Record | Line |
|---|---|---|
| `TEXT` (default) | a sample, one line per channel | `METER t=<us> ch=<i_ua\|v_mv\|p_mw\|die_c> v=<value>` |
| `TEXT` | a mark | `METER_MARK mark=<label> n=<sample index> t=<us>` |
| `CSV` | a sample, one line | `S,<t_us>,<i_ua>,<v_mv>,<p_mw>,<die_c>` |
| `CSV` | a mark | `K,<sample index>,<t_us>,<label>` |

A threshold mark (`SENSe:CURRent:THReshold:MARK ON`, 0.11.0) is a mark record labeled `thr_rise` or `thr_fall`, with the index and timestamp of the sample the crossing was seen at, written right after that sample.

A hold mark (0.16.0) is a mark record labeled `hold:k1:closed` or `hold:k2:open`: a relay the hold moved by itself under a running capture, at the sample index it was driven at, as a `LIVE` move is marked (a ring capture keeps it as a `K` row). The hold's release from `path=bypass` writes both, K1 first; from `path=none` the first, from `path=both` the second. Two slots beyond `TRACe:MARK`'s 16 are kept for them, so a capture whose table is full still records the move; the capture's `hold_teardowns=` counts the releases either way.

`t` is microseconds since the instrument booted (esp_timer), the sample's own timestamp: on an alert-paced capture (`pace=alert`, 0.9.0 and later) the moment the conversion-ready edge woke the sampler, which is the conversion's end; on a timer-paced one the moment its read finished. Values are in thousandths of their unit as the device computed them: `i_ua` microamps, `v_mv` millivolts at the DUT, `p_mw` milliwatts delivered, `die_c` degrees. A channel not chosen, or not converted by the profile, is left out (`TEXT`) or empty (`CSV`); it is never written as a zero. The `TEXT` sample line is the one the console protocol before SCPI wrote, so recorded `meter.log` files and new ones read the same.

The stream fits the line by construction: every channel at once, on the fastest streaming profile (`STATE`), needs 167 kB/s of the 480 kB/s that 80 % of the 6 Mbps line carries, and the firmware does not build with a profile whose stream would need more, so no channel set is refused for its byte rate (until FW-27, `INITiate` checked it at run time and answered `-221`, `stream too fast`, which no profile could reach). The byte budget is derived from the `TEXT` line and the `CSV` row is never longer. A sample the console could not write is counted in `dropped=`.

### The capture header

`FETCh:HEADer?` is one report unit; its keys, in order: `fw chip addr mfg die rev` (identity), `config adc_config shunt_cal range` (the registers written), `diag_cfg alatch sovl suvl` (the clip detector: with `alatch=1` and these limits, a conversion at either end of the field sets `SHNTOL`/`SHNTUL` in `flags`), `mode vbusct_us vshct_us vtct_us avg period_us` (0 for an input the mode does not convert), `pace pace_edges pace_expect osc_tol_ppm` (what paces the samples: from 0.9.0 `STATE` and `SLEEP` probe the `ALE` pin for `pace_expect=8` conversions, reading `DIAG_ALRT` after each edge, and run `pace=alert` when 7 to 9 edges came, `pace=timer` otherwise; `TRACE` and `BURST` are always `pace=timer pace_edges=0 pace_expect=0`, no probe), `shunt_lsb_pv bus_lsb_pv temp_lsb_nc current_lsb_pa power_lsb_pw charge_lsb_pc charge_raw_start`, `shunt_uohm shunt_measured offset_pv off_die_mc vbus_side`, `cal_tag q die_mc_start vs_cur_delta`, `scl_hz sink ring anchor_every t0_us format` (`sink` is `stats` for a statistics-only capture, with `ring=0`, 0.10.0), `relays` and the relay tokens of the circuit at the start, then (0.11.0) `trig_source` (`imm` or `level`), `trig_level_na` and `trig_slope` (`pos`, `neg`; both `-` with `imm`), `trig_pre` and `limit` (the samples the capture ends itself after, 0 for none), and (0.18.0) `avg_state`, the averaging override the capture ran with (`off`, `on`), and `pins_src`, where the pin map it ran on came from (`SYSTem:PINS?`'s `src`).

### The ring dump

`TRACe:DATA?` rows, inside the block:

```text
METER_DUMP count=<n> t0_us=<us> period_us=<us> fmt=raw20 [first=<i>] [trig_n=<i>]        (RAW)
METER_DUMP count=<n> t0_us=<us> period_us=<us> ring=<capacity> [first=<i>] [trig_n=<i>]  (SCALed)
A n=<index> t=<us>                 a drift anchor, every 1024 samples
K n=<index> label=<label>          a mark
T n=<index> t=<us>                 the sample a level trigger fired at (0.11.0)
R n=<first index> c=<c>,<c>,...    sixteen raw 20-bit counts (RAW)
METER t=<us> ch=i_ua v=<value>     one scaled sample (SCALed)
METER_DUMP_END crc32=<8 hex> bytes=<n>
```

A ring sample's timestamp is implicit: `t0_us + index × period_us`, corrected by interpolating between the anchors, which carry real timestamps. Every anchor the ring holds below `<from>` is written first, so a partial dump keeps its drift reference. A raw count times the header's `current_lsb_pa` is the current at the nominal shunt before the null.

From 0.11.0 the indices are the capture's sample indices. Under a level trigger the ring runs circular, so the oldest sample it holds is index `first=` (written on the opening row whenever it is not 0), and `trig_n=` and the `T` row say where the trigger fired; the implicit timestamps hold across the wrap, because the index is the capture's and not the ring's. An untriggered dump reads as it always did, but for its closing row: `METER_DUMP_END` carries `crc32=`, the IEEE CRC-32 (`zlib.crc32`) of the block's first `bytes=` bytes, every row before it, so that a dump damaged on the line, or a filed one, is told from a good one (`check_dump()` in the reference client, which `Meter.dump()` applies). Before 0.11.0 the row was the bare `METER_DUMP_END`.

## Deviations from SCPI-1999

The command structure, the common commands, the error queue and its numbering, the status model, the numeric and string data and the arbitrary block follow SCPI-1999 and IEEE 488.2. Where the instrument departs from them, on purpose:

- **Report responses** are `key=value` tokens separated by spaces, not comma-separated values. Every report carries more facts than a positional list can keep readable or extensible, and a host reads a key without counting commas; a new key never breaks an old client.
- **Stream records** are written while a capture runs, outside any query (SCPI instruments buffer and are read with `FETCh`). A deep-sleep capture lasts minutes and a `STATE` one fills hundreds of kilobytes, so the samples are streamed as they are taken and the host stores them; `FETCh` still gives the figures of the window.
- **`ABORt` keeps the data**: it is the normal end of a capture, and `FETCh` reads the closed window.
- **`ABORt` with nothing open is an error** (`111`), so a host can tell a window that had already closed from one it just closed.
- **Device-specific report queries** (`SYSTem:INFormation?`, `SYSTem:STATus?`, `SENSe:PROFile:REPort?`, `SENSe:CURRent:RANGe:REPort?`, `ROUTe:RESult?`, `CALibration:ZERO:RESult?`) carry what the old console printed after each action, since a SCPI command answers nothing.
- **`INITiate:FORCe`** overrides an interlock; SCPI has no word for it.
- **The averaging and conversion times** are the profile's, and from 0.18.0 the averaging count and the shunt conversion time are settable per profile through `SENSe:AVERage` alone; the bus and die conversion times are read-only.
- **No service request line**: over a serial port `*SRE` only shapes `*STB?`'s master summary bit.
- **`*OPC?` and `*WAI` do not block.** A blocking query could not be interrupted by `ABORt` over a line with no device clear, so `*OPC?` answers `1` at once and `*WAI` returns at once; `*OPC` is the one that waits, by setting bit 0 of the ESR when a window that ends by itself has ended (0.11.0), and a host polls `*ESR?`.

## Old console command to SCPI

The console protocol of firmware 0.1.0 (`key=value` lines with `METER_OK:` / `METER_ERR:` prefixes) maps to the SCPI set as follows. Every capability it had is kept; nothing it printed is lost, though some facts now come from a query beside the command instead of from the command's own acknowledgement.

| Old command | SCPI | Notes |
|---|---|---|
| `IDN?` | `*IDN?` and `SYSTem:INFormation?` | `*IDN?` is the standard four fields; `SYSTem:INFormation?` carries the old fields, `model=kleidos-meter` in place of the bare word |
| `METER?` | `SYSTem:STATus?` | the same fields, without the leading word |
| `PROFILE TRACE\|STATE\|SLEEP\|BURST` | `SENSe:PROFile <name>` + `SENSe:PROFile:REPort?` | the acknowledgement's fields are the report |
| `RANGE 0\|1` | `SENSe:CURRent:RANGe:INDex 0\|1` + `SENSe:CURRent:RANGe:REPort?` | or `SENSe:CURRent:RANGe <amps>` |
| `BUS <hz>` | `SYSTem:COMMunicate:I2C:FREQuency <hz>` | |
| `START [ch,...]` | `SENSe:FUNCtion:ON "CURR",...` + `INITiate` + `FETCh:HEADer?` | channel `i_ua` is `CURR`, `v_mv` `VOLT`, `p_mw` `POW`, `die_c` `TEMP`; the `METER_HDR` block is one report unit |
| `START FORCE` | `INITiate:FORCe` | |
| `STOP` (capture) | `ABORt` + `FETCh:STATistics?` | the `METER_OK: stopped` fields, without the word |
| `STOP` (null window) | `ABORt` + `CALibration:ZERO:RESult?` | the result line's fields plus `running=0 stored=`; the refusals that followed it (`null not stored`, `calibration write failed`) are errors 102 and 106 in the same answer |
| `MARK <label>` | `TRACe:MARK "<label>"` | the acknowledgement is the `METER_MARK` stream record |
| `DUMP [RAW] [from] [count]` | `TRACe:DATA? [SCALed\|RAW][,from[,count]]` | the same rows, inside a definite-length block |
| `ZERO <s>` | `CALibration:ZERO <s>` + `CALibration:ZERO:RESult?` | the acknowledgement's fields are the running report |
| `CAL SHUNT <uohm>` | `SENSe:CURRent:SHUNt <ohms>` | ohms now: `100012` µΩ is `0.100012` |
| `CAL TAG <text>` | `CALibration:TAG "<text>"` | |
| `CAL VBUSSIDE SOURCE\|UNKNOWN` | `CALibration:VBUSside SOURce\|UNKNown` | |
| `CAL?` | `CALibration:DATA?` | |
| `DIAG?` | `DIAGnostic:ALERt?` | |
| `SCAN?` | `DIAGnostic:I2C:SCAN?` | |
| `REGR <addr>` | `DIAGnostic:REGister? <addr>` | `0x04` is written `#H04` |
| `CHARGE?` | `SENSe:CHARge?` | |
| `CHARGE RESET` | `SENSe:CHARge:RESet` | |
| `LED?` | `SYSTem:LED?` | carries the boot report too |
| `RELAY?` | `ROUTe:STATe?` | |
| `RELAY 3\|4 ON\|OFF [LIVE]` | `ROUTe:RELay3\|RELay4 ON\|OFF[,LIVE]` | |
| `LEAD OPEN\|CLOSE [FORCE] [LIVE]` | `ROUTe:LEAD OPEN\|CLOSe[,FORCe][,LIVE]` + `ROUTe:RESult?` | |
| `BYPASS ON\|OFF [FORCE] [LIVE]` | `ROUTe:BYPass ON\|OFF[,FORCe][,LIVE]` + `ROUTe:RESult?` | |
| `NULLPATH ON\|OFF [LIVE [FORCE]]` | `ROUTe:NULLpath ON\|OFF[,LIVE[,FORCe]]` + `ROUTe:RESult?` | |
| `METER_ERR: <reason>` | an error in the queue, `<code>,"<text>;<reason>"` | read with `SYSTem:ERRor:ALL?`; every unit after a refused one still runs |
| boot lines `IDN …`, `LED …`, `METER_ERR: …` | `*ESR?` bit 7 and the error queue | nothing is printed at boot |
| `METER t= ch= v=` stream line | unchanged (`FORMat:DATA TEXT`) | `CSV` added |
| `METER_OK: mark=…` printed for a `LIVE` relay move | `METER_MARK mark=… n=… t=…` | |
| — | `MEASure`, `READ`, `FETCh`, `CONFigure` for current, voltage, power, energy, temperature; `SAMPle:COUNt`, `SAMPle:TIMer?`; the apertures and averaging; `*RST`, `*TST?`; the status registers; `FORMat:DATA CSV`; `TRACe:POINts?` | new |
