"""
Kalshi REST + WebSocket helpers (auth, pagination, rate-limit retry).
"""

import base64
import os
import time
import uuid

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


# --- Order entry / portfolio (role-scoped keys) ---
# "read" = read-only key (market data + portfolio reads); "trade" = trade key
# (order placement ONLY). Default usage stays read-only so nothing can trade by
# accident — order writes must explicitly pass role="trade".

_PRIV_CACHE: dict[str, object] = {}


def _priv_key(path: str):
    if path not in _PRIV_CACHE:
        with open(path, "rb") as f:
            _PRIV_CACHE[path] = serialization.load_pem_private_key(f.read(), password = None)
    return _PRIV_CACHE[path]


def _role_key(role: str) -> tuple[str, str]:
    if role == "trade":
        return os.environ["KALSHI_KEY_ID_TRADE"], os.environ["KALSHI_PRIVATE_KEY_PATH_TRADE"]
    return os.environ["KALSHI_KEY_ID"], os.environ["KALSHI_PRIVATE_KEY_PATH"]


def rest_headers(method: str, path: str, role: str = "read") -> dict:
    """Signed REST headers. `path` is the full request path incl. /trade-api/v2,
    no query string (Kalshi signs ts+METHOD+path)."""
    key_id, key_path = _role_key(role)
    ts = str(int(time.time() * 1000))
    return {"KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-SIGNATURE": _sign(_priv_key(key_path), ts + method + path),
            "KALSHI-ACCESS-TIMESTAMP": ts, "Content-Type": "application/json"}


def create_order(ticker: str, side: str, action: str, count: int, price_cents: int,
                 *, client_order_id: str | None = None, order_type: str = "limit"):
    """Place an order via the TRADE key. `price_cents` is the limit price 1-99 in
    YES space for side='yes' / NO space for side='no'. Returns the requests
    Response (caller inspects .status_code: 201 ok, 429 rate-limited, 4xx reject)."""
    path = "/trade-api/v2/portfolio/orders"
    body = {"ticker": ticker, "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": side, "action": action, "count": int(count), "type": order_type}
    if order_type == "limit":
        body["yes_price" if side == "yes" else "no_price"] = int(price_cents)
    return requests.post(BASE_URL + "/portfolio/orders", json = body,
                         headers = rest_headers("POST", path, "trade"), timeout = 30)


def cancel_order(order_id: str):
    """Cancel one resting order via the TRADE key. Returns the Response
    (200 ok, 429 rate-limited, 404 already gone)."""
    path = "/trade-api/v2/portfolio/orders/" + order_id
    return requests.delete(BASE_URL + "/portfolio/orders/" + order_id,
                           headers = rest_headers("DELETE", path, "trade"), timeout = 30)


def get_orders(status: str = "resting", ticker: str | None = None) -> list:
    """All orders matching status (read key, paginated)."""
    path = "/trade-api/v2/portfolio/orders"
    out, cursor = [], None
    while True:
        params = {"status": status, "limit": 200}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        r = requests.get(BASE_URL + "/portfolio/orders", params = params,
                         headers = rest_headers("GET", path, "read"), timeout = 30)
        r.raise_for_status()
        d = r.json()
        out.extend(d.get("orders", []))
        cursor = d.get("cursor", "")
        if not cursor or not d.get("orders"):
            break
    return out


def get_positions() -> list:
    """Current market positions (read key)."""
    path = "/trade-api/v2/portfolio/positions"
    r = requests.get(BASE_URL + "/portfolio/positions",
                     headers = rest_headers("GET", path, "read"), timeout = 30)
    r.raise_for_status()
    return r.json().get("market_positions", [])


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
