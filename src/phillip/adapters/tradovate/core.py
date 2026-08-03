from nautilus_trader.model.identifiers import ClientId
from nautilus_trader.model.identifiers import Venue


TRADOVATE = "TRADOVATE"
TRADOVATE_CLIENT_ID = ClientId(TRADOVATE)
TRADOVATE_VENUE = Venue(TRADOVATE)


PRODUCTION_DEMO_HTTP_URL = "https://demo.tradovateapi.com/v1"
PRODUCTION_LIVE_HTTP_URL = "https://live.tradovateapi.com/v1"
PRODUCTION_DEMO_WS_URL = "wss://demo.tradovateapi.com/v1/websocket"
PRODUCTION_LIVE_WS_URL = "wss://live.tradovateapi.com/v1/websocket"
PRODUCTION_DEMO_MARKET_DATA_WS_URL = "wss://md-demo.tradovateapi.com/v1/websocket"
PRODUCTION_LIVE_MARKET_DATA_WS_URL = "wss://md.tradovateapi.com/v1/websocket"
