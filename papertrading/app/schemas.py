from pydantic import BaseModel, EmailStr, Field
from decimal import Decimal

class RegisterRequest(BaseModel):
    email: EmailStr
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

class depositwallet(BaseModel):
    amount: Decimal= Field(gt=0, le=100000)
    
class WalletResponse(BaseModel):
    balance: Decimal

