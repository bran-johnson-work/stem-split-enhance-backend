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
    create_password_reset_token,
    decode_email_verification_token,
    decode_password_reset_token,
    hash_password,
)
from app.models.user import User
from app.repositories.refresh_token_repository import refresh_token_repository
from app.repositories.revoked_token_repository import revoked_token_repository
from app.repositories.user_repository import user_repository

logger = logging.getLogger(__name__)


# --- Brand -----------------------------------------------------------------
# KlearNote visual identity. Kept here (not in Settings) because it's design,
# not configuration — every transactional email shares this look.
_BRAND_NAME = "KlearNote"
_BRAND_TAGLINE = "AI Audio Stem Separation"
_BRAND_INK = "#0f172a"  # near-black headings
_BRAND_BODY = "#475569"  # muted body text
_BRAND_ACCENT = "#6366f1"  # indigo
_BRAND_ACCENT_2 = "#8b5cf6"  # violet (gradient partner)
_BRAND_PAGE_BG = "#eef0f6"  # page canvas behind the card
_FONT_STACK = (
    "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
)


def _logo() -> str:
    """Branded logo lockup: a gradient badge with a waveform glyph + wordmark.

    Uses only inline styles + a Unicode glyph so it renders without external
    images (which most clients block by default) or SVG (stripped by Gmail).
    """
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:0 auto"><tr>'
        '<td style="vertical-align:middle;padding-right:10px">'
        '<div style="width:40px;height:40px;border-radius:11px;'
        f"background:{_BRAND_ACCENT};"
        f"background-image:linear-gradient(135deg,{_BRAND_ACCENT} 0%,{_BRAND_ACCENT_2} 100%);"
        'text-align:center;line-height:40px;font-size:20px;color:#ffffff">'
        "&#127925;</div></td>"  # 🎵 musical note
        '<td style="vertical-align:middle">'
        f'<span style="font-family:{_FONT_STACK};font-size:20px;font-weight:700;'
        f'color:{_BRAND_INK};letter-spacing:-0.02em">Klear'
        f'<span style="color:{_BRAND_ACCENT}">Note</span></span></td>'
        "</tr></table>"
    )


def _wrap(title: str, body_html: str, *, preheader: str = "") -> str:
    """Wrap message content in the branded, client-friendly KlearNote shell.

    A centered white card on a soft canvas, table-based with inline styles for
    broad email-client support (Gmail, Outlook, Apple Mail). ``preheader`` is the
    hidden inbox-preview snippet shown next to the subject line.
    """
    hidden_preheader = (
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;'
        f'mso-hide:all">{preheader}</div>'
        if preheader
        else ""
    )
    return (
        f'<div style="background:{_BRAND_PAGE_BG};margin:0;padding:32px 16px;'
        f'font-family:{_FONT_STACK}">'
        f"{hidden_preheader}"
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="100%" style="max-width:520px;margin:0 auto">'
        # Logo header
        '<tr><td style="text-align:center;padding:4px 0 24px">'
        f"{_logo()}</td></tr>"
        # Card
        '<tr><td style="background:#ffffff;border-radius:16px;'
        'padding:40px 40px 36px;'
        'box-shadow:0 1px 3px rgba(15,23,42,0.06),0 8px 24px rgba(15,23,42,0.05)">'
        # Accent pill
        f'<div style="width:44px;height:4px;border-radius:2px;'
        f"background:{_BRAND_ACCENT};"
        f"background-image:linear-gradient(90deg,{_BRAND_ACCENT},{_BRAND_ACCENT_2});"
        'margin:0 0 24px"></div>'
        f'<h1 style="margin:0 0 16px;font-size:24px;line-height:1.3;'
        f'font-weight:700;color:{_BRAND_INK};letter-spacing:-0.02em">{title}</h1>'
        f'<div style="font-size:15px;line-height:1.65;color:{_BRAND_BODY}">'
        f"{body_html}</div>"
        "</td></tr>"
        # Footer
        '<tr><td style="text-align:center;padding:24px 16px 4px;'
        f'font-size:12px;line-height:1.6;color:#94a3b8">'
        f"<p style=\"margin:0 0 4px;font-weight:600;color:#64748b\">{_BRAND_NAME}</p>"
        f'<p style="margin:0">{_BRAND_TAGLINE}</p>'
        f'<p style="margin:8px 0 0">You received this email because an account '
        f"was created with this address.</p>"
        "</td></tr>"
        "</table></div>"
    )


def _button(url: str, label: str) -> str:
    """A prominent, gradient call-to-action button (solid-color fallback for Outlook)."""
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'style="margin:28px 0"><tr><td style="border-radius:10px;'
        f"background:{_BRAND_ACCENT};"
        f'background-image:linear-gradient(135deg,{_BRAND_ACCENT} 0%,{_BRAND_ACCENT_2} 100%)">'
        f'<a href="{url}" style="display:inline-block;padding:14px 32px;'
        'font-size:15px;font-weight:600;color:#ffffff;text-decoration:none;'
        'border-radius:10px">'
        f"{label}</a></td></tr></table>"
    )


class EmailService:
    """Business logic for composing and sending transactional emails."""

    def __init__(self) -> None:
        self.repository = user_repository
        # Reused as a single-use guard: a consumed verification/reset jti is denylisted.
        self.revoked_tokens = revoked_token_repository
        self.refresh_tokens = refresh_token_repository

    def _verification_link(self, token: str) -> str:
        settings = get_settings()
        base = settings.frontend_url.rstrip("/")
        return f"{base}/verify-email?token={quote(token)}"

    def _password_reset_link(self, token: str) -> str:
        settings = get_settings()
        base = settings.frontend_url.rstrip("/")
        return f"{base}/reset-password?token={quote(token)}"

    async def send_verification_email(self, user: User) -> str | None:
        """Send an email-verification link to ``user``.

        Returns the provider message id, or ``None`` if email is disabled.
        Never raises: a failed verification email should not break the flow that
        triggered it (registration, resend). The error is logged instead.
        """
        token = create_email_verification_token(str(user.id), user.email)
        link = self._verification_link(token)

        if get_settings().is_local:
            logger.info("Local email-verification link for %s: %s", user.email, link)
        greeting = f"Hi {user.full_name}," if user.full_name else "Hey there,"
        html = _wrap(
            "Confirm your email",
            f'<p style="margin:0 0 16px">{greeting}</p>'
            '<p style="margin:0 0 16px">Welcome to KlearNote &#127911; — where your '
            "tracks get split into clean, studio-ready stems. You're one click away "
            "from getting started.</p>"
            '<p style="margin:0 0 8px">Tap the button below to confirm your email '
            "and unlock your account:</p>"
            f"{_button(link, 'Confirm my email')}"
            '<p style="margin:0 0 8px;font-size:13px;color:#94a3b8">'
            "Button not working? Paste this link into your browser:</p>"
            f'<p style="margin:0 0 24px;font-size:13px;word-break:break-all">'
            f'<a href="{link}" style="color:{_BRAND_ACCENT};text-decoration:none">'
            f"{link}</a></p>"
            f'<div style="border-top:1px solid #eef0f6;margin:24px 0 0;padding-top:16px">'
            '<p style="margin:0;font-size:13px;color:#94a3b8">This link expires in '
            "24 hours. If you didn't create a KlearNote account, you can safely "
            "ignore this email.</p></div>",
            preheader="Confirm your email to activate your KlearNote account.",
        )
        try:
            return await send_email(
                to=user.email,
                subject="Confirm your email · KlearNote",
                html=html,
                text=(
                    f"{greeting}\n\n"
                    "Welcome to KlearNote! Confirm your email to activate your "
                    f"account:\n{link}\n\n"
                    "This link expires in 24 hours. If you didn't create a "
                    "KlearNote account, you can ignore this email."
                ),
            )
        except Exception:  # pragma: no cover - defensive; send_email logs details
            logger.exception("Failed to send verification email to %s", user.email)
            return None

    async def send_password_reset_email(self, user: User) -> str | None:
        """Send a password-reset link to ``user``.

        Returns the provider message id, or ``None`` if email is disabled. Never
        raises: a failed reset email should not break the endpoint that triggered
        it (which stays generic to avoid account enumeration). The error is logged.
        """
        token = create_password_reset_token(str(user.id), user.email)
        link = self._password_reset_link(token)

        if get_settings().is_local:
            logger.info("Local password-reset link for %s: %s", user.email, link)
        greeting = f"Hi {user.full_name}," if user.full_name else "Hey there,"
        html = _wrap(
            "Reset your password",
            f'<p style="margin:0 0 16px">{greeting}</p>'
            '<p style="margin:0 0 16px">We received a request to reset the password '
            "for your KlearNote account. Tap the button below to choose a new one:</p>"
            f"{_button(link, 'Reset my password')}"
            '<p style="margin:0 0 8px;font-size:13px;color:#94a3b8">'
            "Button not working? Paste this link into your browser:</p>"
            f'<p style="margin:0 0 24px;font-size:13px;word-break:break-all">'
            f'<a href="{link}" style="color:{_BRAND_ACCENT};text-decoration:none">'
            f"{link}</a></p>"
            f'<div style="border-top:1px solid #eef0f6;margin:24px 0 0;padding-top:16px">'
            '<p style="margin:0;font-size:13px;color:#94a3b8">This link expires in '
            "1 hour. If you didn't request a password reset, you can safely ignore "
            "this email — your password won't change.</p></div>",
            preheader="Reset your KlearNote password.",
        )
        try:
            return await send_email(
                to=user.email,
                subject="Reset your password · KlearNote",
                html=html,
                text=(
                    f"{greeting}\n\n"
                    "We received a request to reset your KlearNote password. Use this "
                    f"link to choose a new one:\n{link}\n\n"
                    "This link expires in 1 hour. If you didn't request a password "
                    "reset, you can ignore this email — your password won't change."
                ),
            )
        except Exception:  # pragma: no cover - defensive; send_email logs details
            logger.exception("Failed to send password-reset email to %s", user.email)
            return None

    async def reset_password(
        self, db: AsyncSession, token: str, new_password: str
    ) -> User | None:
        """Validate a reset token and set the user's new password.

        Single-use: the token's ``jti`` is denylisted once consumed, so the same
        link cannot be replayed. On success every live refresh token for the user
        is revoked ("sign out everywhere") — a password reset is exactly the moment
        to end any session an attacker may hold. Returns the user on success, or
        ``None`` if the token is invalid/expired/already-used, its subject no longer
        exists (or is inactive), or its bound email no longer matches.
        """
        try:
            payload = decode_password_reset_token(token)
        except jwt.PyJWTError as exc:
            logger.info("Rejected password-reset token: %s", exc)
            return None

        jti = payload.get("jti")
        if jti and await self.revoked_tokens.is_revoked(db, jti):
            logger.info("Rejected already-used password-reset token jti=%s", jti)
            return None

        try:
            user_id = uuid.UUID(str(payload.get("sub")))
        except ValueError:
            return None

        user = await self.repository.get(db, user_id)
        if user is None or not user.is_active:
            return None
        # Guard against a token issued before the user changed their email.
        if payload.get("email") and payload["email"] != user.email:
            logger.info("Password-reset token email mismatch for user_id=%s", user_id)
            return None

        # Consume the token (single-use), set the new hash, and end all sessions —
        # all in one transaction.
        if jti:
            expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
            await self.revoked_tokens.add(db, jti, expires_at)
        # Completing a reset over an emailed link also proves email ownership.
        updates: dict = {"password_hash": hash_password(new_password)}
        if not user.is_verified:
            updates["is_verified"] = True
        user = await self.repository.update(db, user, updates)
        revoked = await self.refresh_tokens.revoke_all_for_user(db, user.id)
        await db.commit()
        logger.info(
            "Password reset for user_id=%s (token consumed, %d session(s) revoked)",
            user_id,
            revoked,
        )
        return user

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
