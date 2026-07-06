from pydantic import BaseModel, EmailStr


class MessageResponse(BaseModel):
    """Generic, non-enumerable acknowledgement for email-triggering endpoints."""

    detail: str


class VerificationRequest(BaseModel):
    """Ask for a fresh verification email to be sent to ``email``."""

    email: EmailStr


class VerifyEmailRequest(BaseModel):
    """Confirm ownership of an email by presenting the token from the link."""

    token: str
