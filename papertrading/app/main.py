import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import FINNHUB_KEY
from app.routers.auth import router as auth_router
from app.routers.market import router as market_router
from app.routers.orders import router as orders_router
from app.routers.prices import router as prices_router
from app.routers.wallet import router as wallet_router
from app.services.price_stream import price_stream

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the single Finnhub WebSocket connection with the app and stop it on shutdown.
    # Without a key (e.g. in tests) the app still works, just without live prices.
    task = asyncio.create_task(price_stream.run()) if FINNHUB_KEY else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Paper Trading Platform", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(wallet_router)
app.include_router(market_router)
app.include_router(orders_router)
app.include_router(prices_router)


@app.get("/health")
def health():
    return {"status": "ok"}


# The UI is served from the same origin as the API, so the browser needs no CORS setup.
app.mount("/ui", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="ui")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/ui/")
