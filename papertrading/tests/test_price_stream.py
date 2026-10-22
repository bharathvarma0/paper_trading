import asyncio
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.services import market_data, price_stream as stream_module
from app.services.price_stream import PriceStream, SymbolLimitReached


class FakeUpstream:
    """Stands in for the Finnhub connection and records what we send it."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def make_stream() -> tuple[PriceStream, FakeUpstream]:
    stream = PriceStream(api_key="test")
    upstream = FakeUpstream()
    stream._ws = upstream  # pretend we are connected
    return stream, upstream


def trade(*trades: tuple[str, str]) -> str:
    return json.dumps({"type": "trade", "data": [{"s": s, "p": float(p), "t": 0, "v": 1} for s, p in trades]})


def test_upstream_subscribe_once_per_symbol_and_unsubscribe_after_last_client():
    async def scenario():
        stream, upstream = make_stream()
        a, b = asyncio.Queue(), asyncio.Queue()
        await stream.subscribe("AAPL", a)
        await stream.subscribe("AAPL", b)
        await stream.unsubscribe("AAPL", a)
        assert upstream.sent == [{"type": "subscribe", "symbol": "AAPL"}]  # b still watching
        await stream.unsubscribe("AAPL", b)
        assert upstream.sent[-1] == {"type": "unsubscribe", "symbol": "AAPL"}

    asyncio.run(scenario())


def test_trades_fan_out_only_to_watchers_with_newest_price():
    async def scenario():
        stream, _ = make_stream()
        aapl_watcher, tsla_watcher = asyncio.Queue(), asyncio.Queue()
        await stream.subscribe("AAPL", aapl_watcher)
        await stream.subscribe("TSLA", tsla_watcher)

        stream.handle_message(trade(("AAPL", "100.10"), ("AAPL", "100.25")))

        assert aapl_watcher.get_nowait() == {"type": "price", "symbol": "AAPL", "price": "100.25"}
        assert aapl_watcher.empty()  # two trades in one message -> one update
        assert tsla_watcher.empty()

    asyncio.run(scenario())


def test_new_subscriber_gets_last_known_price_immediately():
    async def scenario():
        stream, _ = make_stream()
        await stream.subscribe("AAPL", asyncio.Queue())
        stream.handle_message(trade(("AAPL", "99.5")))
        late = asyncio.Queue()
        await stream.subscribe("AAPL", late)
        assert late.get_nowait()["price"] == "99.5"

    asyncio.run(scenario())


def test_slow_client_does_not_block_others():
    async def scenario():
        stream, _ = make_stream()
        slow, fast = asyncio.Queue(maxsize=1), asyncio.Queue()
        await stream.subscribe("AAPL", slow)
        await stream.subscribe("AAPL", fast)
        for p in ["1", "2", "3"]:
            stream.handle_message(trade(("AAPL", p)))  # would raise/hang if we awaited a full queue
        assert slow.qsize() == 1 and fast.qsize() == 3

    asyncio.run(scenario())


def test_symbol_limit(monkeypatch):
    monkeypatch.setattr(stream_module, "MAX_SYMBOLS", 2)

    async def scenario():
        stream, _ = make_stream()
        q = asyncio.Queue()
        await stream.subscribe("A", q)
        await stream.subscribe("B", q)
        with pytest.raises(SymbolLimitReached):
            await stream.subscribe("C", q)
        await stream.subscribe("A", asyncio.Queue())  # an already streamed symbol is still fine

    asyncio.run(scenario())


def test_ping_and_unknown_messages_are_ignored():
    stream, _ = make_stream()
    stream.handle_message(json.dumps({"type": "ping"}))
    assert stream.latest == {}


def test_live_trade_refreshes_cached_quote(monkeypatch):
    monkeypatch.setattr(market_data, "_cache", {"AAPL": (0.0, market_data.Quote("AAPL", Decimal("1"), Decimal("0.9")))})
    stream, _ = make_stream()
    stream.handle_message(trade(("AAPL", "123.45")))
    # Fresh enough to be served without a REST call, previous close kept.
    assert market_data.get_quote("AAPL") == market_data.Quote("AAPL", Decimal("123.45"), Decimal("0.9"))


# ---------- the /ws/prices endpoint ----------

def test_ws_rejects_missing_or_bad_token():
    client = TestClient(app)
    for url in ["/ws/prices", "/ws/prices?token=garbage"]:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(url) as ws:
                ws.receive_json()
        assert exc.value.code == 1008


def test_ws_subscribe_flow(client, auth):
    token = auth["Authorization"].split()[1]
    with client.websocket_connect(f"/ws/prices?token={token}") as ws:
        ws.send_json({"action": "subscribe", "symbol": "aapl"})
        assert ws.receive_json() == {"type": "subscribed", "symbol": "AAPL"}
        ws.send_json({"action": "subscribe", "symbol": "bad symbol!"})
        assert ws.receive_json()["type"] == "error"
        ws.send_json({"action": "dance", "symbol": "AAPL"})
        assert ws.receive_json()["type"] == "error"
    # Disconnecting released the symbol, so nothing stays subscribed upstream.
    assert "AAPL" not in stream_module.price_stream._subscribers
