import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole

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
    role: UserRole
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class Token(BaseModel):
    access_token: str
    token_type: str
    # Opaque, rotating. Absent only in flows that deliberately don't start a
    # long-lived session.
    refresh_token: str | None = None
    # Access-token lifetime in seconds, so the client can refresh *before* it
    # expires instead of waiting for a 401 it could have avoided.
    expires_in: int | None = None


class RefreshRequest(BaseModel):
    """Exchange a refresh token for a fresh access token (and a new refresh)."""

    refresh_token: str


class LogoutRequest(BaseModel):
    """Optional logout body. Sending the refresh token ends the whole session,
    not just the presented access token."""

    refresh_token: str | None = None


class TokenPayload(BaseModel):
    sub: str | None = None
