import threading
import time
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.database import engine
from app.services import market_data, trading
from app.services.trading import place_market_order
from tests.conftest import register_and_login


def deposit(client, auth, amount: str) -> None:
    assert client.post("/wallet/deposit", json={"amount": amount}, headers=auth).status_code == 200


def order(client, auth, symbol: str, side: str, quantity: int, key: str | None = None):
    headers = {**auth, **({"Idempotency-Key": key} if key else {})}
    return client.post("/orders", json={"symbol": symbol, "side": side, "quantity": quantity}, headers=headers)


def balance(client, auth) -> Decimal:
    return Decimal(client.get("/wallet", headers=auth).json()["balance"])


def assert_ledger_consistent(user_id: int = 1) -> None:
    """The core invariant: wallet balance == latest balance_after == deposits - buys + sells."""
    with engine.connect() as conn:
        bal = conn.execute(text("SELECT balance FROM wallets WHERE user_id = :u"), {"u": user_id}).scalar()
        latest = conn.execute(
            text("SELECT balance_after FROM wallet_transactions WHERE user_id = :u ORDER BY id DESC LIMIT 1"),
            {"u": user_id},
        ).scalar()
        total = conn.execute(
            text("""
                SELECT COALESCE(SUM(CASE WHEN type = 'BUY' THEN -amount ELSE amount END), 0)
                FROM wallet_transactions WHERE user_id = :u
            """),
            {"u": user_id},
        ).scalar()
    assert bal == total
    assert latest is None or bal == latest


def test_buy_success(client, auth, prices):
    prices["AAPL"] = Decimal("150.25")
    deposit(client, auth, "1000")

    r = order(client, auth, "aapl", "BUY", 3)  # lowercase symbol is normalised

    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "EXECUTED" and body["symbol"] == "AAPL"
    assert Decimal(body["executed_price"]) == Decimal("150.25")
    assert balance(client, auth) == Decimal("549.25")  # 1000 - 3 * 150.25
    tx = client.get("/wallet/transactions", headers=auth).json()[0]  # newest first
    assert tx["type"] == "BUY" and Decimal(tx["amount"]) == Decimal("450.75") and tx["order_id"] == body["id"]
    assert_ledger_consistent()


def test_buy_insufficient_funds_is_rejected_and_recorded(client, auth, prices):
    prices["AAPL"] = Decimal("100")
    deposit(client, auth, "50")

    r = order(client, auth, "AAPL", "BUY", 1)

    assert r.status_code == 400
    assert "Insufficient funds" in r.json()["detail"]["reason"]
    assert balance(client, auth) == Decimal("50")
    # The REJECTED row survived the rolled-back transaction.
    saved = client.get("/orders?status=REJECTED", headers=auth).json()
    assert len(saved) == 1 and saved[0]["id"] == r.json()["detail"]["order_id"]
    assert client.get("/orders?status=EXECUTED", headers=auth).json() == []
    assert_ledger_consistent()


def test_sell_success_realized_pnl(client, auth, prices):
    deposit(client, auth, "1000")
    prices["TSLA"] = Decimal("100")
    order(client, auth, "TSLA", "BUY", 5)
    prices["TSLA"] = Decimal("120.50")

    r = order(client, auth, "TSLA", "SELL", 2)

    assert r.status_code == 201
    assert Decimal(r.json()["realized_pnl"]) == Decimal("41.00")  # (120.50 - 100) * 2
    assert balance(client, auth) == Decimal("741.00")  # 1000 - 500 + 241
    with engine.connect() as conn:
        qty = conn.execute(text("SELECT quantity FROM holdings WHERE symbol = 'TSLA'")).scalar()
    assert qty == 3
    assert_ledger_consistent()


def test_selling_everything_deletes_holding(client, auth, prices):
    deposit(client, auth, "1000")
    prices["TSLA"] = Decimal("100")
    order(client, auth, "TSLA", "BUY", 2)
    prices["TSLA"] = Decimal("90")

    r = order(client, auth, "TSLA", "SELL", 2)

    assert Decimal(r.json()["realized_pnl"]) == Decimal("-20.00")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM holdings")).scalar() == 0


def test_sell_more_than_owned_is_rejected(client, auth, prices):
    deposit(client, auth, "1000")
    prices["TSLA"] = Decimal("100")
    order(client, auth, "TSLA", "BUY", 2)

    prices["NVDA"] = Decimal("5")

    r = order(client, auth, "TSLA", "SELL", 3)
    never_owned = order(client, auth, "NVDA", "SELL", 1)

    assert r.status_code == 400 and "Insufficient shares" in r.json()["detail"]["reason"]
    assert never_owned.status_code == 400
    assert balance(client, auth) == Decimal("800")
    assert_ledger_consistent()


def test_average_price_after_two_buys(client, auth, prices):
    deposit(client, auth, "10000")
    prices["MSFT"] = Decimal("100")
    order(client, auth, "MSFT", "BUY", 1)
    prices["MSFT"] = Decimal("200")
    order(client, auth, "MSFT", "BUY", 2)

    with engine.connect() as conn:
        row = conn.execute(text("SELECT quantity, avg_buy_price FROM holdings WHERE symbol = 'MSFT'")).one()
    assert row.quantity == 3
    assert row.avg_buy_price == Decimal("166.6667")  # (100 + 400) / 3, rounded to 4 places


def test_idempotency_key_replay_does_not_execute_twice(client, auth, prices):
    deposit(client, auth, "1000")
    prices["AAPL"] = Decimal("100")

    first = order(client, auth, "AAPL", "BUY", 1, key="abc-1")
    second = order(client, auth, "AAPL", "BUY", 1, key="abc-1")

    assert first.status_code == 201 and second.status_code == 200
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json()["id"] == second.json()["id"]
    assert balance(client, auth) == Decimal("900")
    assert len(client.get("/orders", headers=auth).json()) == 1


def test_idempotency_key_reused_for_different_order(client, auth, prices):
    deposit(client, auth, "1000")
    prices["AAPL"] = Decimal("100")
    order(client, auth, "AAPL", "BUY", 1, key="k")

    r = order(client, auth, "AAPL", "BUY", 2, key="k")

    assert r.status_code == 409


def test_idempotency_replay_of_rejected_order_returns_same_rejection(client, auth, prices):
    prices["AAPL"] = Decimal("100")
    first = order(client, auth, "AAPL", "BUY", 1, key="k")
    deposit(client, auth, "1000")  # even with money now, the replay must not execute
    second = order(client, auth, "AAPL", "BUY", 1, key="k")

    assert first.status_code == second.status_code == 400
    assert first.json()["detail"]["order_id"] == second.json()["detail"]["order_id"]
    assert balance(client, auth) == Decimal("1000")


def test_unknown_symbol_returns_404_and_creates_no_order(client, auth, prices):
    deposit(client, auth, "1000")
    r = order(client, auth, "NOPE", "BUY", 1)
    assert r.status_code == 404
    assert client.get("/orders", headers=auth).json() == []


def test_provider_down_returns_503_and_creates_no_order(client, auth, monkeypatch):
    def down(symbol):
        raise market_data.MarketDataError("timeout")

    monkeypatch.setattr(market_data, "get_price", down)
    deposit(client, auth, "1000")

    r = order(client, auth, "AAPL", "BUY", 1)

    assert r.status_code == 503
    assert client.get("/orders", headers=auth).json() == []
    assert balance(client, auth) == Decimal("1000")


def test_order_validation(client, auth, prices):
    for bad in [{"quantity": 0}, {"quantity": -1}, {"quantity": 1.5}, {"quantity": "2"}, {"side": "HOLD"}]:
        body = {"symbol": "AAPL", "side": "BUY", "quantity": 1, **bad}
        assert client.post("/orders", json=body, headers=auth).status_code == 422, bad


def test_orders_require_token(client):
    assert client.post("/orders", json={"symbol": "AAPL", "side": "BUY", "quantity": 1}).status_code == 401


def test_cannot_see_other_users_orders(client, auth, prices):
    deposit(client, auth, "1000")
    prices["AAPL"] = Decimal("100")
    my_order = order(client, auth, "AAPL", "BUY", 1).json()
    other = register_and_login(client, "b@example.com")

    assert client.get(f"/orders/{my_order['id']}", headers=other).status_code == 404
    assert client.get("/orders", headers=other).json() == []
    assert client.get(f"/orders/{my_order['id']}", headers=auth).status_code == 200


def test_order_history_newest_first_and_paginated(client, auth, prices):
    deposit(client, auth, "1000")
    prices["AAPL"] = Decimal("10")
    ids = [order(client, auth, "AAPL", "BUY", 1).json()["id"] for _ in range(3)]

    page1 = client.get("/orders?limit=2&offset=0", headers=auth).json()
    page2 = client.get("/orders?limit=2&offset=2", headers=auth).json()

    assert [o["id"] for o in page1 + page2] == list(reversed(ids))


@pytest.fixture
def slow_transactions(monkeypatch):
    """Sleep inside the order transaction, after the wallet is read.

    Without this the race window is microseconds and a broken lock can still pass by
    luck. With it, if FOR UPDATE were missing, both threads would read the same
    old balance and both orders would execute.
    """
    real_lock_holding = trading._lock_holding

    def slow_lock_holding(*args):
        time.sleep(0.3)
        return real_lock_holding(*args)

    monkeypatch.setattr(trading, "_lock_holding", slow_lock_holding)


def test_concurrent_buys_only_one_can_afford(client, auth, prices, slow_transactions):
    """Two BUYs at the same instant, money for one: the wallet row lock serialises them."""
    deposit(client, auth, "150")
    prices["AAPL"] = Decimal("100")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def buy():
        barrier.wait()  # release both threads together
        o, _ = place_market_order(1, "AAPL", "BUY", 1)
        results.append(o["status"])

    threads = [threading.Thread(target=buy) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == ["EXECUTED", "REJECTED"]
    assert balance(client, auth) == Decimal("50")
    assert_ledger_consistent()


def test_concurrent_identical_requests_with_same_key_execute_once(client, auth, prices, slow_transactions):
    """Both requests pass the first key lookup; the UNIQUE constraint catches the loser."""
    deposit(client, auth, "1000")
    prices["AAPL"] = Decimal("100")
    barrier = threading.Barrier(5)
    results: list[tuple[int, bool]] = []

    def buy():
        barrier.wait()
        o, created = place_market_order(1, "AAPL", "BUY", 1, "same-key")
        results.append((o["id"], created))

    threads = [threading.Thread(target=buy) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({order_id for order_id, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert balance(client, auth) == Decimal("900")
    assert_ledger_consistent()
