# 2026-11-21 — Addendum to NW-00: the null shift does not go away with the transmit power

One session, from 21:04:27 to 22:58:44 (CET), the evening after the NW-00 night ([radio-ab-2026-11-21.md](radio-ab-2026-11-21.md)), on instrument `KM-0000000000`. NW-00's rule forbade a lower transmit power as a way to pass; this asks only whether one would have, so that the ruling rests on more than the worst case. It would not: the floor's noise and the beacon line go away below 20 dBm, the null's shift does not.

**The run.** C4 only (the meter as access point, the M5StickC Plus 1.1 host harness receiving the 200 kB/s stream), against C1 (the same image, Wi-Fi never started), at five transmit powers: 20, 11, 8, 5 and 2 dBm, each read back from the driver before its block. Eight interleaved rounds, the order rotated each round and reversed every other one, each C4 block flanked by two C1 blocks so that the powers share the references: 81 blocks, 40 C4 and 41 C1, none spoiled. Each block: the state change, 15 s to settle, a 61 s window on the null path with the `STATE` stream (its first second dropped); no DUT window and no load. The image was a diagnostic build of 0.16.1 with a transmit-power command, ELF `231981f6a24556a7`, never on a pushed branch. Each C4 difference is judged against the C1 A/A's spread at 99 %.

**Caveat.** The T-Deck was charging from its USB supply through the shunt during the whole run (−224 mA at the start, −26 mA at the end), so the DUT's own metrics are not comparable with the night's; the null-path metrics below are, because the bypass carries the DUT's current around the shunt.

| TX | null D (µV) | 99 % interval | floor sd D | its 99 % interval | beacon ×1 (T99 ≈ 1.28) | link RSSI (dBm) | stream, lowest (kB/s) |
|---|---|---|---|---|---|---|---|
| 20 dBm | **−0.260** | −0.312 .. −0.208 | **+2.43 %** | +2.10 .. +2.76 % | **5.49** | −42 .. −47 | 199.8 |
| 11 dBm | **−0.092** | −0.119 .. −0.066 | +0.17 % | −0.43 .. +0.78 % | 1.27 | −51 .. −55 | 199.7 |
| 8 dBm | **−0.084** | −0.103 .. −0.064 | −0.06 % | −0.47 .. +0.36 % | 1.11 | −55 .. −58 | 196.9 |
| 5 dBm | −0.057 | −0.124 .. +0.010 | +0.33 % | −0.15 .. +0.83 % | 0.93 | −55 .. −61 | 199.9 |
| 2 dBm | **−0.063** | −0.115 .. −0.011 | +0.10 % | −0.85 .. +1.05 % | 1.23 | −58 .. −64 | 199.6 |

n = 8 per power; bold is outside its interval (for the beacon line, above its bin's T99). At 5 dBm the null's interval only just reaches zero (p = 0.021).

- **The null moves at every power.** A least-squares line through the five differences against the radiated power in milliwatts gives **D ≈ −0.062 µV − 0.0020 µV/mW × P_TX**: a part that scales with the power (−0.20 µV at 100 mW, 20 dBm) and a part that does not, about −0.06 µV even at 2 dBm (1.6 mW). The night's null at 20 dBm, −0.145 µV, has the same sign and is smaller; the two runs differ in their references (the night's C1 standard deviation was 0.113 µV, the evening's 0.035 µV) and in the DUT's supply (the caveat above).
- **The floor's noise and the beacon line are the power's.** The floor's standard deviation rises 2.43 % at 20 dBm and is not distinguishable from C1 at 11 dBm or below; the beacon's first harmonic is 5.49 times its reference at 20 dBm and at most 1.27 below. The impulses (+5.8 per block) and the kurtosis move at 20 dBm only. The Allan deviation, the pacing and the die temperature moved at no power.
- **The link does not need the power.** Even at 2 dBm the client held −58 .. −64 dBm and the stream its 200 kB/s, with no disconnection. A meter that transmitted less would still have shifted its null.
- Counts in the radio blocks: `dropped=0 read_err=0` at every power, no `-363`, no garbled line; one stale edge in one 5 dBm block.

**What it adds to the ruling.** NW-00 judged the worst case and failed it. This shows the failure is not the worst case alone: a part of the null's shift is the radio being on, whatever it radiates, which no transmit-power cap removes. The meter stays serial-only (owner, 2026-11-21).

The bench at the end: the 0.16.5 `meter` image flashed back, `*TST?` 0, `ROUTe:STATe?` `path=shunt`, `kleidos-meter calibration verify` `KM-0000000000: applied`. The raw blocks (19 MB), the runner, its analysis and its log are kept on the bench host, not filed.
