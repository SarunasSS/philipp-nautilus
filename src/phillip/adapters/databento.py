import asyncio


import pandas as pd


from nautilus_trader.adapters.databento.common import instrument_id_to_pyo3
from nautilus_trader.adapters.databento.config import DatabentoDataClientConfig
from nautilus_trader.adapters.databento.data import DatabentoDataClient
from nautilus_trader.adapters.databento.factories import get_cached_databento_http_client
from nautilus_trader.adapters.databento.loaders import DatabentoDataLoader
from nautilus_trader.adapters.databento.providers import DatabentoInstrumentProvider
from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.live.factories import LiveDataClientFactory
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import instruments_from_pyo3


class RecentDefinitionDatabentoInstrumentProvider(DatabentoInstrumentProvider):
    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict | None = None,
    ) -> None:
        requested_ids = sorted(
            {instrument_id for instrument_id in instrument_ids if self.find(instrument_id) is None},
        )
        if not requested_ids:
            return

        dataset = self._check_all_datasets_equal(requested_ids)
        available_range = await self._http_client.get_dataset_range(dataset)
        available_end = pd.to_datetime(
            str(available_range["end"]).replace("+00:00:00", ""),
            utc=True,
        )
        available_start = available_end - pd.Timedelta(days=7)

        self._log.info(
            f"Requesting recent definitions for {len(requested_ids)} instrument(s): "
            f"dataset={dataset}, start={available_start}, end={available_end}",
        )
        pyo3_instruments = await self._http_client.get_range_instruments(
            dataset=dataset,
            instrument_ids=[
                instrument_id_to_pyo3(instrument_id) for instrument_id in requested_ids
            ],
            start=available_start.value,
            end=available_end.value,
        )
        requested_id_set = set(requested_ids)
        latest_instruments = {}
        for instrument in instruments_from_pyo3(pyo3_instruments):
            if instrument.id not in requested_id_set:
                continue

            current = latest_instruments.get(instrument.id)
            if current is None or instrument.ts_init > current.ts_init:
                latest_instruments[instrument.id] = instrument

        self.add_bulk(list(latest_instruments.values()))

        missing_ids = [
            instrument_id
            for instrument_id in requested_ids
            if self.find(instrument_id) is None
        ]
        if missing_ids:
            missing = ", ".join(str(instrument_id) for instrument_id in missing_ids)
            raise RuntimeError(f"Databento returned no recent instrument definition for: {missing}")


class RecentDefinitionDatabentoLiveDataClientFactory(LiveDataClientFactory):
    @staticmethod
    def create(
        loop: asyncio.AbstractEventLoop,
        name: str,
        config: DatabentoDataClientConfig,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
    ) -> DatabentoDataClient:
        http_client = get_cached_databento_http_client(
            key=config.api_key,
            gateway=config.http_gateway,
            use_exchange_as_venue=config.use_exchange_as_venue,
        )
        loader = DatabentoDataLoader(config.venue_dataset_map)
        provider = RecentDefinitionDatabentoInstrumentProvider(
            http_client=http_client,
            clock=clock,
            live_api_key=config.api_key,
            live_gateway=config.live_gateway,
            loader=loader,
            config=config.instrument_provider,
            use_exchange_as_venue=config.use_exchange_as_venue,
        )

        return DatabentoDataClient(
            loop=loop,
            http_client=http_client,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=provider,
            loader=loader,
            config=config,
            name=name,
        )
