import typer


from datetime import datetime
from pathlib import Path
from typing import Annotated


from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog


from strategies.subscribe import SubscribeStrategy
from strategies.subscribe import SubscribeStrategyConfig


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
    ] = datetime(2026, 7, 1),
    catalog_path: Annotated[
        Path,
        typer.Option("--catalog-path", help="Nautilus ParquetDataCatalog path", envvar="DATA_CATALOG_PATH"),
    ] = Path("data/catalog"),
    bar_type: Annotated[
        str | None,
        typer.Option("--bar-type", help="Bar type to load. Defaults to the first catalog bar type in range."),
    ] = None,
    starting_balance: Annotated[
        str,
        typer.Option("--starting-balance", help="Starting balance for simulated venue"),
    ] = "100000 USD",
) -> None:
    if end <= start:
        raise typer.BadParameter("End date must be after start date")

    catalog = ParquetDataCatalog(catalog_path)
    requested_bar_types = [bar_type] if bar_type is not None else None
    bars = catalog.bars(bar_types=requested_bar_types, start=start, end=end)
    if not bars:
        raise typer.BadParameter(f"No bars found in {catalog_path} for {start.date()} -> {end.date()}")

    selected_bar_type = BarType.from_str(bar_type) if bar_type is not None else bars[0].bar_type
    bars = [bar for bar in bars if str(bar.bar_type) == str(selected_bar_type)]
    instruments = catalog.instruments(instrument_ids=[str(selected_bar_type.instrument_id)])
    if not instruments:
        raise typer.BadParameter(f"No instrument found in {catalog_path} for {selected_bar_type.instrument_id}")

    instrument = instruments[0]
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId(trader_id),
            logging=LoggingConfig(log_level=log_level),
        ),
    )
    typer.echo(f"Created Nautilus BacktestEngine for trader_id={trader_id}")

    try:
        engine.add_venue(
            venue=instrument.venue,
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money.from_str(starting_balance)],
            base_currency=None,
        )
        engine.add_instrument(instrument)
        engine.add_strategy(SubscribeStrategy(config=SubscribeStrategyConfig(bar_type=str(selected_bar_type))))
        engine.add_data(bars)
        engine.run(start=start, end=end)
        typer.echo(
            f"Backtest completed for {selected_bar_type} with {len(bars)} bar(s) "
            f"from {start.date()} -> {end.date()}.",
        )
    finally:
        engine.dispose()
        typer.echo("Backtest engine disposed.")
