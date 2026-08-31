import typer


from typing import Annotated


from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId

from strategies.htf_sweep_cisd import HTFSweepCISDStrategy
from strategies.htf_sweep_cisd import HTFSweepCISDStrategyConfig
from strategies.htf_sweep_cisd import OrderMode

from ..live import _get_live_settings
from ..live import _run_live


cli = typer.Typer(help="Run HTF sweep + CISD live")


@cli.command("run")
def _run_htf_sweep_cisd(
    ctx: typer.Context,
    htf_bar_type: Annotated[
        str,
        typer.Option("--htf-bar-type", help="Composite/internal HTF bar type to subscribe to"),
    ],
    ltf_bar_type: Annotated[
        str,
        typer.Option("--ltf-bar-type", help="External LTF bar type to subscribe to"),
    ],
    execution_instrument_id: Annotated[
        str,
        typer.Option(
            "--execution-instrument-id",
            help="Instrument used for orders; its symbol must match the bar instrument",
        ),
    ],
    entry_order_type: Annotated[
        OrderMode,
        typer.Option("--entry-order-type", help="Entry order type"),
    ] = OrderMode.LIMIT,
    entry_limit_offset: Annotated[
        float,
        typer.Option("--entry-limit-offset", help="Limit entry offset as a direct ratio"),
    ] = 0.0,
    entry_order_expire_minutes: Annotated[
        int,
        typer.Option(
            "--entry-order-expire-minutes",
            min=1,
            help="GTD expiry for entry limit orders",
        ),
    ] = 15,
    trade_quantity: Annotated[
        int,
        typer.Option(
            "--trade-quantity",
            min=1,
            max=1,
            help="Fixed number of contracts for each entry; this deployment is capped at one",
        ),
    ] = 1,
    stop_order_type: Annotated[
        OrderMode,
        typer.Option("--stop-order-type", help="Stop order type"),
    ] = OrderMode.MARKET,
    stop_loss_distance_ratio: Annotated[
        float,
        typer.Option("--stop-loss-distance-ratio", help="SL as a ratio of CISD-to-swing distance"),
    ] = 1.0,
    stop_limit_offset: Annotated[
        float,
        typer.Option("--stop-limit-offset", help="Stop-limit price offset as a direct ratio"),
    ] = 0.0,
) -> None:
    ltf_instrument_id = BarType.from_str(ltf_bar_type).instrument_id
    execution_id = InstrumentId.from_str(execution_instrument_id)
    _run_live(
        data_instrument_ids=[ltf_instrument_id],
        execution_instrument_ids=[execution_id],
        settings=_get_live_settings(ctx),
        strategy=HTFSweepCISDStrategy(
            config=HTFSweepCISDStrategyConfig(
                live=True,
                htf_bar_type=htf_bar_type,
                ltf_bar_type=ltf_bar_type,
                execution_instrument_id=execution_instrument_id,
                entry_order_type=entry_order_type.value,
                entry_limit_offset=entry_limit_offset,
                entry_order_expire_minutes=entry_order_expire_minutes,
                trade_quantity=trade_quantity,
                stop_order_type=stop_order_type.value,
                stop_loss_distance_ratio=stop_loss_distance_ratio,
                stop_limit_offset=stop_limit_offset,
            ),
        ),
    )
