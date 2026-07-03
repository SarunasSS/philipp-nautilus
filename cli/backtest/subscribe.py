import typer


from typing import Annotated


from cli.backtest import _get_backtest_settings
from cli.backtest import _run_backtest
from strategies.subscribe import SubscribeStrategy
from strategies.subscribe import SubscribeStrategyConfig


cli = typer.Typer(help="Run SubscribeStrategy backtests")


@cli.command("run")
def _run_subscribe(
    ctx: typer.Context,
    bar_type: Annotated[
        str | None,
        typer.Option("--bar-type", help="Bar type to load. Defaults to the first catalog bar type in range."),
    ] = None,
) -> None:
    _run_backtest(
        settings=_get_backtest_settings(ctx),
        requested_bar_type=bar_type,
        strategy_name="subscribe",
        create_strategy=lambda selected_bar_type: SubscribeStrategy(
            config=SubscribeStrategyConfig(bar_type=str(selected_bar_type)),
        ),
    )
