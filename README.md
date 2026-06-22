# Philipp Nautilus

Minimal Nautilus Trader scaffold for backtesting experiments.

## Repository Structure

```text
cli/
├── backtest.py          # Run catalog-backed backtests
└── catalog.py           # Download Databento bars into the catalog
strategies/
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
uv run python main.py backtest run --start 2026-01-01 --end 2026-06-01
```

The catalog downloader reads `DATABENTO_API_KEY` from the environment. Defaults target `NQ.c.0` on `GLBX.MDP3` with Databento `ohlcv-1m` bars.
For continuous symbols, the downloader writes a matching continuous instrument entry so Nautilus can load `NQ.c.0.GLBX` bars for the backtest.
