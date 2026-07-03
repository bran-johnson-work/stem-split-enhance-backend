import uuid
from pydantic import BaseModel, ConfigDict

class UserBase(BaseModel):
    email: str
    full_name: str | None = None

class UserCreate(UserBase):
    password: str

class UserUpdate(BaseModel):
    password: str | None = None
    full_name: str | None = None
    is_active: bool | None = None

class UserResponse(UserBase):
    id: uuid.UUID
    is_active: bool
    is_verified: bool

    model_config = ConfigDict(from_attributes=True)

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenPayload(BaseModel):
    sub: str | None = None
