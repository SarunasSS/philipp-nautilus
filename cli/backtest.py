import typer


from datetime import datetime
from typing import Annotated


from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.identifiers import TraderId


cli = typer.Typer(help="Run Nautilus Trader backtests")


@cli.command()
def run(
    trader_id: Annotated[
        str,
        typer.Option("--trader-id", help="Nautilus trader ID", envvar="TRADER_ID"),
    ] = "PHILIPP-001",
    log_level: Annotated[
        str,
        typer.Option("--log-level", help="Console log level", envvar="LOG_LEVEL"),
    ] = "INFO",
    start: Annotated[
        datetime,
        typer.Option("--start", help="Backtest start date"),
    ] = datetime(2026, 1, 1),
    end: Annotated[
        datetime,
        typer.Option("--end", help="Backtest end date"),
    ] = datetime(2026, 2, 1),
) -> None:
    if end <= start:
        raise typer.BadParameter("End date must be after start date")

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId(trader_id),
            logging=LoggingConfig(log_level=log_level),
        ),
    )
    typer.echo(f"Created Nautilus BacktestEngine for trader_id={trader_id}")

    try:
        engine.run(start=start, end=end)
        typer.echo(f"Backtest engine run completed with no data loaded for {start.date()} -> {end.date()}.")
    finally:
        engine.dispose()
        typer.echo("Backtest engine disposed.")
