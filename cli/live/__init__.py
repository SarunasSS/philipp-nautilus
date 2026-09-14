import typer


from typing import Annotated


from cli.live.runner import _run_live
from cli.live.settings import LiveRunSettings
from cli.live.settings import TradovateEnvironment


cli = typer.Typer(help="Run Nautilus Trader live strategies")


@cli.callback()
def configure_live(
    ctx: typer.Context,
    ib_host: Annotated[
        str | None,
        typer.Option(envvar="IB_HOST", help="TWS socket host; supplying it enables the IB data adapter"),
    ] = None,
    ib_port: Annotated[int, typer.Option(envvar="IB_PORT", min=1, max=65535)] = 7497,
    ib_client_id: Annotated[int, typer.Option(envvar="IB_CLIENT_ID", min=1, max=2147483647)] = 101,
    ib_account_id: Annotated[
        str | None,
        typer.Option(envvar="IB_ACCOUNT_ID", help="TWS account ID; supplying it also enables the IB execution adapter"),
    ] = None,
    tradovate_environment: Annotated[
        TradovateEnvironment,
        typer.Option(envvar="TRADOVATE_ENVIRONMENT", help="Tradovate account environment"),
    ] = TradovateEnvironment.DEMO,
    tradovate_username: Annotated[
        str | None,
        typer.Option(envvar="TRADOVATE_USERNAME", help="Tradovate username"),
    ] = None,
    tradovate_password: Annotated[
        str | None,
        typer.Option(envvar="TRADOVATE_PASSWORD", hide_input=True, help="Tradovate password"),
    ] = None,
    tradovate_app_id: Annotated[
        str | None,
        typer.Option(envvar="TRADOVATE_APP_ID", help="API key name; use with --tradovate-cid and --tradovate-sec"),
    ] = None,
    tradovate_app_version: Annotated[
        str,
        typer.Option(envvar="TRADOVATE_APP_VERSION", help="API application version"),
    ] = "1.0",
    tradovate_cid: Annotated[
        int | None,
        typer.Option(envvar="TRADOVATE_CID", help="API key ID; use with --tradovate-app-id and --tradovate-sec"),
    ] = None,
    tradovate_sec: Annotated[
        str | None,
        typer.Option(
            envvar="TRADOVATE_SEC",
            hide_input=True,
            help="API key secret; use with --tradovate-app-id and --tradovate-cid",
        ),
    ] = None,
    tradovate_device_id: Annotated[
        str | None,
        typer.Option(envvar="TRADOVATE_DEVICE_ID", help="Optional stable device ID"),
    ] = None,
    tradovate_account_id: Annotated[
        int | None,
        typer.Option(
            envvar="TRADOVATE_ACCOUNT_ID",
            help="Tradovate account ID; required when credentials expose multiple open accounts",
        ),
    ] = None,
    databento_api_key: Annotated[str | None, typer.Option(envvar="DATABENTO_API_KEY", hidden=True)] = None,
    trader_id: Annotated[str, typer.Option(envvar="TRADER_ID")] = "PHILIPP-LIVE-001",
    log_level: Annotated[str, typer.Option(envvar="LOG_LEVEL")] = "INFO",
) -> None:
    ctx.obj = LiveRunSettings(
        ib_host=ib_host,
        ib_port=ib_port,
        ib_client_id=ib_client_id,
        ib_account_id=ib_account_id,
        tradovate_environment=tradovate_environment,
        tradovate_username=tradovate_username,
        tradovate_password=tradovate_password,
        tradovate_app_id=tradovate_app_id,
        tradovate_app_version=tradovate_app_version,
        tradovate_cid=tradovate_cid,
        tradovate_sec=tradovate_sec,
        tradovate_device_id=tradovate_device_id,
        tradovate_account_id=tradovate_account_id,
        databento_api_key=databento_api_key,
        trader_id=trader_id,
        log_level=log_level,
    )


def _get_live_settings(ctx: typer.Context) -> LiveRunSettings:
    settings = ctx.find_object(LiveRunSettings)
    if settings is None:
        raise RuntimeError("Live settings were not configured")

    return settings


from cli.live.subscribe import cli as subscribe_cli
from cli.live.execute import cli as execute_cli
from cli.live.htf_sweep_cisd import cli as htf_sweep_cisd_cli


cli.add_typer(subscribe_cli, name="subscribe")
cli.add_typer(execute_cli, name="execute")
cli.add_typer(htf_sweep_cisd_cli, name="htf-sweep-cisd")
