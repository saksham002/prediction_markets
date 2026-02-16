"""
Client initialization for Polymarket APIs.

Provides three access levels:
  - gamma_client(): No auth, for market discovery/research
  - clob_read_client(): No auth, for public CLOB data (prices, orderbooks)
  - clob_trading_client(): Authenticated, for placing/cancelling orders
"""

import os
import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient

load_dotenv()

GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
CHAIN_ID = 137


class GammaClient:
    """Lightweight wrapper around the Gamma REST API for market discovery."""

    def __init__(self, base_url = GAMMA_API_BASE):
        self.base_url = base_url
        self.session = requests.Session()

    def get_events(self, **params) -> list[dict]:
        resp = self.session.get(f"{self.base_url}/events", params = params, timeout = 30)
        resp.raise_for_status()
        return resp.json()

    def get_event(self, event_id: str) -> dict:
        resp = self.session.get(f"{self.base_url}/events/{event_id}", timeout = 30)
        resp.raise_for_status()
        return resp.json()

    def get_event_by_slug(self, slug: str) -> list[dict]:
        return self.get_events(slug = slug)

    def get_markets(self, **params) -> list[dict]:
        resp = self.session.get(f"{self.base_url}/markets", params = params, timeout = 30)
        resp.raise_for_status()
        return resp.json()

    def get_market(self, market_id: str) -> dict:
        resp = self.session.get(f"{self.base_url}/markets/{market_id}", params = {}, timeout = 30)
        resp.raise_for_status()
        return resp.json()


def gamma_client() -> GammaClient:
    return GammaClient()


def clob_read_client() -> ClobClient:
    """Unauthenticated CLOB client for public data (prices, books, midpoints)."""
    return ClobClient(CLOB_API_BASE, chain_id = CHAIN_ID)


def clob_trading_client() -> ClobClient:
    """
    Authenticated CLOB client for trading operations.
    Requires PRIVATE_KEY and FUNDER_ADDRESS in .env.
    Derives/creates API credentials on first call.
    """
    private_key = os.environ.get("PRIVATE_KEY")
    funder = os.environ.get("FUNDER_ADDRESS")

    if not private_key:
        raise RuntimeError("PRIVATE_KEY not set in environment. Copy .env.example to .env and fill it in.")
    if not funder:
        raise RuntimeError("FUNDER_ADDRESS not set in environment. Find it at polymarket.com/settings.")

    client = ClobClient(
        CLOB_API_BASE,
        key = private_key,
        chain_id = CHAIN_ID,
        signature_type = 1,  # POLY_PROXY (browser/email Polymarket account)
        funder = funder,
    )
    client.set_api_creds(client.create_or_derive_api_creds())
    return client
