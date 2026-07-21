import typer


from typing import Annotated


from strategies.subscribe import SubscribeStrategy
from strategies.subscribe import SubscribeStrategyConfig


from ..live import _get_live_settings
from ..live import _run_live


cli = typer.Typer(help="Run SubscribeStrategy live")


@cli.command("run")
def _run_subscribe(
    ctx: typer.Context,
    bar_type: Annotated[
        str,
        typer.Option(
            "--bar-type",
            "-b",
            help="External Tradovate bar type to subscribe to",
        ),
    ],
) -> None:
    _run_live(
        settings=_get_live_settings(ctx),
        strategy=SubscribeStrategy(
            config=SubscribeStrategyConfig(bar_type=bar_type),
        ),
    )
