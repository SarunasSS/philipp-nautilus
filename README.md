# Philipp Nautilus

NautilusTrader scaffold for backtesting experiments and live market-data/execution adapters.

## Repository Structure

```text
cli/
├── backtest/            # Run catalog-backed backtests
│   ├── __init__.py      # Shared backtest callback and runner helpers
│   ├── drift_pullback.py
│   ├── htf_sweep_cisd.py
│   ├── overnight_bias_orb.py
│   ├── subscribe.py
│   └── vault_break.py
├── catalog.py           # Download Databento bars into the catalog
└── live/                # Run live strategies through configured data providers
    ├── __init__.py      # Shared live callback and runner
    ├── execute.py       # Case-selected execution adapter tests
    └── subscribe.py     # Generic bar subscription command
docs/
├── agents/              # Work notes for resumability
│   └── strategy_visualization_options.md  # Charting options and recommendation
├── Strategy_fileHTFSweep+CISD.docx
├── Tradovate_API.md     # Sanitized and supplemented Tradovate API reference
└── strategy_flow.md     # Explicit HTF sweep + CISD strategy flow
src/phillip/adapters/tradovate/
├── data.py              # Bars-only Nautilus live data client
├── execution.py         # Account lifecycle, user stream, and reconciliation
├── execution_commands.py # Order command translation
├── execution_parsing.py # Tradovate-to-Nautilus execution transforms
├── execution_reports.py # REST-backed report retrieval
├── providers.py         # Futures instrument discovery
├── http/                # REST authentication and contract metadata
└── websocket/           # Framing, heartbeat, and reconnect handling
strategies/
├── base.py              # Shared live/backtest stratlet lifecycle
├── drift_pullback.py    # Session-VWAP drift continuation bought on the first 5-minute pullback
├── execute.py           # ExecuteStrategy subscription plus execution tests
├── htf_sweep_cisd.py    # HTF sweep + CISD managed trade strategy
├── overnight_bias_orb.py # Opening range breakout taken only with the overnight gap bias
├── subscribe.py         # SubscribeStrategy requests and logs bars
└── vault_break.py       # Session noise-boundary breakout bought above the session VWAP
```

## Setup

This project uses `uv` for environment and dependency management.

```bash
uv venv --python 3.13
uv sync
```

Tradovate accepts username/password authentication. API-key authentication additionally requires the key name (`TRADOVATE_APP_ID`), key ID (`TRADOVATE_CID`), and key secret (`TRADOVATE_SEC`) as one complete bundle. Existing `TRADOVATE_ACCESS_TOKEN` and `TRADOVATE_MD_ACCESS_TOKEN` can be supplied together instead. Keep these values out of source control and logs.

The live node registers every provider for which credentials are present. Nautilus's built-in Databento adapter reads `DATABENTO_API_KEY`; Tradovate uses the credential forms described above.
Set `TRADOVATE_ACCOUNT_ID` when the credentials expose more than one open account. The execution client deliberately refuses to guess which account should receive commands.

## Run

Download NQ front-month continuous 1-minute bars from Databento into the local catalog:

```bash
uv run python main.py catalog bars download --start 2026-01-01 --end 2026-06-01
```

Run the subscribe-bar backtest over the first half of 2026:

```bash
uv run python main.py backtest \
  --start 2026-01-01 \
  --end 2026-06-01 \
  subscribe run
```

Run the HTF sweep + CISD strategy. It logs `Short_signal` / `long_signal` when the CISD condition confirms, then manages the entry, stop-loss, and take-profit lifecycle:

```bash
uv run python main.py backtest \
  --start 2026-01-01 \
  --end 2026-06-01 \
  htf-sweep-cisd run \
  --htf-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  --ltf-bar-type NQ.c.0.GLBX-1-MINUTE-LAST-EXTERNAL \
  --entry-order-type LIMIT \
  --entry-limit-offset 0.0 \
  --risk-per-trade 1000 \
  --max-contracts 5 \
  --stop-order-type MARKET \
  --stop-loss-distance-ratio 1.0 \
  --take-profit-multiplier 2.0
```

`--entry-limit-offset` and `--stop-limit-offset` are direct ratios, so `0.001` means `0.1%`. Signal cooldown defaults to one HTF period and can be overridden with `--signal-cooldown-seconds`.

Sizing is risk-based: `--risk-per-trade` is the cash put at risk on each trade, spread across the distance from the entry to the stop, and it defaults to `1000`. The stop follows from the CISD level and the swept swing alone, so it is known before the entry order goes out, which is what makes that sizing possible. A tight stop therefore buys more contracts than a wide one — `--max-contracts` caps that, and left unset the risk figure alone decides. The strategy takes one trade at a time and the venue nets, so a signal arriving while a trade is still open is skipped rather than opening a second position.

`--ltf-bar-type` may either be the catalog bar type itself or a composite aggregated from it, such as `NQ.c.0.GLBX-5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL`; in the composite case the catalog source is loaded and the LTF stream is aggregated from it. HTF and LTF have to aggregate from the same source bar type.

Run DriftPullback, an intraday drift-continuation model bought on the first pullback against the
drift. It is the only strategy here that reads **three** bar streams of one instrument: the session
VWAP accumulates on the fastest, the anchor and drift are frozen on the slowest, and the pullback
triggers on the middle one.

```bash
uv run python main.py backtest   --start 2026-04-01   --end 2026-05-01   drift-pullback run   --bar-type NQ.c.0.GLBX-5-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL   --signal-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL   --vwap-bar-type NQ.c.0.GLBX-1-MINUTE-LAST-EXTERNAL   --contracts 1
```

At every 15-minute close the setup arms long when three things hold together: the close is above the
session VWAP, the VWAP is itself rising against its previous 15-minute snapshot, and the close is at
least `--drift-threshold-pct` above the close `--drift-lookback-bars` signal bars ago. The short side
is the exact mirror. A signal bar that satisfies neither set **disarms** rather than leaving the
previous setup standing. Once armed, the first 5-minute bar that closes against the drift - red for a
long, green for a short - is bought at market on the next bar, and the flag is spent whether or not
the fill arrives.

Everything the 15-minute stream computes is frozen between its own boundaries, which is what makes
the backtest and a live run see the same signal inside a signal bar. Coincident bars arrive VWAP
source first, then the signal bar, then the execution bar, so the bar that arms a setup can also be
the bar that triggers it.

`--vwap-bar-type` is a separate option rather than being derived from `--bar-type`, and it should
stay at one minute. The spec accumulates the anchor from 15-minute bars, which measures **3.11
points** away from the true tick VWAP against **0.33** for one minute, and moves the arming verdict
on ~2% of boundaries; one minute is also the finest resolution the Tradovate live client can deliver,
so keeping it there is what makes the backtest anchor and the live anchor the same number. Pass the
15-minute type to run the literal reading.

Exits are fixed point distances from the actual fill, not from the signal bar, so they are attached
once the market entry fills: `--long-stop-points` / `--long-target-points` and the short pair, with
`--stop-slip-ticks` pushing the stop that many ticks further against you and leaving the target
alone. That is a deliberate pessimism in the backtest, not a trading rule. Every open position is
flattened at `--session-cutoff` regardless.

The guardrails come from the account rather than the edge: `--max-trades-per-day` caps entries and
`--max-losses-per-day` stops the session after that many losing round trips, so the worst day is
known in advance - two 80.5-point stops, $3,220 per NQ contract. Sizing is a flat `--contracts`;
there is no risk-derived sizing here, because the stop is a constant.

Note that `--pullback-window-bars` **cannot bind at any value of 3 or more** - arming is
re-evaluated every 15-minute boundary and always resets the counter, so a run at 3 is byte-identical
to the shipped default of 6.

The strategy also runs on **tick data**, and the bar types select it: a type naming a composite
source such as `@1-MINUTE-EXTERNAL` is built from a bar catalog, while a plain
`NQ.c.0.GLBX-5-MINUTE-LAST-INTERNAL` can only be aggregated from prints, so it selects the tick
runner on its own. There is no separate flag that could contradict the bar types. A tick-fed run
needs quote and trade ticks in the catalog, which this branch has no download command for.

Run OvernightBiasORB, an opening-range breakout that is only allowed to trade in the direction the
overnight gap implies. It reads a single 15-minute stream, and the composite source named after the
`@` is doing real work: the venue matches resting orders against the bars the engine is fed, so a
`@1-MINUTE-EXTERNAL` source is exactly the one-minute bar magnifier the source material requires. A
bar type with no composite source is refused rather than silently run without one.

```bash
uv run python main.py backtest \
  --start 2026-01-01 \
  --end 2026-05-29 \
  --chart-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  overnight-bias-orb run \
  --bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  --contracts 1
```

Each session freezes a direction once, from the open of the first bar of the opening range: where
that open sits inside the overnight range decides the day. At or above `--bias-long-min` of the range
the day is long-only, at or below `--bias-short-max` short-only, and anything between the two is a
no-trade day - so the rule is gap **continuation**, not mean reversion. The opening range itself is
then locked at `--opening-range-end`, and the first bar that *closes* beyond it in the frozen
direction is bought or sold at market. A break that is rejected by one of the filters spends nothing:
the level stays live and a later bar closing beyond it can still trigger, up to `--max-trades-per-day`.

**The overnight range runs from midnight, not from the 18:00 Globex open.** The source resets it on a
calendar-date change and extends it up to `--session-start`, which on Eastern-keyed data means
`(00:00, 09:30]`. That range is the denominator of the whole bias rule, and it is the reading the
source's published statistics came from, so it is the one implemented here.

**Session times are Eastern.** The source material also carries a Central column
(0830 / 0845 / 1200 / 1430 / 1500); against ET-keyed data those trade an hour off the validated
window without erroring.

Three filters sit in front of the break, and two of them bind on real data. `--adx-min` is a Wilder
ADX gate meant to sit out chop - Nautilus ships no ADX, so it is computed here, and a
simple-average lookalike would pass different bars. `--min-or-multiple` / `--max-or-multiple` reject
a session whose opening range is out of character against a `--or-median-days` reference. Over
January to May 2026 those two together remove 9 of 46 entries. The third,
`--break-skip-multiple`, skips a break printed on a bar wider than that many prior average ranges.
It does reject breaks - it is what turns away the 88.75-point bar that broke out on 23 January -
but over the same window it changed the trade count by zero, because every session where it
rejected a break either had no bias or went on to trade a later one. Treat its shipped 2.5 as
carried over from the source rather than as a measured setting.

**Two warm-ups run before the strategy is fully armed, and the second one is silent.** Nothing trades
until `--atr-days` complete regular-hours sessions have been seen, because the stop is
`--atr-multiple` of that reference and there is no stop without it. The opening-range band then stays
switched **off** until `--or-median-days` ranges have accumulated. At the defaults that is no trades
for 15 sessions and then five sessions traded with one filter missing. Both counters are
run-lifetime, not per-day, so a run shorter than about two months is mostly warm-up: over the
January to May window above the reference completes on 23 January, the sixteenth session, and the
first trade lands on 26 January.

Risk is not flat here even though size is. The stop is `--atr-multiple` times a 15-day average of
daily regular-hours true ranges, so it moves with volatility - 37.50 to 140.50 points across the
stopped trades of the 2026 sample, or $750 to $2,810 a contract on NQ - and `--take-profit-rr`
puts the target at a whole multiple of it. That is why sizing is a flat `--contracts` with no
`--risk-per-trade`: the dollar risk is already tracking volatility.

Against the source's own out-of-sample claim the port lands close. Replaying 2020-01-01 to
2026-07-31 on `data/NQ/catalog` in year chunks, each given a six-week warm-up prefix so no chunk
loses trades to the ladder above, gives **591 trades, $228,700 net and a 1.58 profit factor on one
contract, with a $19,135 maximum drawdown** - against the header's 599 trades, $257,615 and 1.66
measured to 2026-08-31, one month further on than the catalog reaches. Every year is profitable and
the worst of them, 2025, still returns 1.11. The residual gap is most likely the data rather than
the rules: this runs on a Databento continuous NQ series, not the one the source was validated
against.

```bash
uv run python main.py backtest \
  --log-level ERROR \
  --start 2019-11-15 \
  --end 2021-01-01 \
  --catalog-path data/NQ/catalog \
  --no-visualize \
  overnight-bias-orb run \
  --bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-SECOND-EXTERNAL \
  --contracts 1
```

Chunking is not optional there: `_load_backtest_data` materialises every source bar into a list, and
one year of 1-second data is already about 14 million of them. That 1-second source is also a finer
magnifier than the one minute the material specifies, so its exit fills are a little kinder.

Both exits are measured from the **signal bar's close**, which is the reverse of DriftPullback's
measurement from the fill. In this engine a market order submitted from `on_bar` settles against the
book that bar left behind, so it fills at that same close and every trade is exactly one unit of risk
against `--take-profit-rr` units of reward. The source instead fills at the next bar's open; inside
a continuous session that is the same price give or take a tick, so the difference is small and not
reliably in either direction. The same one-bar offset applies to `--session-cutoff`, which flattens
at that bar's close where the source flattens at the next bar's open. There is no slippage knob,
because the source has no slippage input; reach for the
top-level `--commission-per-contract` instead.

`--chart-bar-type` has to be given. It defaults to the catalog bar type, which on this path is the
one-minute source the strategy never trades on; pointing it at the 15-minute stream is what puts the
fills on the bars that produced them.

Run VaultBreak, a long-only session breakout that buys a 30-minute bar closing above both a noise
boundary fixed once per day and the session VWAP. `--bar-type` must name a composite source: that
source is the one-minute bar magnifier the source material requires, and it is what lets a trade open
and close inside a single 30-minute bar.

```bash
uv run python main.py backtest \
  --start 2026-01-01 \
  --end 2026-05-30 \
  --chart-bar-type NQ.c.0.GLBX-30-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  vault-break run \
  --bar-type NQ.c.0.GLBX-30-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  --contracts 1
```

Each session fixes one level and then never moves it: the first traded bar of the day supplies the
open, and `--noise-multiple` of a `--boundary-atr-days` mean of completed session ranges is added to
it. Any bar in the entry window closing above both that boundary and the session VWAP is bought at
market, up to `--max-trades-per-day`, one position at a time. Exits are `--take-profit-points` above
and `--stop-loss-points` below the actual fill, plus a flatten at `--flatten-time`.

**Session times are Eastern, and so is the daily roll.** The source ships Central inputs and its own
header gives the Eastern equivalents. That is not cosmetic, because the roll anchors the session
open, the session range and the VWAP all at once: replaying the rules against the source's 43-trade
reference list reproduces 42 on an Eastern-keyed day, 39 on a Central-keyed one and 27 on a Globex
trade day.

**The bracket is a fixed point distance and `--contracts` only scales the dollars.** The source
expresses it as whole-position dollar amounts, which on NQ at one contract are exactly the 40 and 75
points shipped here but would tighten to 20 and 37.5 at two contracts. Points keep the levels put and
keep the instrument multiplier out of the arithmetic.

Nothing trades until `--boundary-atr-days` sessions have completed, so the sixteenth session of a run
is the first that can trade. Against the MultiCharts reference over 2026-04-22 → 2026-05-29 the port
reproduces **all 43 trades**, on the same signal bars, with the same exit reasons and an identical
$2,360 net; it adds three more, each clearing the boundary by under six points against a median
margin of 114. Over the full five months, though, it takes 125 trades and **loses $3,480** on one
contract: the 75 targets and 40 stops cancel to exactly zero and every dollar of the loss is the ten
flattens at `--flatten-time`. `--vwap-exit-bars` never fires at all. See [backtest.md](backtest.md)
for the full comparison and the numbers behind each claim.

Each backtest exports Nautilus order, order-fill, fill, position, and account reports as strategy-prefixed CSV files under `data/results/`. It also creates two interactive, self-contained HTML files:

- `<strategy>-tearsheet.html` contains run information, performance statistics, equity, drawdown, periodic returns, return distribution, and rolling Sharpe charts.
- `<strategy>-bars-with-fills.html` contains candlesticks with buy and sell fill markers.

The bars-with-fills chart uses the selected catalog bar type and retains its latest 10,000 bars by default. Set `--chart-bar-type` to chart another bar type cached during the run, such as the HTF composite bars produced by the HTF Sweep/CISD strategy. Composite inputs are resolved to the standard bar type under which Nautilus caches the generated bars. Set `--chart-bar-limit` to review a larger or smaller window, or pass `--no-visualize` to skip HTML generation:

```bash
uv run python main.py backtest \
  --start 2026-01-01 \
  --end 2026-06-01 \
  --chart-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  --chart-bar-limit 150000 \
  htf-sweep-cisd run \
  --htf-bar-type NQ.c.0.GLBX-15-MINUTE-LAST-INTERNAL@1-MINUTE-EXTERNAL \
  --ltf-bar-type NQ.c.0.GLBX-1-MINUTE-LAST-EXTERNAL \
  --risk-per-trade 1000
```

Run the same generic subscribe strategy against live Tradovate bars:

```bash
uv run python main.py live \
  --environment demo \
  subscribe run \
  --bar-type NQU6.TRADOVATE-1-MINUTE-LAST-EXTERNAL
```

The live adapter currently supports external LAST bars only. The example contract expires, so replace `NQU6` with a currently listed contract when necessary. Tradovate API-key **Market Data: Read Only** permission and an ordinary display-data subscription do not by themselves prove that CME non-display API data is enabled; `Symbol is inaccessible` for valid CME symbols must be resolved with Tradovate support. See [docs/Tradovate_API.md](docs/Tradovate_API.md) for the verified diagnostic and protocol details.

Run an active execution-adapter test using Databento bars and a separate Tradovate execution instrument:

```bash
uv run python main.py live \
  --environment demo \
  execute run \
  --bar-type MNQU6.GLBX-1-MINUTE-LAST-EXTERNAL \
  --instrument-id MNQU6.TRADOVATE \
  --case entry-exit \
  --quantity 1
```

This command subscribes to Databento bars, submits a Tradovate market buy, waits for its fill, and then submits a Tradovate market sell for the filled quantity. Omit `--instrument-id` to trade the instrument contained in `--bar-type`. It places real orders in the selected Tradovate environment. Wait for the `completed` log before stopping the node; an interruption between fills requires checking and flattening the account manually. The currently supported case is `entry-exit`.

Databento replaces only the market-data source. A Tradovate `401 Access is denied` response from `/order/placeorder` still means the authenticated Tradovate API user does not have permission to submit that order; verify order-write access and the selected demo account in the API-key configuration.

Run the generic subscribe strategy through Nautilus's built-in Databento adapter. The strategy first requests the instrument definition, then subscribes to its live one-minute bars:

```bash
uv run python main.py live \
  subscribe run \
  --bar-type MNQU6.GLBX-1-MINUTE-LAST-EXTERNAL
```

There is no provider selector. Nautilus routes `TRADOVATE` instruments to the venue-bound custom adapter and uses Databento as the default client for exchange venues such as `GLBX`. The live Databento client retains the dataset venue (`GLBX`) so its instrument IDs match the catalog and CLI examples. Replace `MNQU6` when that futures contract is no longer current.

The Databento API key must have a live `GLBX.MDP3` license. Historical access alone is
not sufficient: Nautilus can resolve the delayed historical instrument definition and
log `Subscribed bars`, while the live gateway still sends no records. Databento's
official client reports this state explicitly as
`A live data license is required to access GLBX.MDP3`.
