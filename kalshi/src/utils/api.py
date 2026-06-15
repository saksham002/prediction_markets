"""
Kalshi REST + WebSocket helpers (auth, pagination, rate-limit retry).
"""

import base64
import os
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv(dotenv_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"


# --- Auth ---

def _load_private_key():
    pk_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]
    with open(pk_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password = None)


def _sign(private_key, text: str) -> str:
    signature = private_key.sign(
        text.encode("utf-8"),
        padding.PSS(
            mgf = padding.MGF1(hashes.SHA256()),
            salt_length = padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def ws_auth_headers():
    key_id = os.environ["KALSHI_KEY_ID"]
    private_key = _load_private_key()
    timestamp_ms = str(int(time.time() * 1000))
    message = timestamp_ms + "GET" + "/trade-api/ws/v2"
    signature = _sign(private_key, message)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }


# --- REST ---

def api_get(url, params = None):
    for attempt in range(5):
        resp = requests.get(url, params = params, timeout = 30)
        if resp.status_code == 429:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        return resp
    resp.raise_for_status()


def fetch_series_fee(series_ticker: str) -> tuple[float, str]:
    """Fetch (fee_multiplier, fee_type) for a Kalshi series."""
    data = api_get(f"{BASE_URL}/series/{series_ticker}").json()
    s = data["series"] if "series" in data else data
    return float(s["fee_multiplier"]), s["fee_type"]


def fetch_market_result(ticker: str) -> str | None:
    """Settlement result for a market: "yes", "no", or None if not settled."""
    market = api_get(f"{BASE_URL}/markets/{ticker}").json()["market"]
    result = market.get("result") or ""
    return result if result in ("yes", "no") else None


def discover_top_events(n: int, category: str | None = None, max_markets: int = 10):
    """Find top N active events (2 to max_markets markets) by 24h volume, optionally filtered by category."""
    print("Fetching active events with nested markets...")
    events = paginate(
        "events",
        params = {"status": "open", "with_nested_markets": True},
        key = "events",
        max_per_page = 200,
    )
    print(f"  {len(events)} active events found")

    scored = []
    for ev in events:
        if category and ev.get("category", "") != category:
            continue
        mkts = ev.get("markets", [])
        if len(mkts) < 2 or len(mkts) > max_markets:
            continue
        total_vol = sum(float(m.get("volume_24h_fp", "0")) for m in mkts)
        scored.append((ev, mkts, total_vol))

    scored.sort(key = lambda x: x[2], reverse = True)
    top = scored[:n]

    result = []
    for ev, mkts, total_vol in top:
        et = ev["event_ticker"]
        result.append({
            "event_ticker": et,
            "title": ev.get("title", et),
            "category": ev.get("category", ""),
            "volume": total_vol,
            "markets": mkts,
            "tickers": [m["ticker"] for m in mkts],
        })
        print(f"  [{et}] category={ev.get('category', '')} volume={total_vol:.0f} markets={len(mkts)} title={ev.get('title', '')[:60]}")

    return result


def paginate(endpoint, params = None, key = None, max_per_page = 1000):
    if key is None:
        key = endpoint.strip("/").split("/")[-1]
    params = dict(params or {})
    params["limit"] = max_per_page
    all_items = []
    cursor = None
    while True:
        if cursor:
            params["cursor"] = cursor
        data = api_get(f"{BASE_URL}/{endpoint}", params = params).json()
        items = data.get(key, [])
        all_items.extend(items)
        cursor = data.get("cursor", "")
        if not cursor or not items:
            break
        time.sleep(0.15)
    return all_items
