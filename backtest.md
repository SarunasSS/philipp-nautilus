Full backtest invocations with every CLI flag spelled out. PowerShell continuations (`` ` ``), not
the `\` used in README.md.

`--original-bar-type` was removed in a647ad5 — the catalog bar type is derived from the composite
source named after the `@` in `--ltf-bar-type` / `--bar-type`. Do not reintroduce it.

Only `htf_sweep_cisd` sizes by risk: `--risk-per-trade` across the entry-to-stop distance, capped by
`--max-contracts`. The other four take a flat `--contracts`, for different reasons — DriftPullback's
and VaultBreak's stops are fixed point distances, so risk sizing would resolve to a constant anyway,
OvernightBiasORB's stop is a fraction of a rolling daily true range, so the dollar risk already
tracks volatility at a fixed size, and VWAP Pullback's pivot stop does vary but its source A/B-tested
risk sizing against the fixed lot and shipped the fixed lot. The tick value is derived from the catalog instrument, which is
why the continuous-contract multiplier had to be repaired — `ES.c.0.GLBX` and `NQ.c.0.GLBX` used to
carry a placeholder `1` cloned from the first definition record in the DBN file, and now hold the
real `50` and `20`. A wrong multiplier there silently mis-sizes every trade.

`htf_sweep_cisd` runs one trade at a time under netting; it previously used hedging and took roughly
a quarter more entries, so results before and after that change are not comparable.

This branch carries six strategies — `htf-sweep-cisd`, `drift-pullback`, `overnight-bias-orb`,
`vault-break`, `vwap-pullback-adx` and `subscribe` — and only those are documented here. The `notes/` evidence files and
`scripts/drift_pullback_verify.py` referenced below live on `Philipp_Strategy01`, not here.

## HTF sweep + CISD

```bash
uv run python main.py backtest `
  --trader-id PHILIPP-001 `
  --log-level INFO `
  --start 2026-01-01 `
  --end 2026-06-01 `
  --catalog-path data/ES/catalog `
  --starting-balance "100000 USD" `
  --visualize `
  --chart-bar-type ES.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL `
  --chart-bar-limit 10000 `
  htf-sweep-cisd run `
  --htf-bar-type ES.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL `
  --ltf-bar-type ES.c.0.GLBX-1-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL `
  --entry-order-type LIMIT `
  --entry-limit-offset 0.0 `
  --entry-order-expire-minutes 9 `
  --risk-per-trade 1000 `
  --max-contracts 10 `
  --stop-order-type MARKET `
  --stop-loss-distance-ratio 1.0 `
  --take-profit-multiplier 2.0 `
  --stop-limit-offset 0.0 `
  --signal-cooldown-seconds 0
```

`comparison_tolerance` is a config field with no CLI flag, so it cannot be set here.
`--signal-cooldown-seconds 0` disables the cooldown; omitting it defaults to one HTF period.

`--min-stop-atr-multiple` (off by default) skips a signal whose stop distance is under that multiple
of the LTF ATR(14). It exists because the round-turn cost is fixed while the CISD stop is not: on ES
this strategy risks 4.00 points at the median, where a 0.33-point round turn is 8.2% of the risk, and
1.75 points in its tightest decile, where it is 18.9%. See
`notes/htf_sweep_cisd_stop_width_evidence.md` for the measurement, the ablation, and why widening the
stop instead of skipping was tested and rejected.

## DriftPullback

The only strategy here that reads **three** bar streams of one instrument, and the only one that
does not size by risk. `--vwap-bar-type` accumulates the session VWAP, `--signal-bar-type` freezes
the anchor and the drift and arms the setup, `--bar-type` triggers the pullback. All three must name
the same instrument and share a catalog source; `--signal-bar-type` must be a strict multiple of
`--bar-type`, and `--vwap-bar-type` must divide `--signal-bar-type` exactly.

Catalog coverage for the 1-minute NQ set at `data/catalog` is 2026-01-01 → 2026-05-29.

```bash
uv run python main.py backtest `
  --trader-id PHILIPP-001 `
  --log-level INFO `
  --start 2026-01-01 `
  --end 2026-05-29 `
  --catalog-path data/catalog `
  --starting-balance "100000 USD" `
  --visualize `
  --chart-bar-type NQ.c.0.GLBX-5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL `
  --chart-bar-limit 10000 `
  drift-pullback run `
  --bar-type NQ.c.0.GLBX-5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL `
  --signal-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL `
  --vwap-bar-type NQ.c.0.GLBX-1-MINUTE-LAST-EXTERNAL `
  --session-timezone America/New_York `
  --entry-window 10:30-15:30 `
  --session-cutoff 15:55 `
  --vwap-window 09:30-16:00 `
  --require-full-vwap-session `
  --drift-lookback-bars 4 `
  --drift-threshold-pct 0.10 `
  --pullback-window-bars 6 `
  --trade-longs `
  --trade-shorts `
  --long-stop-points 80 `
  --long-target-points 40 `
  --short-stop-points 80 `
  --short-target-points 50 `
  --stop-slip-ticks 2 `
  --max-trades-per-day 4 `
  --max-losses-per-day 2 `
  --contracts 1
```

`--chart-bar-type` has to be given here. It defaults to the *catalog* bar type, which on this path
is the 1-minute source the strategy never trades on; pointing it at the execution stream is what
puts the fills on the bars that produced them.

**Session times are Eastern.** The source material ships Central defaults (`SessStart 930` =
09:30 CT), and against ET-keyed data those trade an hour off the validated window without erroring.
The values above are the tutorial's own Eastern column. Note that the two windows use opposite
conventions, and both are literal readings of the source: `--entry-window` is start-inclusive and
end-exclusive, `--vwap-window` is start-exclusive and end-inclusive.

**Keep `--vwap-bar-type` at one minute.** Accumulating the anchor on 15-minute bars, as the source
does, measures 3.11 points from the true tick VWAP against 0.33 for one minute, and moves the arming
verdict on roughly 2% of boundaries. One minute is also the finest resolution the Tradovate live
client can deliver, so holding it there is what makes the backtest anchor and the live anchor the
same number. Passing the 15-minute type reproduces the literal source; over the full period that
swaps 11 of 345 entries and $1,955 of result.

### DriftPullback on ticks

The bar types select the data path. A composite such as `@1-MINUTE-EXTERNAL` loads a bar catalog; a
plain `NQ.c.0.GLBX-5-MINUTE-LAST-INTERNAL` can only be aggregated from prints, so it selects the tick
runner. `--visualize` and the `--chart-*` flags do nothing on the tick path, because
`create_bars_with_fills` has no tick equivalent — a tick run exports CSVs only.

TBBO coverage is 2025-08-27 -> 2026-08-25. One day is roughly 1.07M events, so a month is about 20M
and runs in one pass; the full five-month bar period would be about 103M and needs chunking.

```bash
uv run python main.py backtest `
  --trader-id PHILIPP-001 `
  --log-level INFO `
  --start 2026-03-01 `
  --end 2026-04-01 `
  --catalog-path data/NQ_TBBO/catalog `
  --starting-balance "100000 USD" `
  --no-visualize `
  drift-pullback run `
  --bar-type NQ.c.0.GLBX-5-MINUTE-LAST-INTERNAL `
  --signal-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL `
  --vwap-source TRADE_TICKS `
  --session-timezone America/New_York `
  --entry-window 10:30-15:30 `
  --session-cutoff 15:55 `
  --vwap-window 09:30-16:00 `
  --require-full-vwap-session `
  --drift-lookback-bars 4 `
  --drift-threshold-pct 0.10 `
  --pullback-window-bars 6 `
  --trade-longs `
  --trade-shorts `
  --long-stop-points 80 `
  --long-target-points 40 `
  --short-stop-points 80 `
  --short-target-points 50 `
  --stop-slip-ticks 2 `
  --max-trades-per-day 4 `
  --max-losses-per-day 2 `
  --contracts 1
```

Note the absence of `--vwap-bar-type`: under `--vwap-source TRADE_TICKS` the anchor comes from
prints and passing a bar type is an error rather than a value that would be silently ignored. Swap
`--vwap-source TRADE_TICKS` for `--vwap-bar-type NQ.c.0.GLBX-1-MINUTE-LAST-INTERNAL` to keep tick
fills with a bar-derived anchor, which is the combination that stays comparable with a live run.

**Only the fills and the anchor change.** Bars aggregated from ticks are bit-identical to the
catalog-derived ones, so no signal can move for want of a data source. The A/B that separates the two
effects is three runs over one month: bar-fed with a bar anchor, tick-fed with a bar anchor, tick-fed
with a tick anchor. The first pair isolates the fill engine, the second the anchor.
`notes/drift_pullback_evidence.md` carries the numbers.

To reach further back than the five months at `data/catalog`, point the run at the 1-second catalog
and keep the anchor at one minute by aggregating it from the same source — the strategy code is
unchanged and only the three bar-type strings move:

```bash
uv run python main.py backtest `
  --trader-id PHILIPP-001 `
  --log-level ERROR `
  --start 2022-01-01 `
  --end 2023-01-01 `
  --catalog-path data/NQ/catalog `
  --starting-balance "100000 USD" `
  --no-visualize `
  drift-pullback run `
  --bar-type NQ.c.0.GLBX-5-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL `
  --signal-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL `
  --vwap-bar-type NQ.c.0.GLBX-1-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL `
  --contracts 1
```

Sizing is a flat `--contracts`; there is no `--risk-per-trade` here, because the stop is a constant
and risk sizing would give a constant contract count anyway. On NQ one contract risks 80.5 points ×
$20 = $1,610, so `--max-losses-per-day 2` fixes the worst day at $3,220 per contract before the run
starts. That is the point of the guardrails: they come from the account, not from the equity curve.

**`--pullback-window-bars` cannot bind at any value of 3 or more.** Arming is re-evaluated at every
signal boundary and always resets the counter, so with three execution bars per signal bar the count
never exceeds 3. A run at `3` is byte-identical to the shipped default of `6`; only `1` and `2`
change anything. `notes/drift_pullback_evidence.md` shows the engine runs.

`--stop-slip-ticks` widens the stop rather than modelling a worse fill at the same level, which is
what the source does — so it both costs and risks the extra 0.50 points. `0` switches it off and is
the comparison to run before reading any absolute figure.

`--no-require-full-vwap-session` restores the literal source. The guard is not in the source
material: it refuses to arm when accumulation began after `--vwap-window` opened, which in a
backtest only fires on a truncated first session but live is the difference between a session VWAP
and an anchor that merely looks like one.

Check the implementation against the raw parquet — it recomputes every entry from the specification
and diffs it against the exported positions:

```bash
uv run python scripts/drift_pullback_verify.py
```

## OvernightBiasORB

The only strategy here that reads a single bar stream, and the second that does not size by risk.
`--bar-type` must name a composite source: that source is the one-minute bar magnifier the material
requires, because the venue matches resting orders against the bars the engine is fed rather than
against the 15-minute stream the strategy subscribes to. A bar type without one is rejected.

Catalog coverage for the 1-minute NQ set at `data/catalog` is 2026-01-01 → 2026-05-29.

```bash
uv run python main.py backtest `
  --trader-id PHILIPP-001 `
  --log-level INFO `
  --start 2026-01-01 `
  --end 2026-05-29 `
  --catalog-path data/catalog `
  --starting-balance "100000 USD" `
  --visualize `
  --chart-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL `
  --chart-bar-limit 10000 `
  overnight-bias-orb run `
  --bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL `
  --session-timezone America/New_York `
  --session-start 09:30 `
  --opening-range-end 09:45 `
  --last-entry 13:00 `
  --session-cutoff 15:30 `
  --session-end 16:00 `
  --max-trades-per-day 4 `
  --bias-long-min 0.67 `
  --bias-short-max 0.33 `
  --adx-length 16 `
  --adx-min 20 `
  --break-skip-multiple 2.5 `
  --bar-average-length 20 `
  --min-or-multiple 0 `
  --max-or-multiple 2 `
  --or-median-days 20 `
  --atr-multiple 0.30 `
  --atr-days 15 `
  --take-profit-rr 3 `
  --contracts 1
```

`--chart-bar-type` has to be given, for the same reason it does for DriftPullback: it defaults to the
catalog bar type, which on this path is the one-minute source the strategy never trades on.

**Session times are Eastern.** The source ships a Central column as well
(0830 / 0845 / 1200 / 1430 / 1500); against ET-keyed data those trade an hour off the validated
window without erroring. Every boundary except `--session-cutoff` must fall on a bar boundary, and
the run refuses one that does not — `--session-start 09:31` on a 15-minute bar type is an error
rather than a partition quietly shifted by up to a bar. `--session-cutoff` is exempt because it is
compared against the clock rather than against a bar's closing stamp.

**The overnight range runs from midnight, not from the 18:00 Globex open.** The source resets it on a
calendar-date change and extends it while the bar close is at or before `--session-start`, which on
Eastern data means `(00:00, 09:30]`. That range is the denominator of the bias rule, so the reading
matters more than anything else in the file; this is the literal one, and the one the source's own
published statistics came from.

**Two warm-ups run before the strategy is armed and the second one is silent.** Nothing trades until
`--atr-days` complete regular-hours sessions have accumulated, because the stop is `--atr-multiple`
of that reference and there is no stop without it. The opening-range band then stays **off** until
`--or-median-days` ranges have been seen. At the defaults that is no trades for 15 sessions, then
five sessions traded with one filter missing. Both counters are run-lifetime rather than per-session,
so a window shorter than about two months is mostly warm-up: over the command above the reference
completes on 23 January, the sixteenth session, and the first trade lands on 26 January.

Sizing is a flat `--contracts` and there is no `--risk-per-trade`, but for the opposite reason to
DriftPullback's. The stop is `--atr-multiple` of a 15-day average of daily regular-hours true ranges,
so it is *not* constant — it measured 37.50 to 140.50 points across the stopped trades of the 2026
sample, or $750 to $2,810 a contract on NQ. Risk already tracks volatility, which is what the fixed
size is for. There is no slippage flag either, because the source has no slippage input; the
top-level `--commission-per-contract` is the knob to reach for.

Filter ablations over the same window, against 46 entries with everything switched off: `--adx-min`
removes 3, the `--min-or-multiple` / `--max-or-multiple` band removes 4, and the two together remove
9. `--break-skip-multiple` removes **zero** — it does reject individual breaks, and is what turns
away the 88.75-point breakout bar on 23 January, but every session where it fired either had no bias
or went on to trade a later break. Treat its shipped 2.5 as carried over from the source rather than
as a measured setting.

### OvernightBiasORB out of sample

The source header claims 599 trades, $257,615 net and PF 1.66 over 2020-01-01 → 2026-08-31. That is
reachable here: `data/NQ/catalog` holds NQ 1-second back to 2010-06-07 and forward to 2026-07-31.

Run it in **year chunks with a six-week warm-up prefix**, so no chunk loses trades to the ladder
above, then keep only the rows whose entry falls inside the year:

```bash
uv run python main.py backtest `
  --log-level ERROR `
  --start 2019-11-15 `
  --end 2021-01-01 `
  --catalog-path data/NQ/catalog `
  --starting-balance "100000 USD" `
  --no-visualize `
  overnight-bias-orb run `
  --bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL `
  --contracts 1
```

Chunking is not optional. `_load_backtest_data` materialises every source bar into a Python list and
one year of 1-second data is already about 14 million of them; copy
`data/results/overnight-bias-orb-positions.csv` aside after each chunk, since the next one overwrites
it. Seven chunks take roughly ten minutes each.

| | port | source header |
|---|---|---|
| trades | 591 | 599 |
| net | $228,700 | $257,615 |
| profit factor | 1.58 | 1.66 |
| max drawdown | $19,135 | $20,195 |

Measured 2020-01-01 → 2026-07-31, one month short of the header's window. Per-year trade counts are
74 / 92 / 93 / 91 / 99 / 80 / 62; every year is profitable and the worst, 2025, still returns 1.11.
Two things keep this from being an exact comparison: the `@1-SECOND-EXTERNAL` source is a finer
magnifier than the one minute the material specifies, so its exit fills are slightly kinder, and this
runs on a Databento continuous NQ series rather than whichever series the source was validated
against. The residual gap is most likely that, not the rules.

**The time exit carries the edge, which is not how the source frames it.** Only 3 of the 37 trades in
the 2026 window reached the 3R target against 10 stops — on resolved trades alone the strategy loses,
and the 24 flattens at `--session-cutoff` supply all of the profit. It holds at scale: 53.6% winners
over 591 trades, far above the ~36% a 3:1 payoff at PF 1.66 implies. Ablate `--session-cutoff` and
`--take-profit-rr` before trusting the published framing.

## VaultBreak

The only long-only strategy here, and the second whose bracket is a fixed point distance. It reads a
single 30-minute stream, and `--bar-type` must name a composite source: that source is the one-minute
bar magnifier the source material requires, and it is what lets a trade open and close inside one
30-minute bar — which the reference trade list below does 14 times out of 43.

Catalog coverage for the 1-minute NQ set at `data/catalog` is 2026-01-01 → 2026-05-29 17:00 ET.

```bash
uv run python main.py backtest `
  --trader-id PHILIPP-001 `
  --log-level INFO `
  --start 2026-01-01 `
  --end 2026-05-30 `
  --catalog-path data/catalog `
  --starting-balance "100000 USD" `
  --visualize `
  --chart-bar-type NQ.c.0.GLBX-30-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL `
  --chart-bar-limit 10000 `
  vault-break run `
  --bar-type NQ.c.0.GLBX-30-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL `
  --session-timezone America/New_York `
  --earliest-entry 11:00 `
  --flatten-time 15:30 `
  --noise-multiple 0.3 `
  --boundary-atr-days 15 `
  --max-trades-per-day 3 `
  --vwap-exit-bars 7 `
  --take-profit-points 40 `
  --stop-loss-points 75 `
  --contracts 1
```

Note the `--end 2026-05-30` where the other sections use 05-29. `--end` is exclusive, so passing
05-29 silently drops that whole session — which costs the last trade of the reference comparison
below and is easy to mistake for a rules difference. `--chart-bar-type` has to be given for the same
reason it does everywhere else: it defaults to the catalog bar type, which here is the one-minute
source the strategy never trades on.

Each session fixes one level. The first traded bar of the day supplies the session open, and
`--noise-multiple` of a `--boundary-atr-days` mean of completed session ranges is added to it; that
sum is the noise boundary and it does not move again all day. Inside the entry window, any bar
closing above both the boundary and the session VWAP is bought at market, up to
`--max-trades-per-day`, one position at a time. Exits are `--take-profit-points` above and
`--stop-loss-points` below the **fill**, plus a flatten at `--flatten-time`.

**Session times are Eastern, and the daily roll follows `--session-timezone` too.** The source ships
Central inputs (1000 / 1430) and its own header gives the Eastern equivalents. That choice is not
cosmetic here, because the roll is what anchors the session open, the session range and the VWAP.
Replaying the rules over this catalog against the source's 43-trade reference list reproduces **42
on an Eastern-keyed day, 39 on a Central-keyed one and 27 on a Globex trade day**, so Eastern is what
is implemented. `--session-timezone America/Chicago --earliest-entry 10:00 --flatten-time 14:30`
gives the literal Central reading — the same entry window in absolute time, with the day rolling at
midnight CT — and is the first thing to try if a comparison ever misses on *which sessions* trade.

**The bracket is in points, and `--contracts` does not move it.** The source expresses it in dollars
under `SetStopPosition`, which makes $800 / $1500 whole-position amounts: on NQ at one contract those
are exactly the 40 and 75 points shipped here, but at two contracts the source would tighten them to
20 and 37.5 and silently become a different strategy. Points instead make `--contracts` a linear
multiplier on dollar risk, and take `instrument.multiplier` out of the level arithmetic altogether —
worth having, given the placeholder-multiplier hazard described at the top of this file. The cost is
that 40 and 75 are NQ figures where dollars would have carried to another contract.

**Nothing trades for the first `--boundary-atr-days` sessions.** The boundary reference is empty
until then and the entry gate refuses without it. Over the command above the reference completes on
2026-01-19, the sixteenth session, exactly as the source header says it should, and the first entry
lands on 2026-01-21.

### VaultBreak against the MultiCharts reference

`docs/VaultBreak/Backtesting Strategy Performance Report _ NQ TIP Data - 30 Minutes IP_VaultBreak.xlsx`
holds the 43 trades the original produced over 2026-04-22 → 2026-05-29. Restricting the run above to
that window:

| | port | MultiCharts |
|---|---|---|
| entries | 46 | 43 |
| exits | 28 target / 15 stop / 3 flatten | 27 target / 13 stop / 3 flatten |
| net | $160 | $2,360 |

**All 43 reference trades are reproduced**, on the same signal bar, with the same exit reason, and
their net is identical to the dollar at $2,360. Entry fill prices differ by at most 1.25 points and
0.33 on average, which is the gap between the source's "NQ TIP" series and this Databento continuous
one. MultiCharts stamps a fill with the close of the 30-minute bar containing it, so its stamps run
one bar ahead of `ts_opened` in `data/results/vault-break-positions.csv`.

The whole difference is three extra entries — 2026-05-21 14:30, 2026-05-29 12:00 and 13:30 by the
MultiCharts stamp — and all three clear the noise boundary by **1.91, 5.59 and 4.84 points** against
a median margin of 114 points across all 125 entries of the full run. They are the same data gap
seen from the other side: 2026-05-29 offers three signals clearing by +5.59, +11.84 and +4.84, and
the reference takes exactly the one at +11.84, which is what a boundary sitting some six points
higher would do.

### What the reference window does not show

Over the full 2026-01-01 → 2026-05-30 run the strategy takes 125 trades and **loses $3,480** on one
contract with no costs, against the reference window's +$2,360. The decomposition is worth knowing
before trusting either number:

- 75 targets and 40 stops cancel to **exactly zero** — 40 × 75 points against 75 × 40 points.
- All of the loss is the 10 flattens at `--flatten-time`, which is the mirror image of
  OvernightBiasORB, where the time exit supplies all of the profit.
- The bracket therefore resolved at 75 of 115, or 65.2%, which is precisely the break-even rate a
  40:75 payoff needs. There is no margin in it at these settings over this sample.

Monthly, on one contract: January −$3,995 (9 trades), February −$3,335 (24), March +$1,600 (25),
April +$5,030 (31), May −$2,780 (36). Five months is a short sample and the warm-up eats half of
January, but the reference window is 43 of those 125 trades and is not representative of the rest.

**`--vwap-exit-bars` never fires.** Not once in the reference list, and not once in this run — the
counter never passed 1 while a position was open. Seven consecutive closes below the session VWAP is
three and a half hours inside a four and a half hour window, on a trade that had to close *above*
the VWAP to open and that resolves in a bar or two. The rule is implemented because it is in the
source, but treat the shipped 7 as carried over rather than measured. The source's `VB8.Carry` guard
is likewise unreachable here and is a comment rather than code, for the reason given at
`strategies/vault_break.py:_roll_session`.

There is no slippage flag, because the source has no slippage input; the top-level
`--commission-per-contract` is the knob to reach for.

## VWAP Pullback + ADX Gate

The only strategy here whose bracket is market structure rather than a point distance or a
volatility multiple: the stop is the last confirmed 20-bar pivot low and the target the last
confirmed 5-bar pivot high, both frozen on the signal bar. It reads a single 1-minute stream over
the whole 24-hour session, and `--bar-type` may be either the plain catalog type or a composite
aggregated from a finer source that then acts as the bar magnifier.

Catalog coverage for the 1-minute NQ set at `data/catalog` is 2026-01-01 → 2026-05-29 17:00 ET.

```bash
uv run python main.py backtest `
  --trader-id PHILIPP-001 `
  --log-level INFO `
  --start 2026-01-01 `
  --end 2026-05-30 `
  --catalog-path data/catalog `
  --starting-balance "100000 USD" `
  --visualize `
  --chart-bar-type NQ.c.0.GLBX-1-MINUTE-LAST-EXTERNAL `
  --chart-bar-limit 10000 `
  vwap-pullback-adx run `
  --bar-type NQ.c.0.GLBX-1-MINUTE-LAST-EXTERNAL `
  --session-timezone America/New_York `
  --opening-range-window 09:30-10:00 `
  --vwap-window 09:30-18:00 `
  --entry-window 10:00-18:00 `
  --eod-flat `
  --eod-flat-time 16:55 `
  --retest-mode TOUCH `
  --stop-mode PIVOT `
  --stop-swing-length 20 `
  --target-swing-length 5 `
  --stop-buffer-points 0 `
  --max-trades-per-day 1 `
  --trade-longs `
  --no-trade-shorts `
  --min-rr 0 `
  --max-stop-points 0 `
  --use-adx-elevation `
  --adx-elevation-threshold 20 `
  --use-adx-decay `
  --adx-decay-lookback 1 `
  --adx-length 14 `
  --contracts 1
```

On the plain 1-minute type the catalog bar type is the one the strategy trades on, so
`--chart-bar-type` may be left to its default; on the composite form below it has to name the
1-minute stream, for the reason given in every other section.

**Session times are Eastern, and the daily roll is midnight Eastern.** The source ships Central
inputs for an Exchange-time chart and resets its state at 23:00 CT, which is midnight New York; the
tutorial's own table gives the equivalents, and the Eastern column is what is implemented:

| input | meaning | Central (source) | Eastern (here) |
|---|---|---|---|
| ORStartTime / OREndTime | `--opening-range-window` | 0830 / 0900 | 09:30-10:00 |
| VwapStartTime / VwapEndTime | `--vwap-window` | 0830 / 1700 | 09:30-18:00 |
| EntryStartTime / EntryEndTime | `--entry-window` | 0900 / 1700 | 10:00-18:00 |
| EODFlatTime | `--eod-flat-time` | 1555 | 16:55 |
| DayResetTime | the calendar-day roll | 2300 | midnight |

All three windows select a bar by its closing stamp, after the start and at or before the end,
which is the one convention the source uses for all of them — the opposite of DriftPullback's entry
window. `--session-timezone America/Chicago` with the Central column gives the literal reading;
the roll then moves to midnight Central and the 23:00-to-midnight hour changes sessions.

**One rule departs from the source.** With `--eod-flat` on, a signal on a bar closing at or after
`--eod-flat-time` is refused. The source would enter it at the next bar's open and flatten it at the
bar after that, a one-minute trade that can only cost the spread; the entry window still runs to
18:00 because the source's does, but the flatten time is its effective end. Everything else is
literal, including the parts that look odd on a chart: the ADX gate delays rather than cancels, so a
reclaim that meets a closed gate fires later if it still holds; a day that closes back inside the
range keeps its bias; the pivots and the ADX survive the roll, so the first stop of a session can
be an overnight low; and a stop one tick below the close is a valid stop.

**The market entry fills at the signal bar's close** rather than at the next bar's open, as in the
OvernightBiasORB section. With the plain 1-minute type the venue matches the bracket against
1-minute bars, so a stop and a target straddled by one bar resolve in whichever order the engine
processes them; the 1-second magnifier below is what settles that by price path. Nautilus's
`WilderMovingAverage` is an EMA with alpha 1/N seeded on its first input where MultiCharts seeds
its ADX with a simple average over the first N bars; the two converge geometrically, so only the
first hour of a run can gate a bar differently. Nothing trades until the 20-bar pivot has 41 bars
of history, which is inside the first session of any run.

Over the command above the port takes **47 trades and makes $17,140 gross on one contract**: 33
targets for +$25,705, 8 stops for −$5,930 and 6 flattens at 16:55 for −$2,635, a 70 / 17 / 13
percent split against the source's 64.5 / 29.1 / 6.4 over its whole 2021 → 2026 sample. Monthly:
January +$5,150 (9 trades), February +$4,055 (7), March −$20 (10), April +$5,765 (11), May +$2,190
(10). The median hold is nine minutes, the average winner $756 and the average loser $776, and the
worst closed-trade drawdown $4,430. The median stop is 105.50 points from the fill against a median
target of 33, so the median trade risks about three times what it stands to make and the edge is
the hit rate, exactly as the tutorial says; the widest stop was 464.50 points on 2026-01-21, the
pivot rule working as written on a day that trended from the open, and the tightest a single tick on
2026-01-26. Three entries came after the 16:00 RTH close, inside the source's 18:00 entry window.

There is no reference trade list for this strategy. Instead the run was replayed by an independent
pandas script that re-derives the opening range, the session VWAP, a Wilder ADX, both pivot
detectors and the latch logic straight from the PowerLanguage source: every one of the port's
entries lands on the bar the replay signals, no replay signal is missing from the port, and every
stop and target price in `data/results/vwap-pullback-adx-orders.csv` equals the replay's pivot
level.

### VWAP Pullback + ADX Gate against the source's yearly results

The tutorial publishes trade counts and gross profit per year for one NQ contract, 5 January 2021 to
14 August 2026, from the MultiCharts performance report. `data/NQ/catalog` holds 1-second bars from
2010 to 2026-07-31, so the same years replay here with the 1-second source as the magnifier:

```bash
uv run python main.py backtest `
  --log-level ERROR `
  --start 2024-12-28 `
  --end 2026-01-01 `
  --catalog-path data/NQ/catalog `
  --no-visualize `
  vwap-pullback-adx run `
  --bar-type NQ.c.0.GLBX-1-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL `
  --contracts 1
```

Chunking is not optional: `_load_backtest_data` materialises every source bar, and one year of
1-second data is 12 to 13 million of them, which a 32 GB machine with anything else open runs out
of memory on — 2021 to 2023 ran as whole years, 2024 onwards as half-years. Each chunk starts four
days early so the pivots and the ADX are warm on its first day, and the trades below are those
opened inside the chunk's own window.

| year | port trades | port net | targets / stops / flattens | win rate | source trades | source net |
|---|---|---|---|---|---|---|
| 2021 | 108 | $2,090 | 69 / 36 / 3 | 66.7% | 110 | $4,155 |
| 2022 | 111 | $5,360 | 68 / 35 / 8 | 62.2% | 112 | $3,350 |
| 2023 | 121 | $9,975 | 82 / 35 / 4 | 68.6% | 122 | $13,285 |
| 2024 | 125 | $11,250 | 80 / 38 / 7 | 68.0% | 127 | $11,850 |
| 2025 | 128 | $28,265 | 80 / 36 / 12 | 66.4% | 131 | $25,265 |
| 2026 · to 31 Jul | 66 | $23,195 | 47 / 13 / 6 | 74.2% | 71 · to 14 Aug | $24,515 |
| **total** | **659** | **$80,135** | 426 / 193 / 40 | **67.2%** | **673** | **$82,420** |

**The port reproduces the source to within a few trades a year and the same profit factor over
the whole sample.** 659 trades against 673 with two weeks of 2026 missing, $80,135 against
$82,420, a 67.2% win rate against 67.2%, and a gross profit factor of 1.42 against 1.42. The
source's own in-sample / out-of-sample split holds too: 2021 → 2024 gives 465 trades and $28,675
at a 1.21 profit factor here against its 471 and $32,640 at 1.24, and 2025 onwards 194 trades and
$51,460 at 1.97 against 202 and $49,780 at 1.84. The exit mix is 64.6 / 29.3 / 6.1 percent against
the published 64.5 / 29.1 / 6.4. The residual per-year gap runs both ways — 2023 lands $3,310 under
and 2025 $3,000 over — and is what a different data series (Databento continuous against the
source's "NQ TIP" symbol), a signal-close fill against a next-bar-open fill, and the refused
after-flatten entries add up to on a median bracket of tens of points. There is nothing in it that
points at a rules difference.

The 2021 row is the one to read with care on both sides: at $2,090 gross over 108 trades the port
does not cover the source's own $15 round-trip cost basis, and the source at $4,155 barely does,
which is the tutorial's own point about that year.

There is no slippage flag, because the source has no slippage input; its cost basis is $2.50 a side
plus one tick, $15 a round trip, and `--commission-per-contract 2.5` reproduces the commission half
of that.

## Subscribe

`--bar-type` is the only strategy option and is optional; it defaults to the first catalog bar type
in range.

```bash
uv run python main.py backtest `
  --trader-id PHILIPP-001 `
  --log-level INFO `
  --start 2026-01-01 `
  --end 2026-06-01 `
  --catalog-path data/ES/catalog `
  --starting-balance "100000 USD" `
  --no-visualize `
  subscribe run `
  --bar-type ES.c.0.GLBX-1-SECOND-LAST-EXTERNAL
```
