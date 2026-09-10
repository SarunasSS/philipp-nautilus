import typer


from typing import Annotated


from nautilus_trader.model.data import BarType

from cli.backtest import _get_backtest_settings
from cli.backtest import _run_backtest
from strategies.overnight_bias_orb import OvernightBiasORBStrategy
from strategies.overnight_bias_orb import OvernightBiasORBStrategyConfig


cli = typer.Typer(help="Run OvernightBiasORB opening range breakout backtests")


@cli.command("run")
def _run_overnight_bias_orb(
    ctx: typer.Context,
    bar_type: Annotated[
        str,
        typer.Option("--bar-type", help="Signal bar type; its composite source is the bar magnifier the exits need"),
    ],
    session_timezone: Annotated[
        str,
        typer.Option("--session-timezone", help="IANA timezone the session times are expressed in"),
    ] = "America/New_York",
    session_start: Annotated[
        str,
        typer.Option("--session-start", help="Regular-hours open; starts the opening range and ends the overnight range"),
    ] = "09:30",
    opening_range_end: Annotated[
        str,
        typer.Option("--opening-range-end", help="Opening range locks here; breakouts are only tested after it"),
    ] = "09:45",
    last_entry: Annotated[
        str,
        typer.Option("--last-entry", help="Latest bar close that may trigger an entry"),
    ] = "13:00",
    session_cutoff: Annotated[
        str,
        typer.Option("--session-cutoff", help="Time open positions are flattened and unfilled entries canceled"),
    ] = "15:30",
    session_end: Annotated[
        str,
        typer.Option("--session-end", help="Regular-hours close; bounds the true range reference's day"),
    ] = "16:00",
    max_trades_per_day: Annotated[
        int,
        typer.Option("--max-trades-per-day", help="Ceiling on entries per session; only ever one position at a time"),
    ] = 4,
    bias_long_min: Annotated[
        float,
        typer.Option("--bias-long-min", help="Open at or above this much of the overnight range makes a long-only day"),
    ] = 0.67,
    bias_short_max: Annotated[
        float,
        typer.Option("--bias-short-max", help="Open at or below this much of the overnight range makes a short-only day"),
    ] = 0.33,
    adx_length: Annotated[
        int,
        typer.Option("--adx-length", help="Period of the Wilder ADX trend-strength gate"),
    ] = 16,
    adx_min: Annotated[
        float,
        typer.Option("--adx-min", help="Minimum ADX to trade; 0 disables the gate"),
    ] = 20.0,
    break_skip_multiple: Annotated[
        float,
        typer.Option("--break-skip-multiple", help="Skip breaks on bars wider than this many prior average ranges"),
    ] = 2.5,
    bar_average_length: Annotated[
        int,
        typer.Option("--bar-average-length", help="Bars the prior average range is measured over, excluding the current one"),
    ] = 20,
    min_or_multiple: Annotated[
        float,
        typer.Option("--min-or-multiple", help="Lower bound of the opening-range size band, in reference ranges"),
    ] = 0.0,
    max_or_multiple: Annotated[
        float,
        typer.Option("--max-or-multiple", help="Upper bound of the opening-range size band, in reference ranges"),
    ] = 2.0,
    or_median_days: Annotated[
        int,
        typer.Option("--or-median-days", help="Sessions the opening-range reference smooths over; the band is off until then"),
    ] = 20,
    atr_multiple: Annotated[
        float,
        typer.Option("--atr-multiple", help="Stop distance as a fraction of the daily true range reference"),
    ] = 0.30,
    atr_days: Annotated[
        int,
        typer.Option("--atr-days", help="Sessions the true range reference averages; nothing trades until it fills"),
    ] = 15,
    take_profit_rr: Annotated[
        float,
        typer.Option("--take-profit-rr", help="Target distance as a multiple of the stop distance"),
    ] = 3.0,
    contracts: Annotated[
        int,
        typer.Option("--contracts", help="Fixed position size in contracts"),
    ] = 1,
) -> None:
    try:
        parsed_bar_type = BarType.from_str(bar_type)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid bar type: {bar_type}", param_hint="--bar-type") from exc

    config = OvernightBiasORBStrategyConfig(
        bar_type=bar_type,
        session_timezone=session_timezone,
        session_start=session_start,
        opening_range_end=opening_range_end,
        last_entry=last_entry,
        session_cutoff=session_cutoff,
        session_end=session_end,
        max_trades_per_day=max_trades_per_day,
        bias_long_min=bias_long_min,
        bias_short_max=bias_short_max,
        adx_length=adx_length,
        adx_min=adx_min,
        break_skip_multiple=break_skip_multiple,
        bar_average_length=bar_average_length,
        min_or_multiple=min_or_multiple,
        max_or_multiple=max_or_multiple,
        or_median_days=or_median_days,
        atr_multiple=atr_multiple,
        atr_days=atr_days,
        take_profit_rr=take_profit_rr,
        contracts=contracts,
    )
    settings = _get_backtest_settings(ctx)

    # The strategy refuses a bar type with no composite source, because that source is the bar
    # magnifier its exits are matched against. It is loaded here and aggregated by the engine.
    typer.echo(f"Bar-fed run: loading {parsed_bar_type.composite()} from the catalog")
    _run_backtest(
        settings=settings,
        requested_bar_type=str(parsed_bar_type.composite()),
        strategy_name="overnight-bias-orb",
        create_strategy=lambda _selected_bar_type: OvernightBiasORBStrategy(config=config),
    )
