import typer


from typing import Annotated


from nautilus_trader.model.data import BarType

from cli.backtest import _get_backtest_settings
from cli.backtest import _run_backtest
from strategies.drift_pullback import DriftPullbackStrategy
from strategies.drift_pullback import DriftPullbackStrategyConfig
from strategies.drift_pullback import VwapSource


cli = typer.Typer(help="Run DriftPullback intraday drift continuation backtests")


@cli.command("run")
def _run_drift_pullback(
    ctx: typer.Context,
    bar_type: Annotated[
        BarType,
        typer.Option("--bar-type", parser=BarType.from_str, help="Execution bar type the pullback is triggered on"),
    ],
    signal_bar_type: Annotated[
        BarType,
        typer.Option(
            "--signal-bar-type",
            parser=BarType.from_str,
            help="Signal bar type the anchor, drift and arming are frozen on",
        ),
    ],
    vwap_bar_type: Annotated[
        BarType | None,
        typer.Option(
            "--vwap-bar-type",
            parser=BarType.from_str,
            help="Bar type the session VWAP accumulates on; keep at one minute to match live",
        ),
    ] = None,
    vwap_source: Annotated[
        VwapSource,
        typer.Option("--vwap-source", help="Accumulate the anchor from a bar stream, or from raw prints on a tick run"),
    ] = VwapSource.BARS,
    session_timezone: Annotated[
        str,
        typer.Option("--session-timezone", help="IANA timezone the session times are expressed in"),
    ] = "America/New_York",
    entry_window: Annotated[
        str,
        typer.Option("--entry-window", help="Window entries may be triggered in; start inclusive, end exclusive"),
    ] = "10:30-15:30",
    session_cutoff: Annotated[
        str,
        typer.Option("--session-cutoff", help="Time open positions are flattened and unfilled entries canceled"),
    ] = "15:55",
    vwap_window: Annotated[
        str,
        typer.Option("--vwap-window", help="Session VWAP accumulation window; start exclusive, end inclusive"),
    ] = "09:30-16:00",
    require_full_vwap_session: Annotated[
        bool,
        typer.Option(
            "--require-full-vwap-session/--no-require-full-vwap-session",
            help="Refuse to arm when accumulation began after the VWAP window opened",
        ),
    ] = True,
    drift_lookback_bars: Annotated[
        int,
        typer.Option("--drift-lookback-bars", help="Signal bars back the drift is measured against"),
    ] = 4,
    drift_threshold_pct: Annotated[
        float,
        typer.Option("--drift-threshold-pct", help="Minimum absolute drift percentage to arm; 0 disables"),
    ] = 0.10,
    pullback_window_bars: Annotated[
        int,
        typer.Option("--pullback-window-bars", help="Execution bars an armed setup survives, counting the arming bar"),
    ] = 6,
    trade_longs: Annotated[
        bool,
        typer.Option("--trade-longs/--no-trade-longs", help="Allow long entries"),
    ] = True,
    trade_shorts: Annotated[
        bool,
        typer.Option("--trade-shorts/--no-trade-shorts", help="Allow short entries"),
    ] = True,
    long_stop_points: Annotated[
        float,
        typer.Option("--long-stop-points", help="Long stop distance below the fill, in points"),
    ] = 80.0,
    long_target_points: Annotated[
        float,
        typer.Option("--long-target-points", help="Long target distance above the fill, in points"),
    ] = 40.0,
    short_stop_points: Annotated[
        float,
        typer.Option("--short-stop-points", help="Short stop distance above the fill, in points"),
    ] = 80.0,
    short_target_points: Annotated[
        float,
        typer.Option("--short-target-points", help="Short target distance below the fill, in points"),
    ] = 50.0,
    stop_slip_ticks: Annotated[
        int,
        typer.Option("--stop-slip-ticks", help="Adverse ticks added to the stop distance only; 0 disables"),
    ] = 2,
    max_trades_per_day: Annotated[
        int,
        typer.Option("--max-trades-per-day", help="Ceiling on entries per session"),
    ] = 4,
    max_losses_per_day: Annotated[
        int,
        typer.Option("--max-losses-per-day", help="Losing round trips after which the session stops trading"),
    ] = 2,
    contracts: Annotated[
        int,
        typer.Option("--contracts", help="Fixed position size in contracts"),
    ] = 1,
) -> None:
    # Built here rather than inside the runner callback so an invalid combination is rejected before
    # a tick run spends minutes loading data it will not use.
    config = DriftPullbackStrategyConfig(
        bar_type=str(bar_type),
        signal_bar_type=str(signal_bar_type),
        vwap_bar_type=str(vwap_bar_type) if vwap_bar_type is not None else None,
        vwap_source=vwap_source.value,
        session_timezone=session_timezone,
        entry_window=entry_window,
        session_cutoff=session_cutoff,
        vwap_window=vwap_window,
        require_full_vwap_session=require_full_vwap_session,
        drift_lookback_bars=drift_lookback_bars,
        drift_threshold_pct=drift_threshold_pct,
        pullback_window_bars=pullback_window_bars,
        trade_longs=trade_longs,
        trade_shorts=trade_shorts,
        long_stop_points=long_stop_points,
        long_target_points=long_target_points,
        short_stop_points=short_stop_points,
        short_target_points=short_target_points,
        stop_slip_ticks=stop_slip_ticks,
        max_trades_per_day=max_trades_per_day,
        max_losses_per_day=max_losses_per_day,
        contracts=contracts,
    )
    settings = _get_backtest_settings(ctx)

    # A bar type naming a composite source is built from a bar catalog; one without a source can only
    # be aggregated from prints. The bar type therefore selects the data path on its own, and no
    # separate flag exists to contradict it.
    if bar_type.is_composite():
        typer.echo(f"Bar-fed run: loading {bar_type.composite()} from the catalog")
        _run_backtest(
            settings=settings,
            bar_types=[
                requested_bar_type
                for requested_bar_type in (bar_type, signal_bar_type, vwap_bar_type)
                if requested_bar_type is not None
            ],
            strategy_name="drift-pullback",
            create_strategy=lambda _selected_bar_type: DriftPullbackStrategy(config=config),
        )

        return

    typer.echo(
        f"Tick-fed run: loading quote and trade ticks for {bar_type.instrument_id}; "
        f"every bar stream is aggregated from them, anchor from {vwap_source.value}",
    )
    _run_backtest(
        settings=settings,
        bar_types=None,
        strategy_name="drift-pullback",
        create_strategy=lambda _selected_bar_type: DriftPullbackStrategy(config=config),
        trade_tick_instrument_ids=[str(bar_type.instrument_id)],
        quote_tick_instrument_ids=[str(bar_type.instrument_id)],
    )
