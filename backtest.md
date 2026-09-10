Full backtest invocations with every CLI flag spelled out. PowerShell continuations (`` ` ``), not
the `\` used in README.md.

`--original-bar-type` was removed in a647ad5 — the catalog bar type is derived from the composite
source named after the `@` in `--ltf-bar-type` / `--bar-type`. Do not reintroduce it.

Only `htf_sweep_cisd` sizes by risk: `--risk-per-trade` across the entry-to-stop distance, capped by
`--max-contracts`. The other two take a flat `--contracts`, for opposite reasons — DriftPullback's
stop is a fixed point distance, so risk sizing would resolve to a constant anyway, while
OvernightBiasORB's stop is a fraction of a rolling daily true range, so the dollar risk already
tracks volatility at a fixed size. The tick value is derived from the catalog instrument, which is
why the continuous-contract multiplier had to be repaired — `ES.c.0.GLBX` and `NQ.c.0.GLBX` used to
carry a placeholder `1` cloned from the first definition record in the DBN file, and now hold the
real `50` and `20`. A wrong multiplier there silently mis-sizes every trade.

`htf_sweep_cisd` runs one trade at a time under netting; it previously used hedging and took roughly
a quarter more entries, so results before and after that change are not comparable.

This branch carries four strategies — `htf-sweep-cisd`, `drift-pullback`, `overnight-bias-orb` and
`subscribe` — and only those are documented here. The `notes/` evidence files and
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
