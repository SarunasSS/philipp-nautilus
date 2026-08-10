# Philipp Nautilus

NautilusTrader scaffold for backtesting experiments and live market-data/execution adapters.

## Repository Structure

```text
cli/
├── backtest/            # Run catalog-backed backtests
│   ├── __init__.py      # Shared backtest callback and runner helpers
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
  --trade-notional 1000000 \
  --stop-order-type MARKET \
  --stop-loss-distance-ratio 1.0
```

`--entry-limit-offset` and `--stop-limit-offset` are direct ratios, so `0.001` means `0.1%`. `--trade-notional` defaults to `1000`; the NQ example above uses `1000000` so contract sizing produces filled futures orders in the local backtest. Signal cooldown defaults to one HTF period and can be overridden with `--signal-cooldown-seconds`. The HTF sweep backtest uses hedging mode so concurrent entries retain separate positions.

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
  --trade-notional 1000000
```

Run the same generic subscribe strategy against live Tradovate bars:

```bash
uv run python main.py live \
  --environment demo \
  subscribe run \
  --bar-type NQU6.TRADOVATE-1-MINUTE-LAST-EXTERNAL
```

The live adapter currently supports external LAST bars only. The example contract expires, so replace `NQU6` with a currently listed contract when necessary. Tradovate API-key **Market Data: Read Only** permission and an ordinary display-data subscription do not by themselves prove that CME non-display API data is enabled; `Symbol is inaccessible` for valid CME symbols must be resolved with Tradovate support. See [docs/Tradovate_API.md](docs/Tradovate_API.md) for the verified diagnostic and protocol details.

Run an active execution-adapter test while retaining the same instrument request and bar subscription behavior:

```bash
uv run python main.py live \
  --environment demo \
  execute run \
  --bar-type NQU6.TRADOVATE-1-MINUTE-LAST-EXTERNAL \
  --case entry-exit \
  --quantity 1
```

This command submits a market buy, waits for its fill, and then submits a market sell for the filled quantity. It places real orders in the selected Tradovate environment. Wait for the `completed` log before stopping the node; an interruption between fills requires checking and flattening the account manually. The currently supported case is `entry-exit`.

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
