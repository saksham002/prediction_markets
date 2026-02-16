"""
Client initialization for Kalshi APIs.

Provides two access levels:
  - read_client(): No auth, for market discovery/research (markets, events, orderbooks)
  - trading_client(): Authenticated, for placing/cancelling orders and portfolio access
"""

import os
from dotenv import load_dotenv
from kalshi_python_sync import (
    KalshiClient, KalshiAuth, Configuration,
    MarketApi, EventsApi, ExchangeApi, OrdersApi, PortfolioApi, SearchApi,
)

load_dotenv(dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env"))

PROD_HOST = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_HOST = "https://demo-api.kalshi.co/trade-api/v2"


class KalshiClients:
    """
    Bundles the typed API classes from the Kalshi SDK.
    Access individual APIs via .market, .events, .exchange, .orders, .portfolio, .search.
    """

    def __init__(self, api_client: KalshiClient):
        self.raw = api_client
        self.market = MarketApi(api_client)
        self.events = EventsApi(api_client)
        self.exchange = ExchangeApi(api_client)
        self.search = SearchApi(api_client)
        self.orders = OrdersApi(api_client)
        self.portfolio = PortfolioApi(api_client)


def read_client(demo: bool = False) -> KalshiClients:
    """Unauthenticated client for public data (markets, events, prices)."""
    host = DEMO_HOST if demo else PROD_HOST
    config = Configuration(host = host)
    return KalshiClients(KalshiClient(configuration = config))


def trading_client(demo: bool = False) -> KalshiClients:
    """
    Authenticated client for trading operations.
    Requires KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env.
    """
    key_id = os.environ.get("KALSHI_KEY_ID")
    pk_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")

    if not key_id:
        raise RuntimeError("KALSHI_KEY_ID not set. Create an API key at kalshi.com/account/profile.")
    if not pk_path:
        raise RuntimeError("KALSHI_PRIVATE_KEY_PATH not set. Point it to your RSA private key PEM file.")

    with open(pk_path, "r") as f:
        private_key_pem = f.read()

    host = DEMO_HOST if demo else PROD_HOST
    config = Configuration(host = host)
    client = KalshiClient(configuration = config)
    auth = KalshiAuth(key_id = key_id, private_key_pem = private_key_pem)
    client.set_kalshi_auth(auth)
    return KalshiClients(client)
