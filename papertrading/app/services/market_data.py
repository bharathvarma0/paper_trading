"""Market data from Finnhub, behind a tiny interface (get_quote/get_price/search_symbols).

The rest of the app only calls these functions, so the provider could be
swapped (or mocked in tests) without touching trading code.
"""

import time
from dataclasses import dataclass
from decimal import Decimal

import httpx

from app.config import FINNHUB_KEY

BASE_URL = "https://finnhub.io/api/v1"
CACHE_SECONDS = 10  # free tier is ~60 calls/min; a short cache keeps us well under it
TIMEOUT_SECONDS = 5  # never let a slow provider hang our request threads


class SymbolNotFound(Exception):
    """The provider does not know this symbol (Finnhub returns price 0)."""


class MarketDataError(Exception):
    """The provider is down, slow, rate limiting us, or returned garbage."""


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    previous_close: Decimal



_cache: dict[str, tuple[float, Quote]] = {}


def _call(path: str, params: dict) -> dict:
    """GET a Finnhub endpoint and return its JSON, with numbers parsed as Decimal."""
    if not FINNHUB_KEY:
        raise MarketDataError("FINNHUB_KEY is not configured")
    try:
        resp = httpx.get(
            f"{BASE_URL}{path}",
            params={**params, "token": FINNHUB_KEY},
            timeout=TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        # parse_float=Decimal: prices never pass through float, so no 0.1+0.2 errors.
        return resp.json(parse_float=Decimal)
    except (httpx.HTTPError, ValueError) as exc:
        raise MarketDataError(f"Market data provider error: {exc}") from exc


def get_quote(symbol: str) -> Quote:
    symbol = symbol.upper()
    cached = _cache.get(symbol)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]

    data = _call("/quote", {"symbol": symbol})
    price = Decimal(str(data.get("c") or 0))
    if price <= 0:
        raise SymbolNotFound(symbol)

    quote = Quote(symbol, price, Decimal(str(data.get("pc") or 0)))
    _cache[symbol] = (time.monotonic(), quote)
    return quote


def update_cached_price(symbol: str, price: Decimal) -> None:
    """Called by the live stream on every trade: keeps cached quotes fresh without REST calls.

    Only refreshes symbols already cached, because a Quote also needs previous_close,
    which the stream does not send.
    """
    cached = _cache.get(symbol)
    if cached:
        _cache[symbol] = (time.monotonic(), Quote(symbol, price, cached[1].previous_close))


def get_price(symbol: str) -> Decimal:
    return get_quote(symbol).price


def search_symbols(query: str) -> list[dict]:
    data = _call("/search", {"q": query})
    return [
        {"symbol": r["symbol"], "description": r.get("description", ""), "type": r.get("type", "")}
        for r in data.get("result", [])
    ]
