import typer


from typing import Annotated


from nautilus_trader.model.data import BarType

from cli.backtest import _get_backtest_settings
from cli.backtest import _run_backtest
from strategies.htf_sweep_cisd import HTFSweepCISDStrategy
from strategies.htf_sweep_cisd import HTFSweepCISDStrategyConfig
from strategies.htf_sweep_cisd import OrderMode


cli = typer.Typer(help="Run HTF sweep + CISD strategy backtests")


@cli.command("run")
def _run_htf_sweep_cisd(
    ctx: typer.Context,
    htf_bar_type: Annotated[
        str,
        typer.Option("--htf-bar-type", help="Composite/internal HTF bar type to subscribe to"),
    ],
    ltf_bar_type: Annotated[
        str,
        typer.Option("--ltf-bar-type", help="LTF bar type to subscribe to; a composite is loaded from its catalog source"),
    ],
    entry_order_type: Annotated[
        OrderMode,
        typer.Option("--entry-order-type", help="Entry order type"),
    ] = OrderMode.LIMIT,
    entry_limit_offset: Annotated[
        float,
        typer.Option("--entry-limit-offset", help="Limit entry offset as a direct ratio, e.g. 0.001 for 0.1%"),
    ] = 0.0,
    entry_order_expire_minutes: Annotated[
        int | None,
        typer.Option("--entry-order-expire-minutes", help="Optional GTD expiry for entry limit orders"),
    ] = None,
    risk_per_trade: Annotated[
        float,
        typer.Option("--risk-per-trade", help="Cash risked per trade, sized across the entry-to-stop distance"),
    ] = 1_000.0,
    max_contracts: Annotated[
        int | None,
        typer.Option("--max-contracts", help="Ceiling on contracts per trade; unset means risk sizing alone decides"),
    ] = None,
    stop_order_type: Annotated[
        OrderMode,
        typer.Option("--stop-order-type", help="Stop order type"),
    ] = OrderMode.MARKET,
    stop_loss_distance_ratio: Annotated[
        float,
        typer.Option("--stop-loss-distance-ratio", help="SL location as a ratio of CISD-to-swing distance"),
    ] = 1.0,
    take_profit_multiplier: Annotated[
        float,
        typer.Option("--take-profit-multiplier", help="TP range multiple of the SL range, for adjusting the Risk Reward Ratio"),
    ] = 2.0,
    stop_limit_offset: Annotated[
        float,
        typer.Option("--stop-limit-offset", help="Stop-limit limit-price offset as a direct ratio"),
    ] = 0.0,
    signal_cooldown_seconds: Annotated[
        float | None,
        typer.Option(
            "--signal-cooldown-seconds",
            help="Minimum time between signals; defaults to one HTF period",
        ),
    ] = None,
) -> None:
    try:
        parsed_ltf_bar_type = BarType.from_str(ltf_bar_type)
    except ValueError as exc:
        raise typer.BadParameter(f"Invalid bar type: {ltf_bar_type}", param_hint="--ltf-bar-type") from exc

    _run_backtest(
        settings=_get_backtest_settings(ctx),
        requested_bar_type=str(parsed_ltf_bar_type.composite())
        if parsed_ltf_bar_type.is_composite()
        else ltf_bar_type,
        strategy_name="htf-sweep-cisd",
        create_strategy=lambda _selected_bar_type: HTFSweepCISDStrategy(
            config=HTFSweepCISDStrategyConfig(
                htf_bar_type=htf_bar_type,
                ltf_bar_type=ltf_bar_type,
                entry_order_type=entry_order_type.value,
                entry_limit_offset=entry_limit_offset,
                entry_order_expire_minutes=entry_order_expire_minutes,
                risk_per_trade=risk_per_trade,
                max_contracts=max_contracts,
                stop_order_type=stop_order_type.value,
                stop_loss_distance_ratio=stop_loss_distance_ratio,
                stop_limit_offset=stop_limit_offset,
                signal_cooldown_seconds=signal_cooldown_seconds,
                take_profit_multiplier=take_profit_multiplier,
            ),
        ),
    )
