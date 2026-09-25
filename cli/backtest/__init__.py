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
from nautilus_trader.backtest.models import PerContractFeeModel
from nautilus_trader.config import CacheConfig
from nautilus_trader.config import LoggingConfig
from nautilus_trader.core.data import Data
from nautilus_trader.model.currencies import USD
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
    commission_per_contract: float
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
        typer.Option("--starting-balance", help="Starting balance for each simulated venue"),
    ] = "100000 USD",
    commission_per_contract: Annotated[
        float,
        typer.Option(
            "--commission-per-contract",
            min=0.0,
            help="Commission charged per contract per side; 0 leaves the venue with no fee model",
        ),
    ] = 0.0,
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
        commission_per_contract=commission_per_contract,
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
    catalog: ParquetDataCatalog,
    bar_types: list[BarType] | None,
    trade_tick_instrument_ids: list[str] | None,
    quote_tick_instrument_ids: list[str] | None,
) -> list[list[Data]]:
    if not bar_types and not trade_tick_instrument_ids and not quote_tick_instrument_ids:
        raise typer.BadParameter("At least one bar, trade tick, or quote tick stream must be requested")

    data: list[list[Data]] = []
    catalog_bar_types = dict.fromkeys(
        bar_type.composite() if bar_type.is_composite() else bar_type for bar_type in bar_types or []
    )
    for bar_type in catalog_bar_types:
        bars = catalog.bars(bar_types=[str(bar_type)], start=settings.start, end=settings.end)
        if not bars:
            raise typer.BadParameter(
                f"No bars found in {settings.catalog_path} for {bar_type} "
                f"{settings.start.date()} -> {settings.end.date()}",
                param_hint="--bar-type",
            )
        data.append(bars)

    for instrument_id in dict.fromkeys(quote_tick_instrument_ids or []):
        quotes = catalog.quote_ticks(instrument_ids=[instrument_id], start=settings.start, end=settings.end)
        if not quotes:
            raise typer.BadParameter(
                f"No quote ticks found in {settings.catalog_path} for {instrument_id} "
                f"{settings.start.date()} -> {settings.end.date()}",
            )
        data.append(quotes)

    for instrument_id in dict.fromkeys(trade_tick_instrument_ids or []):
        trades = catalog.trade_ticks(instrument_ids=[instrument_id], start=settings.start, end=settings.end)
        if not trades:
            raise typer.BadParameter(
                f"No trade ticks found in {settings.catalog_path} for {instrument_id} "
                f"{settings.start.date()} -> {settings.end.date()}",
            )
        data.append(trades)

    return data


def _run_backtest(
    settings: BacktestRunSettings,
    bar_types: list[BarType] | None,
    strategy_name: str,
    create_strategy: Callable[[BarType | None], Strategy],
    oms_type: OmsType = OmsType.HEDGING,
    trade_tick_instrument_ids: list[str] | None = None,
    quote_tick_instrument_ids: list[str] | None = None,
) -> None:
    catalog = ParquetDataCatalog(settings.catalog_path)
    data = _load_backtest_data(
        settings,
        catalog,
        bar_types,
        trade_tick_instrument_ids,
        quote_tick_instrument_ids,
    )
    selected_bar_type = bar_types[0] if bar_types else None
    instrument_ids = dict.fromkeys(
        [str(bar_type.instrument_id) for bar_type in bar_types or []]
        + (trade_tick_instrument_ids or [])
        + (quote_tick_instrument_ids or []),
    )
    instruments: list[Instrument] = []
    for instrument_id in instrument_ids:
        matches = catalog.instruments(instrument_ids=[instrument_id])
        if len(matches) != 1:
            raise typer.BadParameter(
                f"Expected one instrument in {settings.catalog_path} for {instrument_id}, "
                f"found {len(matches)}",
            )
        instruments.append(matches[0])

    bar_count = sum(len(stream) for stream in data if isinstance(stream[0], Bar))
    tick_count = sum(len(stream) for stream in data if not isinstance(stream[0], Bar))
    config_options: dict[str, CacheConfig] = {}
    if settings.visualize and selected_bar_type is not None and settings.chart_bar_limit != 10_000:
        # The chart reads cached bars after the run. Override the default 10,000
        # only for a custom chart window, preserving the backtest reset default.
        config_options["cache"] = CacheConfig(
            bar_capacity=settings.chart_bar_limit,
            drop_instruments_on_reset=False,
        )

    engine = BacktestEngine(
        config=BacktestEngineConfig(
            trader_id=TraderId(settings.trader_id),
            logging=LoggingConfig(log_level=settings.log_level),
            **config_options,
        ),
    )
    typer.echo(f"Created Nautilus BacktestEngine for trader_id={settings.trader_id}")

    try:
        venues = dict.fromkeys(instrument.venue for instrument in instruments)
        for venue in venues:
            engine.add_venue(
                venue=venue,
                oms_type=oms_type,
                account_type=AccountType.MARGIN,
                starting_balances=[Money.from_str(settings.starting_balance)],
                base_currency=None,
                fee_model=(
                    PerContractFeeModel(Money(settings.commission_per_contract, USD))
                    if settings.commission_per_contract > 0
                    else None
                ),
            )
        for instrument in instruments:
            engine.add_instrument(instrument)
        engine.add_strategy(create_strategy(selected_bar_type))

        for stream in data:
            engine.add_data(stream, sort=False)
        engine.sort_data()
        engine.run(start=settings.start, end=settings.end)

        results_path = Path("data/results")
        results_path.mkdir(parents=True, exist_ok=True)
        reports = {
            "orders": engine.trader.generate_orders_report(),
            "order-fills": engine.trader.generate_order_fills_report(),
            "fills": engine.trader.generate_fills_report(),
            "positions": engine.trader.generate_positions_report(),
        }
        for venue in venues:
            report_name = "account" if len(venues) == 1 else f"account-{venue}"
            reports[report_name] = engine.trader.generate_account_report(venue=venue).rename_axis("ts_event")
        for report_name, report in reports.items():
            report.to_csv(results_path / f"{strategy_name}-{report_name}.csv")

        visualization_paths: list[Path] = []
        if settings.visualize and selected_bar_type is not None:
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

        data_description = (
            f"{bar_count} bar(s), {tick_count} tick(s) "
            f"across {len(instruments)} instrument(s)"
        )
        typer.echo(
            f"Backtest completed for {strategy_name} on {data_description} "
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


from cli.backtest.drift_pullback import cli as drift_pullback_cli
from cli.backtest.htf_sweep_cisd import cli as htf_sweep_cisd_cli
from cli.backtest.overnight_bias_orb import cli as overnight_bias_orb_cli
from cli.backtest.subscribe import cli as subscribe_cli
from cli.backtest.vault_break import cli as vault_break_cli
from cli.backtest.vwap_pullback_adx import cli as vwap_pullback_adx_cli


cli.add_typer(subscribe_cli, name="subscribe")
cli.add_typer(drift_pullback_cli, name="drift-pullback")
cli.add_typer(htf_sweep_cisd_cli, name="htf-sweep-cisd")
cli.add_typer(overnight_bias_orb_cli, name="overnight-bias-orb")
cli.add_typer(vault_break_cli, name="vault-break")
cli.add_typer(vwap_pullback_adx_cli, name="vwap-pullback-adx")
