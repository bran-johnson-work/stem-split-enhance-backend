import logging
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.email import send_email
from app.core.security import (
    create_email_verification_token,
    decode_email_verification_token,
)
from app.models.user import User
from app.repositories.revoked_token_repository import revoked_token_repository
from app.repositories.user_repository import user_repository

logger = logging.getLogger(__name__)


def _wrap(title: str, body_html: str) -> str:
    """Wrap message content in a minimal, client-friendly HTML shell."""
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:480px;'
        'margin:0 auto;color:#1a1a1a;line-height:1.5">'
        f'<h2 style="margin:0 0 16px">{title}</h2>'
        f"{body_html}"
        '<p style="margin-top:32px;font-size:12px;color:#888">'
        "AI Audio Stem Separation</p>"
        "</div>"
    )


def _button(url: str, label: str) -> str:
    return (
        f'<p><a href="{url}" '
        'style="display:inline-block;background:#4f46e5;color:#fff;'
        'text-decoration:none;padding:12px 20px;border-radius:8px">'
        f"{label}</a></p>"
    )


class EmailService:
    """Business logic for composing and sending transactional emails."""

    def __init__(self) -> None:
        self.repository = user_repository
        # Reused as a single-use guard: a consumed verification jti is denylisted.
        self.revoked_tokens = revoked_token_repository

    def _verification_link(self, token: str) -> str:
        settings = get_settings()
        base = settings.frontend_url.rstrip("/")
        return f"{base}/verify-email?token={quote(token)}"

    async def send_verification_email(self, user: User) -> str | None:
        """Send an email-verification link to ``user``.

        Returns the provider message id, or ``None`` if email is disabled.
        Never raises: a failed verification email should not break the flow that
        triggered it (registration, resend). The error is logged instead.
        """
        token = create_email_verification_token(str(user.id), user.email)
        link = self._verification_link(token)
        greeting = f"Hi {user.full_name}," if user.full_name else "Hi,"
        html = _wrap(
            "Confirm your email",
            f"<p>{greeting}</p>"
            "<p>Thanks for signing up. Please confirm your email address to "
            "activate all features of your account.</p>"
            f"{_button(link, 'Verify email')}"
            "<p style=\"font-size:13px;color:#555\">Or paste this link into your "
            f'browser:<br><a href="{link}">{link}</a></p>'
            "<p style=\"font-size:13px;color:#555\">If you didn't create this "
            "account, you can safely ignore this email.</p>",
        )
        try:
            return await send_email(
                to=user.email,
                subject="Verify your email address",
                html=html,
                text=f"{greeting}\nVerify your email address: {link}",
            )
        except Exception:  # pragma: no cover - defensive; send_email logs details
            logger.exception("Failed to send verification email to %s", user.email)
            return None

    async def verify_email_token(self, db: AsyncSession, token: str) -> User | None:
        """Validate a verification token and mark the user's email as verified.

        Single-use: the token's ``jti`` is added to the revoked-token denylist
        once consumed, so the same link cannot be replayed. Returns the user on
        success, or ``None`` if the token is invalid/expired/already-used or its
        subject no longer exists.
        """
        try:
            payload = decode_email_verification_token(token)
        except jwt.PyJWTError as exc:
            logger.info("Rejected email-verification token: %s", exc)
            return None

        jti = payload.get("jti")
        if jti and await self.revoked_tokens.is_revoked(db, jti):
            logger.info("Rejected already-used verification token jti=%s", jti)
            return None

        try:
            user_id = uuid.UUID(str(payload.get("sub")))
        except ValueError:
            return None

        user = await self.repository.get(db, user_id)
        if user is None:
            return None
        # Guard against a token issued before the user changed their email.
        if payload.get("email") and payload["email"] != user.email:
            logger.info("Verification token email mismatch for user_id=%s", user_id)
            return None

        # Consume the token (single-use) and flip the flag in one transaction.
        if jti:
            expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
            await self.revoked_tokens.add(db, jti, expires_at)
        if not user.is_verified:
            user = await self.repository.update(db, user, {"is_verified": True})
        await db.commit()
        logger.info("Email verified for user_id=%s (token consumed)", user_id)
        return user


email_service = EmailService()
