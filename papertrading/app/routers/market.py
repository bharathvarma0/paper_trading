from fastapi import APIRouter, Depends, HTTPException, Query

from app.schemas import QuoteResponse, SearchResult
from app.security import get_current_user
from app.services.market_data import MarketDataError, SymbolNotFound, get_quote, search_symbols

router = APIRouter()


@router.get("/quote/{symbol}", response_model=QuoteResponse)
def quote(symbol: str, user_id: int = Depends(get_current_user)):
    try:
        q = get_quote(symbol)
    except SymbolNotFound:
        raise HTTPException(status_code=404, detail=f"Unknown symbol: {symbol.upper()}")
    except MarketDataError:
        raise HTTPException(status_code=503, detail="Market data unavailable, try again later")
    return {"symbol": q.symbol, "price": q.price, "previous_close": q.previous_close}


@router.get("/search", response_model=list[SearchResult])
def search(q: str = Query(min_length=1, max_length=50), user_id: int = Depends(get_current_user)):
    try:
        return search_symbols(q)
    except MarketDataError:
        raise HTTPException(status_code=503, detail="Market data unavailable, try again later")
