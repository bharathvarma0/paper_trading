-- Paper Trading Platform schema. Safe to re-run: every object uses IF NOT EXISTS.
-- Money columns are NUMERIC (exact decimal), never float.
-- CHECK constraints are a safety net: even if application code has a bug,
-- the database refuses impossible states such as a negative balance.

-- ---------- Phase 1 ----------

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMP DEFAULT now()
);

-- One wallet per user; the balance is a cached total of the ledger below.
CREATE TABLE IF NOT EXISTS wallets (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id),
    balance    NUMERIC(18, 2) NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type       TEXT NOT NULL DEFAULT 'MARKET' CHECK (order_type IN ('MARKET', 'LIMIT')),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    limit_price      NUMERIC(18, 4),
    status           TEXT NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING', 'EXECUTED', 'CANCELLED', 'REJECTED')),
    executed_price   NUMERIC(18, 4),
    realized_pnl     NUMERIC(18, 2),
    rejection_reason TEXT,
    idempotency_key  TEXT,
    created_at       TIMESTAMP DEFAULT now(),
    executed_at      TIMESTAMP,
    -- The same client retry key can only create one order per user.
    UNIQUE (user_id, idempotency_key)
);

-- Append-only ledger: every change to a wallet balance gets one row.
CREATE TABLE IF NOT EXISTS wallet_transactions (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    type          TEXT NOT NULL CHECK (type IN ('DEPOSIT', 'BUY', 'SELL')),
    amount        NUMERIC(18, 2) NOT NULL CHECK (amount > 0),
    balance_after NUMERIC(18, 2) NOT NULL,
    order_id      INTEGER REFERENCES orders(id),
    created_at    TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS holdings (
    user_id       INTEGER NOT NULL REFERENCES users(id),
    symbol        TEXT NOT NULL,
    quantity      INTEGER NOT NULL CHECK (quantity >= 0),
    avg_buy_price NUMERIC(18, 4) NOT NULL,
    PRIMARY KEY (user_id, symbol)
);

-- Speeds up "my orders, newest first" and "my ledger, newest first".
CREATE INDEX IF NOT EXISTS idx_orders_user_created ON orders (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wallet_tx_user_created ON wallet_transactions (user_id, created_at DESC);
