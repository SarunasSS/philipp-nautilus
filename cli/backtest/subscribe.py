import typer


from typing import Annotated


from nautilus_trader.model.data import BarType

from cli.backtest import _get_backtest_settings
from cli.backtest import _run_backtest
from strategies.subscribe import SubscribeStrategy
from strategies.subscribe import SubscribeStrategyConfig


cli = typer.Typer(help="Run SubscribeStrategy backtests")


@cli.command("run")
def _run_subscribe(
    ctx: typer.Context,
    bar_types: Annotated[
        list[BarType],
        typer.Option(
            "--bar-type",
            parser=BarType.from_str,
            help="Bar type to load. Repeat for multiple streams.",
        ),
    ],
) -> None:
    _run_backtest(
        settings=_get_backtest_settings(ctx),
        bar_types=bar_types,
        strategy_name="subscribe",
        create_strategy=lambda selected_bar_type: SubscribeStrategy(
            config=SubscribeStrategyConfig(bar_type=str(selected_bar_type)),
        ),
    )
