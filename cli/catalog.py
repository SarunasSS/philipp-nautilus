import databento as db
import typer


from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Annotated


from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader
from nautilus_trader.persistence.catalog import ParquetDataCatalog


cli = typer.Typer(help="Manage local Nautilus data catalogs")
bars_cli = typer.Typer(help="Download and inspect bar data")
cli.add_typer(bars_cli, name="bars")


def _dbn_path(raw_data_path: Path, symbol: str, schema: str, start: datetime, end: datetime) -> Path:
    safe_symbol = symbol.replace("/", "_").replace(".", "_")
    safe_start = start.isoformat().replace(":", "").replace("-", "")
    safe_end = end.isoformat().replace(":", "").replace("-", "")

    return raw_data_path / f"{safe_symbol}_{schema}_{safe_start}_{safe_end}.dbn.zst"


def _download_dbn(
    client: db.Historical,
    path: Path,
    dataset: str,
    symbol: str,
    stype_in: str,
    schema: str,
    start: datetime,
    end: datetime,
    limit: int | None,
    overwrite: bool,
) -> None:
    if path.exists() and not overwrite:
        typer.echo(f"Using existing {schema} file: {path}")
        return

    typer.echo(f"Downloading {schema} for {symbol} {start} -> {end}")
    client.timeseries.get_range(
        dataset=dataset,
        symbols=symbol,
        schema=schema,
        stype_in=stype_in,
        stype_out="instrument_id",
        start=start,
        end=end,
        limit=limit,
        path=path,
    )


@bars_cli.command("download")
def download_bars(
    symbol: Annotated[
        str,
        typer.Option("--symbol", help="Databento symbol to download"),
    ] = "NQ.c.0",
    stype_in: Annotated[
        str,
        typer.Option("--stype-in", help="Databento input symbology type"),
    ] = "continuous",
    dataset: Annotated[
        str,
        typer.Option("--dataset", help="Databento dataset"),
    ] = "GLBX.MDP3",
    schema: Annotated[
        str,
        typer.Option("--schema", help="Databento OHLCV schema"),
    ] = "ohlcv-1m",
    start: Annotated[
        datetime,
        typer.Option("--start", help="Download start timestamp"),
    ] = datetime(2026, 1, 1),
    end: Annotated[
        datetime,
        typer.Option("--end", help="Download end timestamp"),
    ] = datetime(2026, 7, 1),
    catalog_path: Annotated[
        Path,
        typer.Option("--catalog-path", help="Nautilus ParquetDataCatalog path", envvar="DATA_CATALOG_PATH"),
    ] = Path("data/catalog"),
    raw_data_path: Annotated[
        Path,
        typer.Option("--raw-data-path", help="Directory for downloaded Databento DBN files"),
    ] = Path("data/databento"),
    api_key: Annotated[
        str | None,
        typer.Option("--api-key", help="Databento API key", envvar="DATABENTO_API_KEY"),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Maximum number of records to download"),
    ] = None,
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite/--reuse", help="Re-download DBN files if they already exist"),
    ] = False,
    use_exchange_as_venue: Annotated[
        bool,
        typer.Option("--use-exchange-as-venue/--use-glbx-venue", help="Use exchange venue IDs from Databento definitions"),
    ] = False,
) -> None:
    if end <= start:
        raise typer.BadParameter("End timestamp must be after start timestamp")

    raw_data_path.mkdir(parents=True, exist_ok=True)
    catalog_path.mkdir(parents=True, exist_ok=True)

    client = db.Historical(key=api_key)
    definition_start = datetime.combine(start.date(), datetime.min.time())
    definition_end = datetime.combine(end.date(), datetime.min.time()) + timedelta(days=1)
    definition_file = _dbn_path(raw_data_path, symbol, "definition", definition_start, definition_end)
    bars_file = _dbn_path(raw_data_path, symbol, schema, start, end)

    _download_dbn(
        client=client,
        path=definition_file,
        dataset=dataset,
        symbol=symbol,
        stype_in=stype_in,
        schema="definition",
        start=definition_start,
        end=definition_end,
        limit=None,
        overwrite=overwrite,
    )
    _download_dbn(
        client=client,
        path=bars_file,
        dataset=dataset,
        symbol=symbol,
        stype_in=stype_in,
        schema=schema,
        start=start,
        end=end,
        limit=limit,
        overwrite=overwrite,
    )

    loader = DatabentoDataLoader()
    catalog = ParquetDataCatalog(catalog_path)
    instruments = loader.from_dbn_file(
        definition_file,
        as_legacy_cython=True,
        use_exchange_as_venue=use_exchange_as_venue,
    )

    if not instruments:
        raise typer.BadParameter(f"No instruments loaded from {definition_file}")

    price_precisions = {instrument.price_precision for instrument in instruments}
    if len(price_precisions) != 1:
        raise typer.BadParameter(f"Loaded instruments have mixed price precision: {sorted(price_precisions)}")

    price_precision = price_precisions.pop()
    bars = loader.from_dbn_file(
        bars_file,
        as_legacy_cython=False,
        price_precision=price_precision,
        use_exchange_as_venue=use_exchange_as_venue,
    )

    if not bars:
        raise typer.BadParameter(f"No bars loaded from {bars_file}")

    instrument_ids = {str(instrument.id) for instrument in instruments}
    bar_instrument_id = str(bars[0].bar_type.instrument_id)
    if bar_instrument_id not in instrument_ids and stype_in.lower() == "continuous":
        instrument_data = type(instruments[0]).to_dict(instruments[0])
        instrument_data["id"] = bar_instrument_id
        instrument_data["raw_symbol"] = symbol
        if "activation_ns" in instrument_data:
            instrument_data["activation_ns"] = min(instrument.activation_ns for instrument in instruments)
        if "expiration_ns" in instrument_data:
            instrument_data["expiration_ns"] = max(instrument.expiration_ns for instrument in instruments)
        instruments.append(type(instruments[0]).from_dict(instrument_data))

    catalog.write_data(instruments)
    catalog.write_data(bars)

    typer.echo(f"Wrote {len(instruments)} instrument(s) and {len(bars)} bar(s) to {catalog_path}")
