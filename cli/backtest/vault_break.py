import typer


from typing import Annotated


from nautilus_trader.model.data import BarType

from cli.backtest import _get_backtest_settings
from cli.backtest import _run_backtest
from strategies.vault_break import VaultBreakStrategy
from strategies.vault_break import VaultBreakStrategyConfig


cli = typer.Typer(help="Run VaultBreak session noise breakout backtests")


@cli.command("run")
def _run_vault_break(
    ctx: typer.Context,
    bar_type: Annotated[
        str,
        typer.Option("--bar-type", help="Signal bar type; its composite source is the bar magnifier the exits need"),
    ],
    session_timezone: Annotated[
        str,
        typer.Option("--session-timezone", help="IANA timezone the session times and the daily roll follow"),
    ] = "America/New_York",
    earliest_entry: Annotated[
        str,
        typer.Option("--earliest-entry", help="Earliest bar close that may trigger an entry"),
    ] = "11:00",
    flatten_time: Annotated[
        str,
        typer.Option("--flatten-time", help="Entry window ends here, and open positions are flattened"),
    ] = "15:30",
    noise_multiple: Annotated[
        float,
        typer.Option("--noise-multiple", help="Noise boundary above the session open, in boundary ranges"),
    ] = 0.3,
    boundary_atr_days: Annotated[
        int,
        typer.Option("--boundary-atr-days", help="Completed sessions the boundary range averages; nothing trades until it fills"),
    ] = 15,
    max_trades_per_day: Annotated[
        int,
        typer.Option("--max-trades-per-day", help="Ceiling on entries per session; only ever one position at a time"),
    ] = 3,
    vwap_exit_bars: Annotated[
        int,
        typer.Option("--vwap-exit-bars", help="Consecutive closes below the session VWAP that flatten an open trade"),
    ] = 7,
    take_profit_points: Annotated[
        float,
        typer.Option("--take-profit-points", help="Target distance above the fill, in points"),
    ] = 40.0,
    stop_loss_points: Annotated[
        float,
        typer.Option("--stop-loss-points", help="Stop distance below the fill, in points"),
    ] = 75.0,
    contracts: Annotated[
        int,
        typer.Option("--contracts", help="Fixed position size; scales dollar risk without moving either level"),
    ] = 1,
) -> None:
    try:
        parsed_bar_type = BarType.from_str(bar_type)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid bar type: {bar_type}", param_hint="--bar-type") from exc

    config = VaultBreakStrategyConfig(
        bar_type=bar_type,
        session_timezone=session_timezone,
        earliest_entry=earliest_entry,
        flatten_time=flatten_time,
        noise_multiple=noise_multiple,
        boundary_atr_days=boundary_atr_days,
        max_trades_per_day=max_trades_per_day,
        vwap_exit_bars=vwap_exit_bars,
        take_profit_points=take_profit_points,
        stop_loss_points=stop_loss_points,
        contracts=contracts,
    )
    settings = _get_backtest_settings(ctx)

    # The strategy refuses a bar type with no composite source, because that source is the bar
    # magnifier its exits are matched against. It is loaded here and aggregated by the engine.
    typer.echo(f"Bar-fed run: loading {parsed_bar_type.composite()} from the catalog")
    _run_backtest(
        settings=settings,
        requested_bar_type=str(parsed_bar_type.composite()),
        strategy_name="vault-break",
        create_strategy=lambda _selected_bar_type: VaultBreakStrategy(config=config),
    )
