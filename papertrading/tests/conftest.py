import os
from decimal import Decimal

# Point the app at the test database BEFORE importing it: app.config reads
# DATABASE_URL at import time, and load_dotenv() never overrides a variable
# that is already set, so .env (Neon) is ignored here.
os.environ["DATABASE_URL"] = os.getenv("TEST_DATABASE_URL", "postgresql+psycopg2:///papertrading_test")
os.environ["JWT_SECRET"] = "test-secret-at-least-32-bytes-long!!"
os.environ["FINNHUB_KEY"] = ""  # tests must never reach the real provider

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import engine
from app.main import app
from app.services import market_data

# Guard against wiping a real database by mistake.
assert "test" in engine.url.database, f"Refusing to run tests against {engine.url.database!r}"

TABLES = "wallet_transactions, orders, holdings, wallets, users"


@pytest.fixture(autouse=True)
def clean_db():
    # RESTART IDENTITY so ids start at 1 in every test; CASCADE handles foreign keys.
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def prices(monkeypatch) -> dict[str, Decimal]:
    """Fake market: tests set prices["AAPL"] = Decimal("100"). Unknown symbols -> SymbolNotFound."""
    table: dict[str, Decimal] = {}

    def fake_get_price(symbol: str) -> Decimal:
        if symbol not in table:
            raise market_data.SymbolNotFound(symbol)
        return table[symbol]

    monkeypatch.setattr(market_data, "get_price", fake_get_price)
    return table


def register_and_login(client: TestClient, email: str = "a@example.com") -> dict[str, str]:
    client.post("/register", json={"email": email, "password": "password123"})
    token = client.post("/login", json={"email": email, "password": "password123"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def auth(client) -> dict[str, str]:
    return register_and_login(client)
