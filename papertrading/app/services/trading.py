"""Market order execution.

Rules that keep money correct:
- One order = one database transaction (engine.begin()): all writes commit or none do.
- Row locks are always taken in the same order: wallet, then holdings. Every order
  for a user locks that user's wallet first, so a user's orders run one at a time and
  two transactions can never wait on each other in a cycle (deadlock).
- The price is fetched BEFORE the transaction starts, so we never hold row locks
  while waiting on a slow network call.
"""

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.exc import IntegrityError

from app.database import engine
from app.services import market_data

CENT = Decimal("0.01")  # wallet money: NUMERIC(18,2)
PRICE_STEP = Decimal("0.0001")  # prices: NUMERIC(18,4)

ORDER_COLUMNS = """id, symbol, side, order_type, quantity, status, limit_price, executed_price,
                   realized_pnl, rejection_reason, created_at, executed_at"""


class OrderRejected(Exception):
    """A business-rule failure (e.g. insufficient funds). Raised inside the
    transaction so everything it did is rolled back."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class IdempotencyKeyReused(Exception):
    """The same Idempotency-Key was sent with a different order body."""


def to_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def place_market_order(
    user_id: int, symbol: str, side: str, quantity: int, idempotency_key: str | None = None
) -> tuple[dict, bool]:
    """Execute (or reject) a market order.

    Returns (order, created). created=False means the order already existed for this
    Idempotency-Key and was NOT executed again. A rejected order comes back with
    status REJECTED; the router turns that into a 400.
    Raises SymbolNotFound / MarketDataError before any row is written.
    """
    if idempotency_key:
        existing = find_order_by_key(user_id, idempotency_key)
        if existing:
            _ensure_same_request(existing, symbol, side, quantity)
            return existing, False

    # Raises if the symbol is unknown or the provider is down: no valid price, no order.
    price = market_data.get_price(symbol).quantize(PRICE_STEP, rounding=ROUND_HALF_UP)

    try:
        try:
            with engine.begin() as conn:
                return _execute(conn, user_id, symbol, side, quantity, price, idempotency_key), True
        except OrderRejected as rejected:
            # The transaction above was rolled back. Record the attempt in its own
            # small transaction so the rejection stays in the user's history.
            with engine.begin() as conn:
                order = _insert_order(
                    conn, user_id, symbol, side, quantity, "REJECTED", idempotency_key,
                    rejection_reason=rejected.reason,
                )
            return order, True
    except IntegrityError:
        # UNIQUE(user_id, idempotency_key) fired: an identical request committed first
        # (we were waiting on its wallet lock). Return that result, do not execute twice.
        existing = find_order_by_key(user_id, idempotency_key) if idempotency_key else None
        if existing is None:
            raise  # some other constraint failed: a real bug, let it surface
        _ensure_same_request(existing, symbol, side, quantity)
        return existing, False


def _execute(
    conn: Connection, user_id: int, symbol: str, side: str, quantity: int,
    price: Decimal, idempotency_key: str | None,
) -> dict:
    # Lock order: wallet first, then holdings (see module docstring).
    balance = _lock_wallet(conn, user_id)
    holding = _lock_holding(conn, user_id, symbol)

    value = to_money(price * quantity)
    if value < CENT:
        # The ledger requires amount > 0; a sub-cent trade would round to 0.00.
        raise OrderRejected("Order value is below 0.01")

    if side == "BUY":
        return _buy(conn, user_id, symbol, quantity, price, value, balance, holding, idempotency_key)
    return _sell(conn, user_id, symbol, quantity, price, value, balance, holding, idempotency_key)


def _buy(
    conn: Connection, user_id: int, symbol: str, quantity: int, price: Decimal,
    cost: Decimal, balance: Decimal, holding: RowMapping | None, idempotency_key: str | None,
) -> dict:
    if balance < cost:
        raise OrderRejected(f"Insufficient funds: cost {cost}, balance {balance}")

    # Weighted average: total paid for all shares / total shares.
    old_qty = holding["quantity"] if holding else 0
    old_avg = holding["avg_buy_price"] if holding else Decimal(0)
    new_qty = old_qty + quantity
    new_avg = ((old_qty * old_avg + quantity * price) / new_qty).quantize(PRICE_STEP, rounding=ROUND_HALF_UP)

    conn.execute(
        text("""
            INSERT INTO holdings (user_id, symbol, quantity, avg_buy_price)
            VALUES (:uid, :symbol, :qty, :avg)
            ON CONFLICT (user_id, symbol)
            DO UPDATE SET quantity = EXCLUDED.quantity, avg_buy_price = EXCLUDED.avg_buy_price
        """),
        {"uid": user_id, "symbol": symbol, "qty": new_qty, "avg": new_avg},
    )
    order = _insert_order(
        conn, user_id, symbol, "BUY", quantity, "EXECUTED", idempotency_key, executed_price=price,
    )
    _apply_to_wallet(conn, user_id, balance - cost, "BUY", cost, order["id"])
    return order


def _sell(
    conn: Connection, user_id: int, symbol: str, quantity: int, price: Decimal,
    proceeds: Decimal, balance: Decimal, holding: RowMapping | None, idempotency_key: str | None,
) -> dict:
    owned = holding["quantity"] if holding else 0
    if owned < quantity:
        raise OrderRejected(f"Insufficient shares: you own {owned} {symbol}, tried to sell {quantity}")

    realized_pnl = to_money((price - holding["avg_buy_price"]) * quantity)
    remaining = owned - quantity
    if remaining == 0:
        # No zero-quantity rows: "holdings" means what you own right now.
        conn.execute(
            text("DELETE FROM holdings WHERE user_id = :uid AND symbol = :symbol"),
            {"uid": user_id, "symbol": symbol},
        )
    else:
        # Selling does not change the average cost of the shares you keep.
        conn.execute(
            text("UPDATE holdings SET quantity = :qty WHERE user_id = :uid AND symbol = :symbol"),
            {"qty": remaining, "uid": user_id, "symbol": symbol},
        )
    order = _insert_order(
        conn, user_id, symbol, "SELL", quantity, "EXECUTED", idempotency_key,
        executed_price=price, realized_pnl=realized_pnl,
    )
    _apply_to_wallet(conn, user_id, balance + proceeds, "SELL", proceeds, order["id"])
    return order


def _lock_wallet(conn: Connection, user_id: int) -> Decimal:
    # FOR UPDATE: other transactions touching this wallet wait until we commit,
    # so two orders can never both spend the same money.
    balance = conn.execute(
        text("SELECT balance FROM wallets WHERE user_id = :uid FOR UPDATE"), {"uid": user_id}
    ).scalar()
    if balance is None:
        raise OrderRejected("Wallet not found")
    return balance


def _lock_holding(conn: Connection, user_id: int, symbol: str) -> RowMapping | None:
    return conn.execute(
        text("""
            SELECT quantity, avg_buy_price FROM holdings
            WHERE user_id = :uid AND symbol = :symbol FOR UPDATE
        """),
        {"uid": user_id, "symbol": symbol},
    ).mappings().first()


def _apply_to_wallet(
    conn: Connection, user_id: int, new_balance: Decimal, tx_type: str, amount: Decimal, order_id: int
) -> None:
    # Balance and ledger row are written together, so the balance always equals
    # the latest ledger balance_after.
    conn.execute(
        text("UPDATE wallets SET balance = :b, updated_at = now() WHERE user_id = :uid"),
        {"b": new_balance, "uid": user_id},
    )
    conn.execute(
        text("""
            INSERT INTO wallet_transactions (user_id, type, amount, balance_after, order_id)
            VALUES (:uid, :type, :amount, :balance, :order_id)
        """),
        {"uid": user_id, "type": tx_type, "amount": amount, "balance": new_balance, "order_id": order_id},
    )


def _insert_order(
    conn: Connection, user_id: int, symbol: str, side: str, quantity: int, status: str,
    idempotency_key: str | None, executed_price: Decimal | None = None,
    realized_pnl: Decimal | None = None, rejection_reason: str | None = None,
) -> dict:
    row = conn.execute(
        text(f"""
            INSERT INTO orders (user_id, symbol, side, order_type, quantity, status, executed_price,
                                realized_pnl, rejection_reason, idempotency_key, executed_at)
            VALUES (:uid, :symbol, :side, 'MARKET', :qty, :status, :price,
                    :pnl, :reason, :key, CASE WHEN :executed THEN now() END)
            RETURNING {ORDER_COLUMNS}
        """),
        {
            "uid": user_id, "symbol": symbol, "side": side, "qty": quantity, "status": status,
            "price": executed_price, "pnl": realized_pnl, "reason": rejection_reason,
            "key": idempotency_key, "executed": status == "EXECUTED",
        },
    ).mappings().one()
    return dict(row)


def _ensure_same_request(order: dict, symbol: str, side: str, quantity: int) -> None:
    # Reusing a key for a different order is a client bug; silently returning the
    # old order would hide it.
    if (order["symbol"], order["side"], order["quantity"]) != (symbol, side, quantity):
        raise IdempotencyKeyReused()


# ---------- read queries ----------

def find_order_by_key(user_id: int, idempotency_key: str) -> dict | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {ORDER_COLUMNS} FROM orders WHERE user_id = :uid AND idempotency_key = :key"),
            {"uid": user_id, "key": idempotency_key},
        ).mappings().first()
    return dict(row) if row else None


def get_order(user_id: int, order_id: int) -> dict | None:
    # Filtering by user_id too means another user's order looks exactly like a missing one.
    with engine.connect() as conn:
        row = conn.execute(
            text(f"SELECT {ORDER_COLUMNS} FROM orders WHERE id = :id AND user_id = :uid"),
            {"id": order_id, "uid": user_id},
        ).mappings().first()
    return dict(row) if row else None


def list_orders(user_id: int, status: str | None, limit: int, offset: int) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(f"""
                SELECT {ORDER_COLUMNS} FROM orders
                WHERE user_id = :uid AND (CAST(:status AS TEXT) IS NULL OR status = :status)
                ORDER BY created_at DESC, id DESC
                LIMIT :limit OFFSET :offset
            """),
            {"uid": user_id, "status": status, "limit": limit, "offset": offset},
        ).mappings().all()
    return [dict(r) for r in rows]
