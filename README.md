# Philipp Nautilus

Minimal Nautilus Trader scaffold for backtesting experiments.

## Repository Structure

```text
cli/
├── backtest/            # Run catalog-backed backtests
│   ├── __init__.py      # Shared backtest callback and runner helpers
│   ├── htf_sweep_cisd.py
│   └── subscribe.py
└── catalog.py           # Download Databento bars into the catalog
docs/
├── agents/              # Work notes for resumability
│   └── strategy_visualization_options.md  # Charting options and recommendation
├── Strategy_fileHTFSweep+CISD.docx
└── strategy_flow.md     # Explicit HTF sweep + CISD strategy flow
strategies/
├── htf_sweep_cisd.py    # HTF sweep + CISD managed trade strategy
└── subscribe.py         # Subscribe to bars and log them
```

## Setup

This project uses `uv` for environment and dependency management.

```bash
uv venv --python 3.13
uv sync
```

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

Each backtest exports Nautilus order, order-fill, fill, position, and account reports as strategy-prefixed CSV files under `data/results/`.

The catalog downloader reads `DATABENTO_API_KEY` from the environment. Defaults target `NQ.c.0` on `GLBX.MDP3` with Databento `ohlcv-1m` bars.
For continuous symbols, the downloader writes a matching continuous instrument entry so Nautilus can load `NQ.c.0.GLBX` bars for the backtest.

## Strategy Visualization

The investigated route is to export chart-neutral strategy events, generate a bounded Nautilus/Plotly HTML chart first, and reuse the same event contract in TradingView Lightweight Charts only if a live trading viewer is needed. See `docs/agents/strategy_visualization_options.md` for the comparison and decision criteria.
