"""Low-level transactional-email transport (Resend REST API).

This is the cross-cutting client layer — the counterpart to ``storage.py`` — and
carries no business logic: callers pass a fully-rendered subject/body and it puts
the message on the wire. Composing the actual messages (templates, verification
links) is the job of ``app/services/email_service.py``.

We talk to Resend's HTTP API directly with an async ``httpx`` client rather than
the synchronous ``resend`` SDK so sends never block the event loop.
"""

import logging
from collections.abc import Sequence

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class EmailError(RuntimeError):
    """Raised when the email provider rejects or fails to accept a message."""


async def send_email(
    *,
    to: str | Sequence[str],
    subject: str,
    html: str,
    text: str | None = None,
    reply_to: str | None = None,
) -> str | None:
    """Send an email via Resend. Returns the provider message id.

    If email is not configured (no ``RESEND_API_KEY``) this is a no-op that logs
    a warning and returns ``None`` — so local dev and the test suite stay
    network-free without every caller having to guard the call. Raises
    :class:`EmailError` when email *is* configured but the send fails, letting
    callers decide whether that is fatal (it usually is not — see the fire-and-
    forget background sends in the auth flow).
    """
    settings = get_settings()
    recipients = [to] if isinstance(to, str) else list(to)

    if not settings.email_enabled:
        logger.warning(
            "Email disabled (RESEND_API_KEY unset); skipping send to=%s subject=%r",
            recipients,
            subject,
        )
        return None

    payload: dict = {
        "from": settings.email_sender,
        "to": recipients,
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    if reply_to:
        payload["reply_to"] = reply_to

    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{settings.resend_api_base_url}/emails"

    try:
        async with httpx.AsyncClient(timeout=settings.email_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:  # transport error (DNS, timeout, connection)
        logger.error("Email transport error to=%s: %s", recipients, exc)
        raise EmailError(f"Email transport error: {exc}") from exc

    if response.status_code >= 400:
        logger.error(
            "Email rejected to=%s status=%s body=%s",
            recipients,
            response.status_code,
            response.text,
        )
        raise EmailError(f"Resend API error {response.status_code}: {response.text}")

    message_id = response.json().get("id")
    logger.info("Email sent to=%s id=%s subject=%r", recipients, message_id, subject)
    return message_id
