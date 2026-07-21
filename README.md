# Philipp Nautilus

NautilusTrader scaffold for backtesting experiments and live market-data adapters.

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
    └── subscribe.py     # Generic bar subscription command
docs/
├── agents/              # Work notes for resumability
│   └── strategy_visualization_options.md  # Charting options and recommendation
├── Strategy_fileHTFSweep+CISD.docx
├── Tradovate_API.md     # Sanitized and supplemented Tradovate API reference
└── strategy_flow.md     # Explicit HTF sweep + CISD strategy flow
src/phillip/adapters/tradovate/
├── data.py              # Bars-only Nautilus live data client
├── providers.py         # Futures instrument discovery
├── http/                # REST authentication and contract metadata
└── websocket/           # Framing, heartbeat, and reconnect handling
strategies/
├── base.py              # Shared live/backtest stratlet lifecycle
├── htf_sweep_cisd.py    # HTF sweep + CISD managed trade strategy
└── subscribe.py         # Subscribe to bars and log them
```

## Setup

This project uses `uv` for environment and dependency management.

```bash
uv venv --python 3.13
uv sync
```

Tradovate accepts username/password authentication. API-key authentication additionally requires the key name (`TRADOVATE_APP_ID`), key ID (`TRADOVATE_CID`), and key secret (`TRADOVATE_SEC`) as one complete bundle. Existing `TRADOVATE_ACCESS_TOKEN` and `TRADOVATE_MD_ACCESS_TOKEN` can be supplied together instead. Keep these values out of source control and logs.

The live node registers every provider for which credentials are present. Nautilus's built-in Databento adapter reads `DATABENTO_API_KEY`; Tradovate uses the credential forms described above.

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
  --stop-loss-distance-ratio 1.0 \
  --take-profit-multiplier 2.0
```

`--entry-limit-offset` and `--stop-limit-offset` are direct ratios, so `0.001` means `0.1%`. `--trade-notional` defaults to `1000`; the NQ example above uses `1000000` so contract sizing produces filled futures orders in the local backtest. Signal cooldown defaults to one HTF period and can be overridden with `--signal-cooldown-seconds`. The HTF sweep backtest uses hedging mode so concurrent entries retain separate positions.

Each backtest exports Nautilus order, order-fill, fill, position, and account reports as strategy-prefixed CSV files under `data/results/`.

Run the same generic subscribe strategy against live Tradovate bars:

```bash
uv run python main.py live \
  --environment demo \
  subscribe run \
  --bar-type NQU6.TRADOVATE-1-MINUTE-LAST-EXTERNAL
```

The live adapter currently supports external LAST bars only. The example contract expires, so replace `NQU6` with a currently listed contract when necessary. See [docs/Tradovate_API.md](docs/Tradovate_API.md) for protocol and configuration details.

Run the generic subscribe strategy through Nautilus's built-in Databento adapter. The strategy first requests the instrument definition, then subscribes to its live one-minute bars:

```bash
uv run python main.py live \
  subscribe run \
  --bar-type MNQU6.GLBX-1-MINUTE-LAST-EXTERNAL
```

There is no provider selector. Nautilus routes `TRADOVATE` instruments to the venue-bound custom adapter and uses Databento as the default client for exchange venues such as `GLBX`. Replace `MNQU6` when that futures contract is no longer current. Databento access also depends on the API key's entitlement to the `GLBX.MDP3` dataset.
