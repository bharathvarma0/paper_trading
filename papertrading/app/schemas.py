from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    # bcrypt only uses the first 72 bytes of a password, so cap it there.
    password: str = Field(min_length=8, max_length=72)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: EmailStr


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class DepositRequest(BaseModel):
    # decimal_places=2 matches NUMERIC(18,2); otherwise 0.001 would be rounded
    # to 0.00 by Postgres and break the ledger's "amount > 0" rule.
    amount: Decimal = Field(gt=0, le=100000, decimal_places=2)


class WalletResponse(BaseModel):
    balance: Decimal


class TransactionResponse(BaseModel):
    id: int
    type: str
    amount: Decimal
    balance_after: Decimal
    order_id: int | None
    created_at: datetime


class QuoteResponse(BaseModel):
    symbol: str
    price: Decimal
    previous_close: Decimal


class SearchResult(BaseModel):
    symbol: str
    description: str
    type: str


class OrderRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=10, pattern=r"^[A-Za-z0-9.\-]+$")
    side: Literal["BUY", "SELL"]
    # Whole shares only. strict=True rejects 1.5, "5" and true instead of coercing them;
    # the upper bound keeps price * quantity inside NUMERIC(18,2).
    quantity: int = Field(strict=True, gt=0, le=1_000_000)

    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, v: str) -> str:
        # "aapl" and "AAPL" must be the same holding row.
        return v.upper()


class OrderResponse(BaseModel):
    id: int
    symbol: str
    side: str
    order_type: str
    quantity: int
    status: str
    limit_price: Decimal | None
    executed_price: Decimal | None
    realized_pnl: Decimal | None
    rejection_reason: str | None
    created_at: datetime
    executed_at: datetime | None
