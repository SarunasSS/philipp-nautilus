import typer


from typing import Annotated


from nautilus_trader.model.data import BarType

from cli.backtest import _get_backtest_settings
from cli.backtest import _run_backtest
from strategies.vwap_pullback_adx import RetestMode
from strategies.vwap_pullback_adx import StopMode
from strategies.vwap_pullback_adx import VwapPullbackAdxStrategy
from strategies.vwap_pullback_adx import VwapPullbackAdxStrategyConfig


cli = typer.Typer(help="Run VWAP Pullback + ADX Gate opening-range retest backtests")


@cli.command("run")
def _run_vwap_pullback_adx(
    ctx: typer.Context,
    bar_type: Annotated[
        str,
        typer.Option("--bar-type", help="One-minute bar type; a composite is loaded from its catalog source, which becomes the bar magnifier"),
    ],
    session_timezone: Annotated[
        str,
        typer.Option("--session-timezone", help="IANA timezone the session times and the daily roll follow"),
    ] = "America/New_York",
    opening_range_window: Annotated[
        str,
        typer.Option("--opening-range-window", help="Bars closing after the start and up to the end build the range"),
    ] = "09:30-10:00",
    vwap_window: Annotated[
        str,
        typer.Option("--vwap-window", help="Session VWAP accumulation window; start exclusive, end inclusive"),
    ] = "09:30-18:00",
    entry_window: Annotated[
        str,
        typer.Option("--entry-window", help="Window the trigger may fire in; start exclusive, end inclusive"),
    ] = "10:00-18:00",
    eod_flat: Annotated[
        bool,
        typer.Option("--eod-flat/--no-eod-flat", help="Flatten open positions at --eod-flat-time and refuse entries from then on"),
    ] = True,
    eod_flat_time: Annotated[
        str,
        typer.Option("--eod-flat-time", help="Forced flat time when --eod-flat is on"),
    ] = "16:55",
    retest_mode: Annotated[
        RetestMode,
        typer.Option("--retest-mode", help="TOUCH: a wick into the VWAP latches; CLOSE_THROUGH: a close through it latches"),
    ] = RetestMode.TOUCH,
    stop_mode: Annotated[
        StopMode,
        typer.Option("--stop-mode", help="PIVOT: last confirmed swing; PULLBACK_EXTREME: extreme printed since the latch"),
    ] = StopMode.PIVOT,
    stop_swing_length: Annotated[
        int,
        typer.Option("--stop-swing-length", help="Pivot strength for the stop, in bars each side"),
    ] = 20,
    target_swing_length: Annotated[
        int,
        typer.Option("--target-swing-length", help="Pivot strength for the target, in bars each side"),
    ] = 5,
    stop_buffer_points: Annotated[
        float,
        typer.Option("--stop-buffer-points", help="Extra distance beyond the stop level, in points"),
    ] = 0.0,
    max_trades_per_day: Annotated[
        int,
        typer.Option("--max-trades-per-day", help="Ceiling on entries per session, counted at the decision"),
    ] = 1,
    trade_longs: Annotated[
        bool,
        typer.Option("--trade-longs/--no-trade-longs", help="Allow long entries"),
    ] = True,
    trade_shorts: Annotated[
        bool,
        typer.Option("--trade-shorts/--no-trade-shorts", help="Allow short entries"),
    ] = False,
    min_rr: Annotated[
        float,
        typer.Option("--min-rr", help="Skip a trade whose target distance over stop distance is below this; 0 disables"),
    ] = 0.0,
    max_stop_points: Annotated[
        float,
        typer.Option("--max-stop-points", help="Skip a trade whose stop distance exceeds this many points; 0 disables"),
    ] = 0.0,
    use_adx_elevation: Annotated[
        bool,
        typer.Option("--use-adx-elevation/--no-use-adx-elevation", help="Require the ADX at or above --adx-elevation-threshold on the signal bar"),
    ] = True,
    adx_elevation_threshold: Annotated[
        float,
        typer.Option("--adx-elevation-threshold", help="ADX level the elevation gate requires"),
    ] = 20.0,
    use_adx_decay: Annotated[
        bool,
        typer.Option("--use-adx-decay/--no-use-adx-decay", help="Require the ADX not to have risen over --adx-decay-lookback bars"),
    ] = True,
    adx_decay_lookback: Annotated[
        int,
        typer.Option("--adx-decay-lookback", help="Bars back the decay gate compares the ADX against"),
    ] = 1,
    adx_length: Annotated[
        int,
        typer.Option("--adx-length", help="Wilder ADX period, in bars"),
    ] = 14,
    contracts: Annotated[
        int,
        typer.Option("--contracts", help="Fixed position size in contracts"),
    ] = 1,
) -> None:
    try:
        parsed_bar_type = BarType.from_str(bar_type)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid bar type: {bar_type}", param_hint="--bar-type") from exc

    # Built here rather than inside the runner callback so an invalid combination is rejected before
    # a 1-second run spends minutes loading data it will not use.
    config = VwapPullbackAdxStrategyConfig(
        bar_type=bar_type,
        session_timezone=session_timezone,
        opening_range_window=opening_range_window,
        vwap_window=vwap_window,
        entry_window=entry_window,
        eod_flat=eod_flat,
        eod_flat_time=eod_flat_time,
        retest_mode=retest_mode.value,
        stop_mode=stop_mode.value,
        stop_swing_length=stop_swing_length,
        target_swing_length=target_swing_length,
        stop_buffer_points=stop_buffer_points,
        max_trades_per_day=max_trades_per_day,
        trade_longs=trade_longs,
        trade_shorts=trade_shorts,
        min_rr=min_rr,
        max_stop_points=max_stop_points,
        use_adx_elevation=use_adx_elevation,
        adx_elevation_threshold=adx_elevation_threshold,
        use_adx_decay=use_adx_decay,
        adx_decay_lookback=adx_decay_lookback,
        adx_length=adx_length,
        contracts=contracts,
    )
    settings = _get_backtest_settings(ctx)

    # A plain bar type is loaded and matched as itself; a composite loads its source, and the venue
    # then checks the bracket against that finer stream.
    requested_bar_type = str(parsed_bar_type.composite()) if parsed_bar_type.is_composite() else bar_type
    typer.echo(f"Bar-fed run: loading {requested_bar_type} from the catalog")
    _run_backtest(
        settings=settings,
        requested_bar_type=requested_bar_type,
        strategy_name="vwap-pullback-adx",
        create_strategy=lambda _selected_bar_type: VwapPullbackAdxStrategy(config=config),
    )
