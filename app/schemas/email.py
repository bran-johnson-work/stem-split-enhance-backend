from pydantic import BaseModel, EmailStr, Field

# Kept in sync with app.schemas.user password bounds.
_PASSWORD_MIN = 8
_PASSWORD_MAX = 128


class MessageResponse(BaseModel):
    """Generic, non-enumerable acknowledgement for email-triggering endpoints."""

    detail: str


class VerificationRequest(BaseModel):
    """Ask for a fresh verification email to be sent to ``email``."""

    email: EmailStr


class VerifyEmailRequest(BaseModel):
    """Confirm ownership of an email by presenting the token from the link."""

    token: str


class ForgotPasswordRequest(BaseModel):
    """Ask for a password-reset link to be emailed to ``email``."""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Set a new password by presenting the token from the reset link."""

    token: str
    new_password: str = Field(min_length=_PASSWORD_MIN, max_length=_PASSWORD_MAX)
