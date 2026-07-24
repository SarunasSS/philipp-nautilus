from typing import Any

from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.providers import InstrumentProvider
from nautilus_trader.config import InstrumentProviderConfig
from nautilus_trader.model.identifiers import InstrumentId

from .common import parse_instrument
from .core import TRADOVATE_VENUE
from .http.client import TradovateHttpClient


class TradovateInstrumentProvider(InstrumentProvider):
    def __init__(
        self,
        client: TradovateHttpClient,
        clock: LiveClock,
        config: InstrumentProviderConfig | None = None,
    ) -> None:
        super().__init__(config=config)
        self._client = client
        self._clock = clock

    async def load_all_async(self, filters: dict[str, Any] | None = None) -> None:
        symbols = list((filters or {}).get("symbols", []))
        if not symbols:
            self._log.warning(
                "Tradovate does not provide a bounded active-contract listing; "
                "set instrument_provider.filters={'symbols': [...]} or request explicit IDs",
            )
            return
        for symbol in symbols:
            await self.load_async(InstrumentId.from_str(f"{symbol}.{TRADOVATE_VENUE}"))

    async def load_ids_async(
        self,
        instrument_ids: list[InstrumentId],
        filters: dict[str, Any] | None = None,
    ) -> None:
        for instrument_id in instrument_ids:
            await self.load_async(instrument_id, filters)

    async def load_async(
        self,
        instrument_id: InstrumentId,
        filters: dict[str, Any] | None = None,
    ) -> None:
        if instrument_id.venue != TRADOVATE_VENUE:
            raise ValueError(f"Expected venue {TRADOVATE_VENUE}, got {instrument_id.venue}")

        raw_symbol = instrument_id.symbol.value
        contract = await self._client.get_contract(raw_symbol)
        await self._load_contract(contract)

    async def load_contract_async(self, contract_id: int) -> InstrumentId:
        contract = await self._client.get_contract_by_id(contract_id)
        return await self._load_contract(contract)

    async def _load_contract(self, contract: dict[str, Any]) -> InstrumentId:
        raw_symbol = str(contract["name"])
        maturity = await self._client.get_contract_maturity(int(contract["contractMaturityId"]))
        product = await self._client.get_product(int(maturity["productId"]))
        currency = await self._client.get_currency(int(product["currencyId"]))
        instrument = parse_instrument(
            raw_symbol=raw_symbol,
            contract=contract,
            maturity=maturity,
            product=product,
            currency=currency,
            ts_init=self._clock.timestamp_ns(),
        )
        self.add(instrument)
        self._log.info(f"Loaded Tradovate instrument {instrument.id}")
        return instrument.id
