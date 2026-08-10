import typer


from typing import Annotated


from strategies.execute import ExecuteStrategy
from strategies.execute import ExecuteStrategyConfig
from strategies.execute import ExecutionCase


from ..live import _get_live_settings
from ..live import _run_live


cli = typer.Typer(help="Run ExecuteStrategy live execution adapter tests")


@cli.command("run")
def _run_execute(
    ctx: typer.Context,
    bar_type: Annotated[
        str,
        typer.Option(
            "--bar-type",
            "-b",
            help="External bar type to subscribe to",
        ),
    ],
    case: Annotated[
        ExecutionCase,
        typer.Option(
            "--case",
            help="Execution test case",
            case_sensitive=False,
        ),
    ],
    instrument_id: Annotated[
        str | None,
        typer.Option(
            "--instrument-id",
            help="Execution instrument; defaults to the bar type instrument",
        ),
    ] = None,
    quantity: Annotated[
        float,
        typer.Option("--quantity", "-q", min=0.0, help="Order quantity"),
    ] = 1.0,
    timeout: Annotated[
        float,
        typer.Option(min=0.0, help="Seconds to wait for each order fill"),
    ] = 10.0,
    delay: Annotated[
        float,
        typer.Option(min=0.0, help="Seconds to wait between entry and exit"),
    ] = 0.0,
) -> None:
    _run_live(
        settings=_get_live_settings(ctx),
        strategy=ExecuteStrategy(
            config=ExecuteStrategyConfig(
                bar_type=bar_type,
                case=case.value,
                instrument_id=instrument_id,
                quantity=quantity,
                timeout=timeout,
                delay=delay,
            ),
        ),
    )
