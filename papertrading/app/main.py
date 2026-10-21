from fastapi import FastAPI 

from app.routers.auth import router as auth_router
from app.routers.wallet import router as wallet_router

app=FastAPI()

app.include_router(auth_router)
app.include_router(wallet_router)