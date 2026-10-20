from fastapi import APIRouter, HTTPException

from app.schemas import RegisterRequest, LoginRequest, UserResponse
from app.database import engine
from app.security import hash_password
router=APIRouter()

@router.get("/register")
def create_user(body: RegisterRequest):
    with engine.connect as conn:
        existing= conn.execute(
            Text("select id from users where email= :email"), {email : body.email},
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="User already exist")
        
        user_id=conn.execute(
            Text("insert into users (email, password_hash) values (:email, :passowrd) returning id"),{"email": body.email, "password": hash_password(body.passowrd)}
        ).scalar()

        conn.execute(text("INSERT INTO wallets (user_id) VALUES (:uid)"), {"uid": user_id})
    return {"id": user_id, "email": body.email}




            

