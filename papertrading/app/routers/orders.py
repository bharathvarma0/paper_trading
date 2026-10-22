from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response

from app.schemas import OrderRequest, OrderResponse
from app.security import get_current_user
from app.services.market_data import MarketDataError, SymbolNotFound
from app.services.trading import IdempotencyKeyReused, get_order, list_orders, place_market_order

router = APIRouter(prefix="/orders")


@router.post("", response_model=OrderResponse, status_code=201)
def create_order(
    body: OrderRequest,
    response: Response,
    user_id: int = Depends(get_current_user),
    # FastAPI maps idempotency_key to the "Idempotency-Key" header.
    idempotency_key: str | None = Header(default=None, min_length=1, max_length=100),
):
    try:
        order, created = place_market_order(user_id, body.symbol, body.side, body.quantity, idempotency_key)
    except SymbolNotFound:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {body.symbol}")
    except MarketDataError:
        raise HTTPException(status_code=503, detail="Market data unavailable, order not placed")
    except IdempotencyKeyReused:
        raise HTTPException(status_code=409, detail="Idempotency-Key was already used for a different order")

    if order["status"] == "REJECTED":
        # Also returned on replay, so a retried request sees the same outcome as the first one.
        raise HTTPException(
            status_code=400,
            detail={"message": "Order rejected", "reason": order["rejection_reason"], "order_id": order["id"]},
        )
    if not created:
        response.status_code = 200  # replay: nothing new was created
        response.headers["Idempotent-Replayed"] = "true"
    return order


@router.get("", response_model=list[OrderResponse])
def order_history(
    status: Literal["PENDING", "EXECUTED", "CANCELLED", "REJECTED"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user_id: int = Depends(get_current_user),
):
    return list_orders(user_id, status, limit, offset)


@router.get("/{order_id}", response_model=OrderResponse)
def order_detail(order_id: int, user_id: int = Depends(get_current_user)):
    order = get_order(user_id, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order
