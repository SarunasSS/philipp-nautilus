import typer


from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated


from nautilus_trader.analysis import create_tearsheet
from nautilus_trader.analysis.tearsheet import create_bars_with_fills
from nautilus_trader.backtest.config import BacktestEngineConfig
from nautilus_trader.backtest.engine import BacktestEngine
from nautilus_trader.config import CacheConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import AccountType
from nautilus_trader.model.enums import OmsType
from nautilus_trader.model.identifiers import TraderId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Money
from nautilus_trader.persistence.catalog import ParquetDataCatalog
from nautilus_trader.trading.strategy import Strategy


cli = typer.Typer(help="Run Nautilus Trader backtests")


@dataclass(frozen=True)
class BacktestRunSettings:
    trader_id: str
    log_level: str
    start: datetime
    end: datetime
    catalog_path: Path
    starting_balance: str
    visualize: bool
    chart_bar_type: str | None
    chart_bar_limit: int


@cli.callback()
def _configure_backtest(
    ctx: typer.Context,
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
    starting_balance: Annotated[
        str,
        typer.Option("--starting-balance", help="Starting balance for simulated venue"),
    ] = "100000 USD",
    visualize: Annotated[
        bool,
        typer.Option("--visualize/--no-visualize", help="Export Nautilus interactive HTML visualizations"),
    ] = True,
    chart_bar_type: Annotated[
        str | None,
        typer.Option(
            "--chart-bar-type",
            help="Bar type for the bars-with-fills chart; defaults to the selected catalog bar type",
        ),
    ] = None,
    chart_bar_limit: Annotated[
        int,
        typer.Option(
            "--chart-bar-limit",
            min=1,
            help="Maximum number of recent bars retained for the bars-with-fills chart",
        ),
    ] = 10_000,
) -> None:
    if end <= start:
        raise typer.BadParameter("End date must be after start date")

    ctx.obj = BacktestRunSettings(
        trader_id=trader_id,
        log_level=log_level,
        start=start,
        end=end,
        catalog_path=catalog_path,
        starting_balance=starting_balance,
        visualize=visualize,
        chart_bar_type=chart_bar_type,
        chart_bar_limit=chart_bar_limit,
    )


def _get_backtest_settings(ctx: typer.Context) -> BacktestRunSettings:
    settings = ctx.find_object(BacktestRunSettings)
    if settings is None:
        raise RuntimeError("Backtest settings were not configured")

    return settings


def _load_backtest_data(
    settings: BacktestRunSettings,
    requested_bar_type: str | None,
) -> tuple[BarType, list[Bar], Instrument]:
    catalog = ParquetDataCatalog(settings.catalog_path)
    requested_bar_types = [requested_bar_type] if requested_bar_type is not None else None
    bars = catalog.bars(bar_types=requested_bar_types, start=settings.start, end=settings.end)
    if not bars:
        raise typer.BadParameter(
            f"No bars found in {settings.catalog_path} for {settings.start.date()} -> {settings.end.date()}",
        )

    selected_bar_type = BarType.from_str(requested_bar_type) if requested_bar_type is not None else bars[0].bar_type
    bars = [bar for bar in bars if str(bar.bar_type) == str(selected_bar_type)]
    instruments = catalog.instruments(instrument_ids=[str(selected_bar_type.instrument_id)])
    if not instruments:
        raise typer.BadParameter(f"No instrument found in {settings.catalog_path} for {selected_bar_type.instrument_id}")

    return selected_bar_type, bars, instruments[0]


def _run_backtest(
    settings: BacktestRunSettings,
    requested_bar_type: str | None,
    strategy_name: str,
    create_strategy: Callable[[BarType], Strategy],
    oms_type: OmsType = OmsType.NETTING,
) -> None:
    selected_bar_type, bars, instrument = _load_backtest_data(settings, requested_bar_type)
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId(settings.trader_id),
            logging=LoggingConfig(log_level=settings.log_level),
            cache=CacheConfig(
                bar_capacity=min(len(bars), settings.chart_bar_limit) if settings.visualize else 10_000,
            ),
        ),
    )
    typer.echo(f"Created Nautilus BacktestEngine for trader_id={settings.trader_id}")

    try:
        engine.add_venue(
            venue=instrument.venue,
            oms_type=oms_type,
            account_type=AccountType.MARGIN,
            starting_balances=[Money.from_str(settings.starting_balance)],
            base_currency=None,
        )
        engine.add_instrument(instrument)
        engine.add_strategy(create_strategy(selected_bar_type))
        engine.add_data(bars)
        engine.run(start=settings.start, end=settings.end)

        results_path = Path("data/results")
        results_path.mkdir(parents=True, exist_ok=True)
        reports = {
            "orders": engine.trader.generate_orders_report(),
            "order-fills": engine.trader.generate_order_fills_report(),
            "fills": engine.trader.generate_fills_report(),
            "positions": engine.trader.generate_positions_report(),
            "account": engine.trader.generate_account_report(venue=instrument.venue).rename_axis("ts_event"),
        }
        for report_name, report in reports.items():
            report.to_csv(results_path / f"{strategy_name}-{report_name}.csv")

        visualization_paths: list[Path] = []
        if settings.visualize:
            try:
                chart_bar_type = (
                    BarType.from_str(settings.chart_bar_type)
                    if settings.chart_bar_type is not None
                    else selected_bar_type
                ).standard()
            except ValueError as exc:
                raise typer.BadParameter(
                    f"Invalid chart bar type: {settings.chart_bar_type}",
                    param_hint="--chart-bar-type",
                ) from exc

            chart_bars = engine.cache.bars(chart_bar_type)
            if not chart_bars:
                available_bar_types = ", ".join(str(bar_type) for bar_type in engine.cache.bar_types())
                raise typer.BadParameter(
                    f"No cached bars found for {chart_bar_type}. Available bar types: "
                    f"{available_bar_types or 'none'}",
                    param_hint="--chart-bar-type",
                )

            tearsheet_path = results_path / f"{strategy_name}-tearsheet.html"
            bars_with_fills_path = results_path / f"{strategy_name}-bars-with-fills.html"
            create_tearsheet(
                engine=engine,
                output_path=str(tearsheet_path),
                title=f"{strategy_name} backtest results",
            )
            create_bars_with_fills(
                engine=engine,
                bar_type=chart_bar_type,
                output_path=str(bars_with_fills_path),
                title=f"{strategy_name}: {chart_bar_type}",
            )
            visualization_paths.extend((tearsheet_path, bars_with_fills_path))

        typer.echo(
            f"Backtest completed for {strategy_name} on {selected_bar_type} with {len(bars)} bar(s) "
            f"from {settings.start.date()} -> {settings.end.date()}.",
        )
        typer.echo(f"Exported {len(reports)} reports to {results_path}.")
        if visualization_paths:
            typer.echo(
                f"Exported {len(visualization_paths)} visualizations to {results_path}; "
                f"bars-with-fills contains {len(chart_bars)} {chart_bar_type} bar(s).",
            )
    finally:
        engine.dispose()
        typer.echo("Backtest engine disposed.")


from cli.backtest.htf_sweep_cisd import cli as htf_sweep_cisd_cli
from cli.backtest.subscribe import cli as subscribe_cli


cli.add_typer(subscribe_cli, name="subscribe")
cli.add_typer(htf_sweep_cisd_cli, name="htf-sweep-cisd")
