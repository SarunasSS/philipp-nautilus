import asyncio


from dataclasses import dataclass
from typing import Any


from nautilus_trader.cache.cache import Cache
from nautilus_trader.common.component import LiveClock
from nautilus_trader.common.component import MessageBus
from nautilus_trader.data.messages import RequestBars
from nautilus_trader.data.messages import RequestInstrument
from nautilus_trader.data.messages import RequestInstruments
from nautilus_trader.data.messages import SubscribeBars
from nautilus_trader.data.messages import UnsubscribeBars
from nautilus_trader.live.data_client import LiveMarketDataClient
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument


from .common import bar_spec_to_chart
from .common import parse_bar
from .config import TradovateDataClientConfig
from .core import TRADOVATE_CLIENT_ID
from .core import TRADOVATE_VENUE
from .http.client import TradovateHttpClient
from .providers import TradovateInstrumentProvider
from .websocket.client import TradovateWebSocketClient


@dataclass
class _ChartSubscription:
    bar_type: BarType
    instrument: Instrument
    interval_ns: int
    historical_id: int
    realtime_id: int


class TradovateDataClient(LiveMarketDataClient):
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        http_client: TradovateHttpClient,
        msgbus: MessageBus,
        cache: Cache,
        clock: LiveClock,
        instrument_provider: TradovateInstrumentProvider,
        config: TradovateDataClientConfig,
        name: str | None,
    ) -> None:
        super().__init__(
            loop=loop,
            client_id=ClientId(name) if name else TRADOVATE_CLIENT_ID,
            venue=TRADOVATE_VENUE,
            msgbus=msgbus,
            cache=cache,
            clock=clock,
            instrument_provider=instrument_provider,
            config=config,
        )
        self._http_client = http_client
        self._ws_client = TradovateWebSocketClient(
            loop=loop,
            base_url=config.ws_base_url,
            handler=self._handle_ws_message,
            reconnect_handler=self._resubscribe,
            request_timeout_secs=config.request_timeout_secs,
        )
        self._chart_subscriptions: dict[str, _ChartSubscription] = {}
        self._charts_by_id: dict[int, _ChartSubscription] = {}
        self._unmapped_chart_messages: dict[int, list[dict[str, Any]]] = {}
        self._pending_bars: dict[str, Bar] = {}

    async def _connect(self) -> None:
        access_token = await self._http_client.get_access_token(market_data=True)
        for instrument in self._instrument_provider.list_all():
            self._handle_data(instrument)
        await self._ws_client.connect(access_token)

    async def _disconnect(self) -> None:
        await self._ws_client.disconnect()

    async def _request_instrument(self, request: RequestInstrument) -> None:
        instrument = await self._get_instrument(request.instrument_id)
        self._handle_instrument(instrument, request.id, request.start, request.end, request.params)

    async def _request_instruments(self, request: RequestInstruments) -> None:
        await self._instrument_provider.load_all_async(filters=request.params or None)
        self._handle_instruments(
            request.venue,
            list(self._instrument_provider.get_all().values()),
            request.id,
            request.start,
            request.end,
            request.params,
        )

    async def _subscribe_bars(self, command: SubscribeBars) -> None:
        instrument = await self._get_instrument(command.bar_type.instrument_id)
        await self._open_chart_subscription(command.bar_type, instrument)
        self._log.info(f"Subscribed bars for {command.bar_type}")

    async def _unsubscribe_bars(self, command: UnsubscribeBars) -> None:
        key = str(command.bar_type)
        subscription = self._chart_subscriptions.pop(key, None)
        self._pending_bars.pop(key, None)
        if subscription is None:
            return

        await self._ws_client.request(
            "md/cancelChart",
            {"subscriptionId": subscription.realtime_id},
        )
        self._charts_by_id.pop(subscription.historical_id, None)
        self._charts_by_id.pop(subscription.realtime_id, None)

    async def _request_bars(self, request: RequestBars) -> None:
        self._log.error("Historical bar requests are not implemented; use a live chart subscription")

    def _handle_ws_message(self, message: dict[str, Any]) -> None:
        event = message.get("e")
        data = message.get("d") or {}
        if event == "chart":
            for chart in data.get("charts", []):
                chart_id = int(chart["id"])
                subscription = self._charts_by_id.get(chart_id)
                if subscription is None:
                    self._unmapped_chart_messages.setdefault(chart_id, []).append(chart)
                else:
                    self._handle_chart(chart, subscription)
        elif event == "shutdown":
            self._log.error(f"Tradovate requested WebSocket shutdown: {data}")

    def _handle_chart(self, chart: dict[str, Any], subscription: _ChartSubscription) -> None:
        key = str(subscription.bar_type)
        for raw_bar in chart.get("bars", []):
            bar = parse_bar(
                raw=raw_bar,
                bar_type=subscription.bar_type,
                instrument=subscription.instrument,
                interval_ns=subscription.interval_ns,
                ts_init=self._clock.timestamp_ns(),
            )
            pending = self._pending_bars.get(key)
            if pending is not None and pending.ts_event != bar.ts_event:
                self._handle_data(pending)
            self._pending_bars[key] = bar

    async def _resubscribe(self) -> None:
        subscriptions = list(self._chart_subscriptions.values())
        self._chart_subscriptions.clear()
        self._charts_by_id.clear()
        self._unmapped_chart_messages.clear()
        for subscription in subscriptions:
            await self._open_chart_subscription(
                subscription.bar_type,
                subscription.instrument,
            )

    async def _open_chart_subscription(
        self,
        bar_type: BarType,
        instrument: Instrument,
    ) -> None:
        underlying_type, element_size, interval_ns = bar_spec_to_chart(bar_type)
        response = await self._ws_client.request(
            "md/getChart",
            {
                "symbol": self._contract_id(instrument),
                "chartDescription": {
                    "underlyingType": underlying_type,
                    "elementSize": element_size,
                    "elementSizeUnit": "UnderlyingUnits",
                    "withHistogram": False,
                },
                "timeRange": {"asMuchAsElements": 3},
            },
        )
        result = response.get("d") or {}
        subscription = _ChartSubscription(
            bar_type=bar_type,
            instrument=instrument,
            interval_ns=interval_ns,
            historical_id=int(result["historicalId"]),
            realtime_id=int(result["realtimeId"]),
        )
        self._chart_subscriptions[str(bar_type)] = subscription
        self._charts_by_id[subscription.historical_id] = subscription
        self._charts_by_id[subscription.realtime_id] = subscription
        for chart_id in (subscription.historical_id, subscription.realtime_id):
            for chart in self._unmapped_chart_messages.pop(chart_id, []):
                self._handle_chart(chart, subscription)

    async def _get_instrument(self, instrument_id: InstrumentId) -> Instrument:
        instrument = self._cache.instrument(instrument_id) or self._instrument_provider.find(instrument_id)
        if instrument is None:
            await self._instrument_provider.load_async(instrument_id)
            instrument = self._instrument_provider.find(instrument_id)
        if instrument is None:
            raise ValueError(f"Tradovate instrument not found: {instrument_id}")
        return instrument

    @staticmethod
    def _contract_id(instrument: Instrument) -> int:
        return int(instrument.info["contract_id"])
