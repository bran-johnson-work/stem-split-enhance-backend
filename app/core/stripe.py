"""Low-level Stripe transport (Stripe REST API over httpx).

The cross-cutting client layer for billing — the counterpart to ``storage.py`` /
``email.py`` — with no business logic: it puts requests on the wire and verifies
webhook signatures. Composing checkout/portal/subscription flows is the job of
``app/services/billing_service.py``.

We talk to Stripe's HTTP API directly with an async ``httpx`` client (form-encoded,
as Stripe expects) rather than the synchronous ``stripe`` SDK, so calls never block
the event loop — consistent with the Resend email transport. Webhook signatures are
verified with the stdlib ``hmac`` (Stripe's documented ``t=…,v1=…`` scheme), so we
need no SDK for that either.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Stripe's default webhook timestamp tolerance (seconds) to reject replayed events.
DEFAULT_SIGNATURE_TOLERANCE = 300


class StripeError(RuntimeError):
    """Raised when Stripe rejects a request or the transport fails."""


class SignatureVerificationError(StripeError):
    """Raised when a webhook signature is missing, malformed, or does not match."""


def _flatten(data: dict[str, Any], parent: str = "") -> dict[str, str]:
    """Flatten a nested dict/list into Stripe's bracketed form-encoding.

    ``{"line_items": [{"price": "p", "quantity": 1}]}`` becomes
    ``{"line_items[0][price]": "p", "line_items[0][quantity]": "1"}``. ``None``
    values are dropped so optional params can be passed unconditionally.
    """
    out: dict[str, str] = {}
    for key, value in data.items():
        composite = f"{parent}[{key}]" if parent else key
        if value is None:
            continue
        if isinstance(value, dict):
            out.update(_flatten(value, composite))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                idx = f"{composite}[{i}]"
                if isinstance(item, dict):
                    out.update(_flatten(item, idx))
                else:
                    out[idx] = _coerce(item)
        else:
            out[composite] = _coerce(value)
    return out


def _coerce(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


async def _request(method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Send a form-encoded request to a Stripe API path and return the JSON body."""
    settings = get_settings()
    if not settings.billing_enabled:
        raise StripeError("Stripe is not configured (STRIPE_SECRET_KEY unset).")

    url = f"{settings.stripe_api_base_url}/v1/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {settings.stripe_secret_key}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    # Stripe reads list/read filters from the query string on GET, and form body
    # on write verbs. Flattened either way (Stripe uses the same bracketed form
    # for nested query params, e.g. ``created[gte]``).
    flat = _flatten(data or {})
    request_kwargs: dict[str, Any] = {"params": flat} if method == "GET" else {"data": flat}
    try:
        async with httpx.AsyncClient(timeout=settings.stripe_timeout_seconds) as client:
            response = await client.request(method, url, headers=headers, **request_kwargs)
    except httpx.HTTPError as exc:  # transport error (DNS, timeout, connection)
        logger.error("Stripe transport error on %s: %s", path, exc)
        raise StripeError(f"Stripe transport error: {exc}") from exc

    if response.status_code >= 400:
        logger.error("Stripe API error on %s status=%s body=%s", path, response.status_code,
                     response.text)
        raise StripeError(f"Stripe API error {response.status_code}: {response.text}")
    return response.json()


async def _post(path: str, data: dict[str, Any]) -> dict[str, Any]:
    return await _request("POST", path, data)


async def _get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return await _request("GET", path, params)


async def create_customer(*, email: str, user_id: str) -> dict[str, Any]:
    """Create a Stripe Customer linked back to our user via metadata."""
    return await _post("customers", {"email": email, "metadata": {"user_id": user_id}})


async def create_product(*, name: str, metadata: dict[str, str] | None = None) -> dict[str, Any]:
    """Create a Stripe Product (the sellable thing a plan maps to)."""
    return await _post("products", {"name": name, "metadata": metadata or {}})


async def create_price(
    *,
    product_id: str,
    unit_amount: int,
    interval: str,
    currency: str = "usd",
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a recurring Price under a Product (``interval`` = 'month' | 'year')."""
    return await _post(
        "prices",
        {
            "product": product_id,
            "unit_amount": unit_amount,
            "currency": currency,
            "recurring": {"interval": interval},
            "metadata": metadata or {},
        },
    )


async def create_checkout_session(
    *,
    customer_id: str,
    price_id: str,
    success_url: str,
    cancel_url: str,
    client_reference_id: str,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a subscription-mode Checkout Session; returns the session (with ``url``).

    ``client_reference_id`` (our user id) is echoed back on
    ``checkout.session.completed`` — the link Stripe's ``customer.subscription.*``
    events do not carry — and is also copied onto the subscription's metadata.
    """
    payload: dict[str, Any] = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": client_reference_id,
        "subscription_data": {"metadata": {"user_id": client_reference_id, **(metadata or {})}},
    }
    if metadata:
        payload["metadata"] = metadata
    return await _post("checkout/sessions", payload)


async def create_portal_session(*, customer_id: str, return_url: str) -> dict[str, Any]:
    """Create a Customer Portal session (self-serve cancel / switch / update card)."""
    return await _post(
        "billing_portal/sessions", {"customer": customer_id, "return_url": return_url}
    )


async def cancel_subscription_at_period_end(*, subscription_id: str) -> dict[str, Any]:
    """Schedule a subscription to cancel at the end of the paid period.

    Access is retained until then; Stripe emits ``customer.subscription.updated``
    now (with ``cancel_at_period_end=true``) and ``customer.subscription.deleted``
    when the period ends. Returns the updated subscription object.
    """
    return await _post(f"subscriptions/{subscription_id}", {"cancel_at_period_end": True})


async def resume_subscription(*, subscription_id: str) -> dict[str, Any]:
    """Undo a scheduled cancellation (``cancel_at_period_end`` back to false)."""
    return await _post(f"subscriptions/{subscription_id}", {"cancel_at_period_end": False})


async def cancel_subscription_now(*, subscription_id: str) -> dict[str, Any]:
    """Cancel a subscription immediately (DELETE); access ends at once."""
    return await _request("DELETE", f"subscriptions/{subscription_id}")


async def list_invoices(*, customer_id: str, limit: int = 24) -> list[dict[str, Any]]:
    """List a customer's invoices, newest first (Stripe's default order).

    Stripe is the system of record for invoices — it renders and hosts the PDF —
    so the app reads them on demand rather than keeping its own copies. Returns
    the raw ``data`` array; ``billing_service`` maps it to the API DTO.
    """
    response = await _get("invoices", {"customer": customer_id, "limit": limit})
    data = response.get("data")
    return data if isinstance(data, list) else []


def construct_event(
    payload: bytes,
    sig_header: str | None,
    secret: str,
    tolerance: int = DEFAULT_SIGNATURE_TOLERANCE,
) -> dict[str, Any]:
    """Verify a webhook signature over the raw body and return the parsed event.

    Implements Stripe's ``Stripe-Signature: t=<ts>,v1=<hex>`` scheme: the signed
    payload is ``"{t}.{raw_body}"`` HMAC-SHA256'd with the endpoint ``secret``.
    Raises :class:`SignatureVerificationError` on any mismatch, a stale timestamp,
    or a missing/malformed header — the caller turns that into a 400.
    """
    if not sig_header:
        raise SignatureVerificationError("Missing Stripe-Signature header.")

    timestamp: str | None = None
    signatures: list[str] = []
    for part in sig_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)

    if timestamp is None or not signatures:
        raise SignatureVerificationError("Malformed Stripe-Signature header.")

    if tolerance:
        try:
            if abs(time.time() - int(timestamp)) > tolerance:
                raise SignatureVerificationError("Timestamp outside the tolerance zone.")
        except ValueError as exc:
            raise SignatureVerificationError("Invalid signature timestamp.") from exc

    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureVerificationError("Signature mismatch.")

    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SignatureVerificationError("Webhook payload is not valid JSON.") from exc
