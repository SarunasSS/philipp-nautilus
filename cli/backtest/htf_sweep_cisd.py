import typer


from typing import Annotated


from cli.backtest import _get_backtest_settings
from cli.backtest import _run_backtest
from strategies.htf_sweep_cisd import HTFSweepCISDStrategy
from strategies.htf_sweep_cisd import HTFSweepCISDStrategyConfig


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
        typer.Option("--ltf-bar-type", help="LTF catalog bar type to load and subscribe to"),
    ],
) -> None:
    _run_backtest(
        settings=_get_backtest_settings(ctx),
        requested_bar_type=ltf_bar_type,
        strategy_name="htf-sweep-cisd",
        create_strategy=lambda selected_bar_type: HTFSweepCISDStrategy(
            config=HTFSweepCISDStrategyConfig(
                htf_bar_type=htf_bar_type,
                ltf_bar_type=str(selected_bar_type),
            ),
        ),
    )
