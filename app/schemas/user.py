import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


_PASSWORD_MIN = 8
_PASSWORD_MAX = 128


class UserBase(BaseModel):
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=255)


class UserCreate(UserBase):

    password: str = Field(min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX)


class UserUpdate(BaseModel):
    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX)
    full_name: str | None = Field(default=None, max_length=255)
    # NOTE: ``is_active`` / ``is_verified`` are server-controlled and deliberately



class UserResponse(UserBase):
    id: uuid.UUID
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenPayload(BaseModel):
    sub: str | None = None
