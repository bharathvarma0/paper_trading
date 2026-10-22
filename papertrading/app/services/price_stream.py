"""Live prices: ONE WebSocket to Finnhub, fanned out to many of our own clients.

Why one upstream connection: Finnhub's free tier allows a single connection and
about 50 symbols. Each browser subscribing directly would blow both limits and
leak our API key. Instead the server subscribes once per symbol and relays
trades to every client watching it.
"""

import asyncio
import json
import logging
import random
from decimal import Decimal

from websockets.asyncio.client import ClientConnection, connect

from app.config import FINNHUB_KEY
from app.services import market_data

log = logging.getLogger(__name__)

FINNHUB_WS_URL = "wss://ws.finnhub.io"
MAX_SYMBOLS = 50  # Finnhub free-tier symbol limit per connection
CLIENT_QUEUE_SIZE = 100  # per-client buffer; a client that falls this far behind loses messages
MAX_BACKOFF_SECONDS = 60


class SymbolLimitReached(Exception):
    pass


class PriceStream:
    def __init__(self, api_key: str = FINNHUB_KEY):
        self._api_key = api_key
        # symbol -> queues of the clients watching it. A symbol is subscribed
        # upstream while its set is non-empty.
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._ws: ClientConnection | None = None
        self.latest: dict[str, Decimal] = {}

    # ---------- client side ----------

    async def subscribe(self, symbol: str, queue: asyncio.Queue) -> None:
        if symbol not in self._subscribers:
            if len(self._subscribers) >= MAX_SYMBOLS:
                raise SymbolLimitReached(f"Server is already streaming {MAX_SYMBOLS} symbols")
            self._subscribers[symbol] = set()
            await self._send_upstream({"type": "subscribe", "symbol": symbol})
        self._subscribers[symbol].add(queue)

        # Send the last known price at once, so the client is not blank until the next trade.
        if symbol in self.latest:
            _offer(queue, _price_message(symbol, self.latest[symbol]))

    async def unsubscribe(self, symbol: str, queue: asyncio.Queue) -> None:
        watchers = self._subscribers.get(symbol)
        if watchers is None:
            return
        watchers.discard(queue)
        if not watchers:
            # Last watcher left: free the slot so another symbol can use it.
            del self._subscribers[symbol]
            await self._send_upstream({"type": "unsubscribe", "symbol": symbol})

    # ---------- upstream side ----------

    async def run(self) -> None:
        """Keep the Finnhub connection alive forever, reconnecting with backoff."""
        attempt = 0
        while True:
            try:
                async with connect(f"{FINNHUB_WS_URL}?token={self._api_key}") as ws:
                    self._ws = ws
                    attempt = 0  # connected: reset the backoff
                    log.info("Finnhub stream connected")
                    # A new connection knows nothing: re-subscribe everything clients want.
                    for symbol in list(self._subscribers):
                        await ws.send(json.dumps({"type": "subscribe", "symbol": symbol}))
                    async for raw in ws:
                        self.handle_message(raw)
            except asyncio.CancelledError:
                raise  # app shutdown
            except Exception as exc:
                log.warning("Finnhub stream error: %s", exc)
            finally:
                self._ws = None

            # Exponential backoff (1, 2, 4 ... 60s) with jitter, so a Finnhub outage
            # is not hammered by reconnects at a fixed rhythm.
            delay = min(MAX_BACKOFF_SECONDS, 2 ** attempt) * random.uniform(0.5, 1.0)
            attempt += 1
            log.info("Reconnecting to Finnhub in %.1fs", delay)
            await asyncio.sleep(delay)

    def handle_message(self, raw: str | bytes) -> None:
        msg = json.loads(raw, parse_float=Decimal)  # prices stay Decimal end to end
        if msg.get("type") != "trade":
            return  # "ping" keep-alives and anything else

        # One message can hold many trades; only the newest price per symbol matters.
        newest: dict[str, Decimal] = {}
        for trade in msg.get("data", []):
            newest[trade["s"]] = Decimal(str(trade["p"]))

        for symbol, price in newest.items():
            self.latest[symbol] = price
            market_data.update_cached_price(symbol, price)
            message = _price_message(symbol, price)
            for queue in self._subscribers.get(symbol, ()):
                _offer(queue, message)

    async def _send_upstream(self, message: dict) -> None:
        # While disconnected we only record the wish; run() re-subscribes on connect.
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(message))
        except Exception as exc:
            log.warning("Could not send to Finnhub: %s", exc)


def _price_message(symbol: str, price: Decimal) -> dict:
    # Decimal is sent as a string so the browser never sees a rounded float.
    return {"type": "price", "symbol": symbol, "price": str(price)}


def _offer(queue: asyncio.Queue, message: dict) -> None:
    # Never await a slow client: that would stall prices for everyone.
    # If its buffer is full, it misses this tick; the next one replaces it anyway.
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        pass


# One shared instance for the whole app (one upstream connection).
price_stream = PriceStream()
