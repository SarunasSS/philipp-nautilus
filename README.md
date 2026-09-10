# Philipp Nautilus

NautilusTrader scaffold for backtesting experiments and live market-data/execution adapters.

## Repository Structure

```text
cli/
├── backtest/            # Run catalog-backed backtests
│   ├── __init__.py      # Shared backtest callback and runner helpers
│   ├── drift_pullback.py
│   ├── htf_sweep_cisd.py
│   └── subscribe.py
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
└── subscribe.py         # SubscribeStrategy requests and logs bars
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
