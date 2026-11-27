<!-- SPDX-FileCopyrightText: 2026 José M. Requena Plens -->
<!-- SPDX-License-Identifier: GPL-3.0-or-later -->

# Relay operate and release times, measured

The settle waits in `firmware/src/relay.h` are datasheet bounds plus allowances no source gives (the flyback diode's, the bounce margin). This page files what the rig itself measured against them, and how, so that a wait changes only from a record (`docs/architecture.md` §6.1). The record of 2026-11-21 resolved every move, each well inside its wait.

## How

`kleidos-meter relay-timing --relay k1 --relay k2 --repeats 8 --hold 300` (`host/kleidos_meter/relay_timing.py`, 0.13.0) runs one `TRACE` ring capture (280 µs a sample) per relay and moves the relay under it with `LIVE`: `ROUTe:LEAD OPEN,FORCe,LIVE` and `ROUTe:LEAD CLOSe,LIVE` for K1, `ROUTe:BYPass ON,FORCe,LIVE` and `OFF,LIVE` for K2, each held 300 ms. Every move is a mark at the sample index right after its pin is driven (`Console::executeRelayOrder()` drives, then marks). The tool dumps the ring and, from 20 samples before each mark to the next mark of the relay, finds the least-squares split of the raw counts into two levels: the delay from the mark to the split is the contact's time, at the ring's 280 µs resolution, and the step over the samples' pooled standard deviation (`contrast`) says how clearly the split stands out of the noise. The window ends no later than one hold after its mark, since the next order comes no sooner; before 2026-11-21 the last move's window, which has no next mark, ran 2,000 samples (560 ms) on instead, past its hold (below). A capture keeps 16 marks, `TRACe:MARK`'s and `LIVE` moves' together (`docs/scpi.md` error 105), and a `LIVE` move past them is still driven, unmarked: a move is two marks, so the tool refuses `--repeats` above 8.

## The record of 2026-11-20

Image 0.13.0 (ELF `601a5241d576df01`), the rig as wired (rev D), the T-Deck asleep behind K1 drawing about 38 µA, also on USB. Eight moves of each.

| Move | Median | Range | Weakest contrast | Reading |
|---|---|---|---|---|
| K1 released (`k1:closed`, the NC contact recloses) | 10.36 ms | 6.16 to 10.92 ms | 1.46 | the release, with the module's flyback diode across the coil; inside the 50 ms `kReleaseSettleMs` by a factor of 4.6 |
| K1 energized (`k1:open`, the NC contact opens) | 2.10 ms | 0.28 to 3.36 ms | 0.89 | below the datasheet's 10 ms operate bound and inside the 20 ms `kOperateSettleMs`; the step is weak (see below) |
| K2 on (`k2:closed`, the LCA717 SSR conducts) | 1.26 ms | −1.12 to 19.04 ms | 0.40 | not resolved |
| K2 off (`k2:open`) | 0.98 ms | −1.40 to 10.64 ms | 0.51 | not resolved |

**What limits it.** TRACE's per-sample noise on this rig is about 38 µA (sd), the same as the DUT's whole current, so K1's step is one sigma and K2's, which only shares the current with the shunt (about 60 % of it through the closed bypass), less than one: a split found at a contrast below about 1 is the noise's, and the K2 rows and the earliest K1 openings are such splits. The K1 release is the one figure the record supports as measured. The plan's condition was a DUT drawing a few milliamps; the temporary 100 Ω load of the meter-web plan's night test (NW-00: about 41 mA at the cell's 4.1 V, a step a thousand times the noise) was the chance to rerun it with a step that stands out: it was taken on 2026-11-21, and the load removed the same day (the record below resolves all four).

**The waits stay as they are.** Nothing measured here is above its bound, and the two figures resolved (the K1 release, about 10 ms against the 50 ms wait; the K1 operate, a few ms against 20) give make-before-break the margin it was sized for. They change only from a record that resolves every move.

## The record of 2026-11-21, with a 100 Ω load

Instrument `KM-0000000000` on 0.16.5 (ELF `e08ddf610af749ac`), the rig as wired (rev D) with the DUT replaced by the VPG 100 Ω reference (`calibration/README.md`), wired from pigtail 3 (the DUT's + node after K1's NC contact) to the INA228's `GND`, and the T-Deck unplugged. The meter read 40.78 mA at 4.095 V before the runs; in the rings the closed path reads about 12,950 raw counts, against a pooled spread of 34 to 173 counts on the openings. Two runs of `kleidos-meter relay-timing --relay k1 --relay k2 --repeats 16 --hold 300`, each measuring eight moves of each relay (below): run B's every edge from its `--json` output, run A's per-move summary beside it. Each figure is a whole number of the ring's 280 µs samples, so it is good to one sample.

| Move | Median | Range | Contrast | Reading |
|---|---|---|---|---|
| K1 energized (`k1:open`, the NC contact opens) | 2.24 ms | 1.96 to 2.52 ms | 75 to 383 | eight of eight, from about 12,950 counts to under 7; run A the same median and range, weakest contrast 74.76. Below the datasheet's 10 ms operate bound and inside the 20 ms `kOperateSettleMs` |
| K1 released (`k1:closed`, the NC contact recloses) | 11.20 ms | 11.20 ms, each of seven | 18.6 to 20.7 | above the bare coil's 10 ms release bound, as the module's flyback diode was expected to make it, and inside the 50 ms `kReleaseSettleMs` by a factor of 4.5; run A's median, 11.48 ms over its eight moves, puts at least three of its seven at 11.48 ms or more |
| K2 on (`k2:closed`, the LCA717 SSR conducts) | 0.84 ms | 0.56 to 0.84 ms | 68 to 126 | eight of eight; the shunt's counts fall from about 12,940 to 3,960, so the closed bypass carries 69.4 % of the current (69.0 to 69.6 %, against about 60 % under the T-Deck); run A 0.84 to 1.12 ms, median 0.84 ms, weakest contrast 72.01. Under IXYS's 3 ms bound and inside the 10 ms `kSsrOperateSettleMs` |
| K2 off (`k2:open`) | 0.84 ms | 0.56 to 0.84 ms | 85 to 151 | seven of seven, from about 4,000 counts back to 13,010; run A's median 0.70 ms over its eight puts at least four of its seven at 0.56 ms. Inside the 10 ms `kSsrReleaseSettleMs` |

**Eight moves of sixteen, and the last of each left out.** `--repeats 16` drove sixteen moves of each relay, but a capture keeps 16 marks and a move is two, so moves 9 to 16 ran unmarked (`Console::executeRelayOrder()` drives the pin whether or not a mark slot is left) and the tool measured the first eight. The tool then searched the last marked move over 2,000 samples, which reached the first unmarked order: K1's last reclose split 1,463 samples on (409,640 µs, contrast 5.98, from 12,511 counts to 1.4), where the next opening came 1,431 to 1,478 samples after every earlier reclose, and K2's last opening 1,266 samples on (354,480 µs, contrast 9.48), where the next closing came 1,250 to 1,282 samples after every earlier opening. Those two edges are the tool's, not the relays', and are left out of the table; run A's own (409,360 and 355,600 µs) are the maxima of its summary for those two rows, so its ranges there are not quoted. The tool now ends each window one hold after its mark and refuses more than eight repeats (`host/kleidos_meter/relay_timing.py`, `host/tests/test_relay_timing.py`).

**Why the release's contrast is lower.** On every reclose the samples the split leaves before it (the 20 before the mark and the 40 to the step) average 965 to 1,071 counts, where the open lead reads under 7: some of them already carry current, as a contact bouncing as it makes would show at 280 µs a sample (the ring cannot tell a bounce from a partial make). That spread puts the pooled standard deviation at 579 to 647 counts, against 34 to 173 on the openings, and the contrast at about 19, still far above the noise. The 11.20 ms is the time to the settled closure; the contact first passes current somewhat before it.

**All four resolved, and the waits stay.** Every edge of run B sits inside its wait: K1's operate 2.52 ms at most against 20 ms, its release 11.20 ms against 50 ms, and the SSR 0.84 ms at most each way against 10 ms; run A agrees wherever its summary can be read (K2 on at most 1.12 ms; its release and K2-off maxima are hidden by its last moves). The release is the wait make-before-break rests on (`ROUTe:NULLpath OFF` must not open K2 before K1's NC contact has reclosed), and it is about four times the time measured. A wait changes only from a record that asks for it, and this one does not.

## The lead cycle

`ROUTe:LEAD:CYCLe 200,FORCe,LIVE` under a `STATE` capture on the same image marked `k1:open` at sample 901 and `k1:closed` at sample 1092, 220.5 ms apart: that candidate timed the cycle from the end of the opening's 20 ms settle. The released 0.13.0 (ELF `57ee41d3627426df`, a clean build) times it from the drive. Under a `STATE` capture its two marks fell, by the instrument's clock:

| Cycle asked | Mark to mark | Error |
|---|---|---|
| 50 ms | 49.998 ms | −2 µs |
| 200 ms | 199.944 ms | −56 µs |
| 1,000 ms | 999.890 ms | −110 µs |

Each reclose lands within a millisecond, one FreeRTOS tick, of its deadline (the console's loop waits for input no longer than the time left), and a little early rather than late: the deadline is taken at the drive, and the opening's mark a few tens of microseconds after it. The relays ended at `path=shunt`, `DIAG:HEAP?` at `allocs_after_init=0 frees_after_init=0`.

**The open lead's hold**, on the candidate: `ROUTe:LEAD OPEN,FORCe` with no window open, `hold_s` counting down from 120, and K1 reclosed by the instrument 121.3 s later by the host's clock (`path=shunt`, `hold_released=1`) with error `114,"Lead reclosed;the hold reclosed the lead after 120 s (ROUTe:LEAD OPEN again)"` in the queue.
