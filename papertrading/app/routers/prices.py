"""WebSocket /ws/prices: clients subscribe to symbols and receive live prices.

Protocol (JSON text frames):
  client -> {"action": "subscribe", "symbol": "AAPL"}   / {"action": "unsubscribe", ...}
  server -> {"type": "subscribed", "symbol": "AAPL"}
            {"type": "price", "symbol": "AAPL", "price": "231.45"}
            {"type": "error", "message": "..."}
"""

import asyncio
import re

import jwt
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.security import decode_token
from app.services.price_stream import CLIENT_QUEUE_SIZE, SymbolLimitReached, price_stream

router = APIRouter()

# Also allows exchange-prefixed symbols like BINANCE:BTCUSDT, which trade 24/7
# (handy for testing when the US market is closed).
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9.:\-]{1,20}$")
MAX_SYMBOLS_PER_CLIENT = 10
POLICY_VIOLATION = 1008  # WebSocket close code for "you are not allowed"


@router.websocket("/ws/prices")
async def prices_ws(websocket: WebSocket, token: str = ""):
    # Browsers cannot set an Authorization header on a WebSocket, so the JWT comes
    # in the query string. It is checked BEFORE accepting the connection.
    try:
        decode_token(token)
    except jwt.InvalidTokenError:
        await websocket.close(code=POLICY_VIOLATION)
        return
    await websocket.accept()

    # Everything we send goes through this queue and ONE sender task, so price
    # ticks and replies never write to the socket at the same time.
    queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE_SIZE)
    sender = asyncio.create_task(_send_from_queue(websocket, queue))
    symbols: set[str] = set()
    try:
        while True:
            await _handle_command(await websocket.receive_json(), queue, symbols)
    except (WebSocketDisconnect, ValueError):
        pass  # client left, or sent something that is not JSON
    finally:
        # Always release this client's symbols, or they would stay subscribed upstream forever.
        sender.cancel()
        for symbol in symbols:
            await price_stream.unsubscribe(symbol, queue)


async def _handle_command(msg: object, queue: asyncio.Queue, symbols: set[str]) -> None:
    if not isinstance(msg, dict):
        return _reply(queue, {"type": "error", "message": "Expected a JSON object"})
    action = msg.get("action")
    symbol = str(msg.get("symbol", "")).upper()
    if not SYMBOL_PATTERN.match(symbol):
        return _reply(queue, {"type": "error", "message": "Invalid symbol"})

    if action == "subscribe":
        if symbol in symbols:
            return _reply(queue, {"type": "subscribed", "symbol": symbol})
        if len(symbols) >= MAX_SYMBOLS_PER_CLIENT:
            # Stops one client from using up the shared 50-symbol budget.
            return _reply(queue, {"type": "error", "message": f"Max {MAX_SYMBOLS_PER_CLIENT} symbols per connection"})
        try:
            await price_stream.subscribe(symbol, queue)
        except SymbolLimitReached as exc:
            return _reply(queue, {"type": "error", "message": str(exc)})
        symbols.add(symbol)
        _reply(queue, {"type": "subscribed", "symbol": symbol})
    elif action == "unsubscribe":
        symbols.discard(symbol)
        await price_stream.unsubscribe(symbol, queue)
        _reply(queue, {"type": "unsubscribed", "symbol": symbol})
    else:
        _reply(queue, {"type": "error", "message": "action must be subscribe or unsubscribe"})


def _reply(queue: asyncio.Queue, message: dict) -> None:
    try:
        queue.put_nowait(message)
    except asyncio.QueueFull:
        pass


async def _send_from_queue(websocket: WebSocket, queue: asyncio.Queue) -> None:
    while True:
        await websocket.send_json(await queue.get())
