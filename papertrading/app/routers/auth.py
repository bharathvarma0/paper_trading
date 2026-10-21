from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from app.database import engine
from app.schemas import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.security import create_token, get_current_user, hash_password, verify_password

router=APIRouter()

@router.post("/register")
def create_user(body: RegisterRequest):
    with engine.connect() as conn:
        existing= conn.execute(
            text("select id from users where email= :email"), {"email": body.email},
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="User already exist")

        user_id=conn.execute(
            text("insert into users (email, password_hash) values (:email, :password) returning id"),{"email": body.email, "password": hash_password(body.password)}
        ).scalar()

        conn.execute(text("INSERT INTO wallets (user_id) VALUES (:uid)"), {"uid": user_id})
        conn.commit()
    return {"id": user_id, "email": body.email}


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, password_hash FROM users WHERE email = :email"),
            {"email": body.email},
        ).first()

    if row is None or not verify_password(body.password, row.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return {"access_token": create_token(row.id)}


@router.get("/me", response_model=UserResponse)
def me(user_id: int = Depends(get_current_user)):
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, email FROM users WHERE id = :id"),
            {"id": user_id},
        ).first()
    return {"id": row.id, "email": row.email}



            

