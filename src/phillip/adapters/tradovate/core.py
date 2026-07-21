from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue


TRADOVATE = "TRADOVATE"
TRADOVATE_CLIENT_ID = ClientId(TRADOVATE)
TRADOVATE_VENUE = Venue(TRADOVATE)


PRODUCTION_DEMO_HTTP_URL = "https://demo.tradovateapi.com/v1"
PRODUCTION_LIVE_HTTP_URL = "https://live.tradovateapi.com/v1"
PRODUCTION_MARKET_DATA_WS_URL = "wss://md.tradovateapi.com/v1/websocket"
