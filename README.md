# Philipp Nautilus

Minimal Nautilus Trader scaffold for backtesting experiments.

## Repository Structure

```text
cli/
└── backtest.py          # Typer command for the minimal backtest run
```

## Setup

This project uses `uv` for environment and dependency management.

```bash
uv venv --python 3.13
uv sync
```

## Run

```bash
uv run python main.py backtest run --start 2026-01-01 --end 2026-02-01
```

The current backtest creates a Nautilus `BacktestEngine`, runs the requested date range with no data, and disposes it. Add venues, instruments, data, and strategies as the next functional layer.
