from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy import text

from app.database import engine
from app.schemas import depositwallet, WalletResponse
from app.security import get_current_user

router=APIRouter(prefix="/wallet")

@router.get("", response_model=WalletResponse)
def get_wallet(user_id: int = Depends(get_current_user)):
    with engine.connect() as conn:
        balance=conn.execute(
            text("select balance from wallets where user_id = :id"),{"id": user_id}
            ).scalar()

        if balance is None:
            raise HTTPException(status_code=404, detail="wallet empty or not found")
        return {"balance": balance}

@router.post("/add_funds", response_model=WalletResponse)
def add_wallet(body: depositwallet, user_id: int = Depends(get_current_user)):
    with engine.connect() as conn:
        balance=conn.execute(
            text("select balance from wallets where user_id = :id for update"),{"id": user_id}
            ).scalar()

        if balance is None:
            raise HTTPException(status_code=404, detail="Balance not found")

        new_balance=balance+body.amount

        conn.execute(
            text("update wallets set balance= :b , updated_at= now() where user_id= :id"),{"b": new_balance, "id": user_id}
        )

        conn.execute(
            text("insert into wallet_transactions (user_id, type, amount, balance_after) values (:id, 'DEPOSIT', :amt, :new)"),{"id": user_id, "amt": body.amount, "new": new_balance}
        )
        conn.commit()
    return {"balance": new_balance}

        

        
    