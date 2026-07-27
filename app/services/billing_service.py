"""Business logic for Stripe subscriptions.

Owns the checkout / portal flows and the webhook state machine. The webhook is
the **source of truth**: the browser redirect after payment is treated as UX
only, and all subscription state is written from verified ``customer.subscription.*``
events. Every write path is idempotent (webhooks retry and arrive out of order).
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import stripe
from app.core.config import get_settings
from app.models.plan import Plan
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import User
from app.repositories.billing_event_repository import billing_event_repository
from app.repositories.plan_repository import plan_repository
from app.repositories.subscription_repository import subscription_repository
from app.schemas.billing import (
    BillingEventCreate,
    InvoiceResponse,
    PlanResponse,
    SubscriptionCreate,
    SubscriptionResponse,
)

logger = logging.getLogger(__name__)

# The plan a user falls back to when they have no subscription row (mirrors
# ``project_service.DEFAULT_PLAN_NAME`` — the free tier is the system default).
DEFAULT_PLAN_NAME = "free"


class BillingDisabledError(RuntimeError):
    """Raised when a billing action is attempted while Stripe is unconfigured."""


class PlanNotFoundError(LookupError):
    """Raised when a requested plan does not exist or is inactive."""


class BillingRequestError(ValueError):
    """Raised for a bad billing request (free plan, missing price, no account)."""


# Stripe subscription.status -> our enum. Stripe spells it "canceled" (one l).
_STATUS_MAP = {
    "trialing": SubscriptionStatus.TRIALING,
    "active": SubscriptionStatus.ACTIVE,
    "past_due": SubscriptionStatus.PAST_DUE,
    "unpaid": SubscriptionStatus.PAST_DUE,
    "canceled": SubscriptionStatus.CANCELLED,
    "incomplete": SubscriptionStatus.INCOMPLETE,
    "incomplete_expired": SubscriptionStatus.INCOMPLETE_EXPIRED,
}


def _map_status(stripe_status: str | None) -> SubscriptionStatus:
    return _STATUS_MAP.get(stripe_status or "", SubscriptionStatus.INCOMPLETE)


def _ts(value: Any) -> datetime | None:
    """Convert a Stripe unix timestamp to an aware datetime (None passthrough)."""
    if not value:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _first_price_id(subscription_obj: dict[str, Any]) -> str | None:
    try:
        return subscription_obj["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError, TypeError):
        return None


def _first_item(subscription_obj: dict[str, Any]) -> dict[str, Any] | None:
    """The subscription's first line item (``{id, price, ...}``), or None."""
    try:
        item = subscription_obj["items"]["data"][0]
    except (KeyError, IndexError, TypeError):
        return None
    return item if isinstance(item, dict) else None


def _first_period(subscription_obj: dict[str, Any]) -> tuple[Any, Any]:
    """The current billing period ``(start, end)`` as raw Stripe timestamps.

    Newer Stripe API versions report ``current_period_start`` / ``current_period_end``
    on the subscription **item**, not the subscription object; older versions put
    them at the top level. Read the item first and fall back, so the renewal date
    is captured on every version — otherwise an annual sub (item period a year out)
    stores a null period end and the app can't show when it renews.
    """
    item = _first_item(subscription_obj) or {}
    start = item.get("current_period_start") or subscription_obj.get("current_period_start")
    end = item.get("current_period_end") or subscription_obj.get("current_period_end")
    return start, end


# Months in a year — used to annualise a monthly price so a monthly plan and an
# annual plan can be compared on the same footing (upgrade vs downgrade).
_MONTHS_PER_YEAR = 12


def _annualised_cents(plan: Plan, interval: str) -> int:
    """A plan's yearly cost for the given cadence, so plans on different cadences
    (monthly vs annual) compare like-for-like when classifying a change."""
    if interval == "annual":
        return plan.price_annual_cents
    return plan.price_monthly_cents * _MONTHS_PER_YEAR


# Stripe's price.recurring.interval -> our stored cadence. Only month/year are
# offered; anything else (or a missing recurring block) leaves it unknown.
_INTERVAL_MAP = {"month": "monthly", "year": "annual"}


def _first_interval(subscription_obj: dict[str, Any]) -> str | None:
    """The billing cadence ("monthly"/"annual") from a Stripe subscription object.

    Read off the first item's price recurring interval, which the subscription
    row doesn't otherwise persist. Returns ``None`` when it can't be determined
    so an existing value isn't clobbered with a guess.
    """
    try:
        interval = subscription_obj["items"]["data"][0]["price"]["recurring"]["interval"]
    except (KeyError, IndexError, TypeError):
        return None
    return _INTERVAL_MAP.get(interval)


# Stripe invoice.status -> the three states the UI renders. Anything unpayable
# (voided, written off) reads as a failed charge; a draft hasn't been issued yet
# but is filtered out before this map is reached.
_INVOICE_STATUS_MAP = {
    "paid": "paid",
    "open": "open",
    "draft": "open",
    "uncollectible": "failed",
    "void": "failed",
}


def _map_invoice_status(stripe_status: str | None) -> str:
    return _INVOICE_STATUS_MAP.get(stripe_status or "", "open")


def _invoice_description(inv: dict[str, Any]) -> str | None:
    """A human label for the invoice.

    Stripe rarely fills the top-level ``description`` on a subscription invoice,
    so fall back to the first line item's — which reads like "Pro — monthly" when
    the price carries a nickname. Never fabricated: the client shows
    "Subscription" when this is null.
    """
    if inv.get("description"):
        return inv["description"]
    lines = (inv.get("lines") or {}).get("data") or []
    if lines and lines[0].get("description"):
        return lines[0]["description"]
    return None


def _is_listable_invoice(inv: dict[str, Any]) -> bool:
    """Hide drafts — they aren't finalised, carry no stable amount, and would
    confuse a receipts list."""
    return inv.get("status") != "draft"


def _map_invoice(inv: dict[str, Any]) -> "InvoiceResponse":
    # Stripe amounts are in the currency's minor unit (cents); the API speaks
    # dollars, matching how plan prices are surfaced. ``total`` is the invoice's
    # definitive amount (``amount_paid`` is 0 on an unpaid one).
    total_cents = inv.get("total")
    amount = (total_cents or 0) / 100
    return InvoiceResponse(
        id=str(inv["id"]),
        created_at=datetime.fromtimestamp(int(inv.get("created", 0)), tz=timezone.utc),
        amount=amount,
        currency=str(inv.get("currency", "usd")).upper(),
        status=_map_invoice_status(inv.get("status")),
        description=_invoice_description(inv),
        hosted_invoice_url=inv.get("hosted_invoice_url"),
        invoice_pdf=inv.get("invoice_pdf"),
    )


def _extract_user_id(obj: dict[str, Any]) -> uuid.UUID | None:
    """Pull our user id from a Stripe object's metadata / client_reference_id."""
    raw = (obj.get("metadata") or {}).get("user_id") or obj.get("client_reference_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


class BillingService:
    """Checkout, Customer Portal, and webhook synchronisation."""

    async def list_plans(self, db: AsyncSession) -> Sequence[Plan]:
        return await plan_repository.list_active(db)

    async def get_subscription(
        self, db: AsyncSession, user_id: uuid.UUID
    ) -> Subscription | None:
        return await subscription_repository.get_for_user(db, user_id)

    async def get_subscription_view(
        self, db: AsyncSession, user_id: uuid.UUID
    ) -> SubscriptionResponse | None:
        """Build the effective subscription view for a user.

        Returns the real subscription when one exists; otherwise a free-plan
        default (``has_subscription=False``) so the endpoint never returns
        ``null`` for a normal account. Returns ``None`` only in the degenerate
        case where even the free plan is unseeded.
        """
        subscription = await subscription_repository.get_for_user(db, user_id)
        if subscription is not None:
            return SubscriptionResponse(
                has_subscription=True,
                status=subscription.status,
                plan=PlanResponse.model_validate(subscription.plan),
                interval=subscription.billing_interval,
                current_period_start=subscription.current_period_start,
                current_period_end=subscription.current_period_end,
                cancel_at_period_end=subscription.cancel_at_period_end,
                cancelled_at=subscription.cancelled_at,
            )

        free = await plan_repository.get_by_name(db, DEFAULT_PLAN_NAME)
        if free is None:
            logger.warning("No '%s' plan seeded; returning null subscription.", DEFAULT_PLAN_NAME)
            return None
        return SubscriptionResponse(
            has_subscription=False,
            status=None,
            plan=PlanResponse.model_validate(free),
            current_period_start=None,
            current_period_end=None,
            cancel_at_period_end=False,
            cancelled_at=None,
        )

    async def list_invoices(
        self, db: AsyncSession, user_id: uuid.UUID
    ) -> list[InvoiceResponse]:
        """The caller's invoices, read live from Stripe (newest first).

        Returns ``[]`` rather than erroring for anyone without a billing account
        — a free user, or one who never completed checkout, simply has no
        receipts. Only a genuine Stripe transport failure propagates.
        """
        settings = get_settings()
        if not settings.billing_enabled:
            return []

        subscription = await subscription_repository.get_for_user(db, user_id)
        if subscription is None or not subscription.stripe_customer_id:
            return []

        raw_invoices = await stripe.list_invoices(
            customer_id=subscription.stripe_customer_id
        )
        return [_map_invoice(inv) for inv in raw_invoices if _is_listable_invoice(inv)]

    # --- Checkout / Portal ------------------------------------------------

    async def create_checkout_session(
        self, db: AsyncSession, *, user: User, plan_name: str, interval: str
    ) -> str:
        """Create a Stripe Checkout Session and return its hosted URL."""
        settings = get_settings()
        if not settings.billing_enabled:
            raise BillingDisabledError("Billing is not configured.")

        plan = await plan_repository.get_by_name(db, plan_name)
        if plan is None or not plan.is_active:
            raise PlanNotFoundError(f"Unknown plan '{plan_name}'.")
        if plan.price_monthly_cents == 0 and plan.price_annual_cents == 0:
            raise BillingRequestError("The free plan does not require checkout.")

        price_id = (
            plan.stripe_price_id_annual if interval == "annual" else plan.stripe_price_id_monthly
        )
        if not price_id:
            raise BillingRequestError(
                f"Plan '{plan_name}' is not available for {interval} billing yet."
            )

        customer_id = await self._ensure_customer(db, user, plan)
        session = await stripe.create_checkout_session(
            customer_id=customer_id,
            price_id=price_id,
            success_url=settings.checkout_success_url,
            cancel_url=settings.checkout_cancel_url,
            client_reference_id=str(user.id),
            metadata={"plan_name": plan_name, "interval": interval},
        )
        await db.commit()
        return session["url"]

    async def create_portal_session(self, db: AsyncSession, *, user: User) -> str:
        """Create a Customer Portal session for self-serve management."""
        settings = get_settings()
        if not settings.billing_enabled:
            raise BillingDisabledError("Billing is not configured.")

        subscription = await subscription_repository.get_for_user(db, user.id)
        if subscription is None or not subscription.stripe_customer_id:
            raise BillingRequestError("No billing account exists for this user yet.")

        session = await stripe.create_portal_session(
            customer_id=subscription.stripe_customer_id,
            return_url=settings.portal_return_url,
        )
        return session["url"]

    async def change_plan(
        self, db: AsyncSession, *, user: User, plan_name: str, interval: str
    ) -> SubscriptionResponse | None:
        """Switch an existing subscription to a different plan/cadence, in place.

        Swaps the price on the live Stripe subscription (never a second one) and
        prorates: an upgrade is invoiced now, a downgrade books a credit toward
        the next invoice. Reuses the webhook sync path so the follow-up
        ``customer.subscription.updated`` is an idempotent no-op. A no-op change
        (already on this exact price) returns the current view without calling
        Stripe. Moving *to* the free plan isn't a price swap — cancel instead.
        """
        settings = get_settings()
        if not settings.billing_enabled:
            raise BillingDisabledError("Billing is not configured.")

        plan = await plan_repository.get_by_name(db, plan_name)
        if plan is None or not plan.is_active:
            raise PlanNotFoundError(f"Unknown plan '{plan_name}'.")
        if plan.price_monthly_cents == 0 and plan.price_annual_cents == 0:
            raise BillingRequestError(
                "Cancel your subscription to move to the free plan."
            )

        price_id = (
            plan.stripe_price_id_annual if interval == "annual" else plan.stripe_price_id_monthly
        )
        if not price_id:
            raise BillingRequestError(
                f"Plan '{plan_name}' is not available for {interval} billing yet."
            )

        subscription = await subscription_repository.get_for_user(db, user.id)
        if subscription is None or not subscription.stripe_subscription_id:
            raise BillingRequestError("No active subscription to change.")

        stripe_sub = await stripe.retrieve_subscription(
            subscription_id=subscription.stripe_subscription_id
        )
        current_item = _first_item(stripe_sub)
        if current_item is None or "id" not in current_item:
            raise BillingRequestError("This subscription has no plan to change.")

        # Already on the requested price/cadence — nothing to do, and Stripe would
        # reject a swap to the same price. Report the current state.
        if (current_item.get("price") or {}).get("id") == price_id:
            return await self.get_subscription_view(db, user.id)

        # Upgrades are billed now; downgrades book a credit for the next invoice.
        new_cost = _annualised_cents(plan, interval)
        old_cost = _annualised_cents(subscription.plan, subscription.billing_interval or "monthly")
        proration = "always_invoice" if new_cost >= old_cost else "create_prorations"

        obj = await stripe.update_subscription_price(
            subscription_id=subscription.stripe_subscription_id,
            item_id=current_item["id"],
            price_id=price_id,
            proration_behavior=proration,
        )
        await self._on_subscription_upsert(db, obj)
        await db.commit()
        return await self.get_subscription_view(db, user.id)

    async def cancel_subscription(
        self, db: AsyncSession, *, user: User, immediate: bool = False
    ) -> SubscriptionResponse | None:
        """Cancel the caller's subscription in Stripe and sync the result locally.

        Defaults to end-of-period (``cancel_at_period_end``) so the user keeps the
        access they paid for; ``immediate=True`` ends it now. The Stripe response
        is a full subscription object, so we reuse the same sync path as webhooks —
        the webhook that follows is then an idempotent no-op.
        """
        settings = get_settings()
        if not settings.billing_enabled:
            raise BillingDisabledError("Billing is not configured.")

        subscription = await subscription_repository.get_for_user(db, user.id)
        if subscription is None or not subscription.stripe_subscription_id:
            raise BillingRequestError("No active subscription to cancel.")

        if immediate:
            obj = await stripe.cancel_subscription_now(
                subscription_id=subscription.stripe_subscription_id
            )
        else:
            obj = await stripe.cancel_subscription_at_period_end(
                subscription_id=subscription.stripe_subscription_id
            )

        await self._on_subscription_upsert(db, obj)
        await db.commit()
        return await self.get_subscription_view(db, user.id)

    async def resume_subscription(
        self, db: AsyncSession, *, user: User
    ) -> SubscriptionResponse | None:
        """Undo a scheduled cancellation (clear ``cancel_at_period_end``).

        The mirror of :meth:`cancel_subscription`: only valid while the sub is
        still live but set to lapse at period end. Reuses the webhook sync path,
        so the follow-up ``customer.subscription.updated`` is an idempotent no-op.
        Raises :class:`BillingRequestError` when there's nothing to resume.
        """
        settings = get_settings()
        if not settings.billing_enabled:
            raise BillingDisabledError("Billing is not configured.")

        subscription = await subscription_repository.get_for_user(db, user.id)
        if subscription is None or not subscription.stripe_subscription_id:
            raise BillingRequestError("No subscription to resume.")
        if not subscription.cancel_at_period_end:
            raise BillingRequestError("This subscription isn't scheduled to cancel.")

        obj = await stripe.resume_subscription(
            subscription_id=subscription.stripe_subscription_id
        )
        await self._on_subscription_upsert(db, obj)
        await db.commit()
        return await self.get_subscription_view(db, user.id)

    async def _ensure_customer(self, db: AsyncSession, user: User, plan: Plan) -> str:
        """Return the user's Stripe customer id, creating (and persisting) if needed.

        Reuses the id from an existing subscription row; otherwise creates a Stripe
        Customer and records it on a placeholder INCOMPLETE row (which the webhook
        later promotes). INCOMPLETE is never counted by the quota gate.
        """
        subscription = await subscription_repository.get_for_user(db, user.id)
        if subscription and subscription.stripe_customer_id:
            return subscription.stripe_customer_id

        customer = await stripe.create_customer(email=user.email, user_id=str(user.id))
        customer_id = customer["id"]
        if subscription:
            await subscription_repository.update(
                db, subscription, {"stripe_customer_id": customer_id}
            )
        else:
            await subscription_repository.create(
                db,
                SubscriptionCreate(
                    user_id=user.id,
                    plan_id=plan.id,
                    status=SubscriptionStatus.INCOMPLETE,
                    stripe_customer_id=customer_id,
                ),
            )
        return customer_id

    # --- Webhook ----------------------------------------------------------

    async def handle_webhook(
        self, db: AsyncSession, *, payload: bytes, sig_header: str | None
    ) -> dict[str, str]:
        """Verify, dedupe, and process a Stripe webhook event.

        Raises :class:`stripe.SignatureVerificationError` on a bad signature (the
        endpoint maps that to 400). Duplicate events (same ``id``) are a no-op.
        """
        settings = get_settings()
        if not settings.billing_enabled or not settings.stripe_webhook_secret:
            raise BillingDisabledError("Billing webhooks are not configured.")

        event = stripe.construct_event(payload, sig_header, settings.stripe_webhook_secret)
        event_id = event["id"]
        event_type = event["type"]

        if await billing_event_repository.get_by_stripe_event_id(db, event_id):
            logger.info("Stripe event %s already processed; skipping.", event_id)
            return {"status": "duplicate"}

        obj = event.get("data", {}).get("object", {})
        record = await billing_event_repository.create(
            db,
            BillingEventCreate(
                stripe_event_id=event_id,
                event_type=event_type,
                payload=event,
                user_id=_extract_user_id(obj),
            ),
        )

        handler = {
            "checkout.session.completed": self._on_checkout_completed,
            "customer.subscription.created": self._on_subscription_upsert,
            "customer.subscription.updated": self._on_subscription_upsert,
            "customer.subscription.deleted": self._on_subscription_deleted,
            "invoice.payment_failed": self._on_payment_failed,
        }.get(event_type)

        if handler:
            await handler(db, obj)
        else:
            logger.debug("No handler for Stripe event type %s; logged only.", event_type)

        await billing_event_repository.update(
            db, record, {"processed_at": datetime.now(timezone.utc)}
        )
        await db.commit()
        return {"status": "processed", "type": event_type}

    async def _resolve_subscription(
        self, db: AsyncSession, obj: dict[str, Any]
    ) -> Subscription | None:
        """Find the local subscription row for a Stripe object, by any known key."""
        sub_id = obj.get("id") if obj.get("object") == "subscription" else obj.get("subscription")
        customer = obj.get("customer")
        user_id = _extract_user_id(obj)

        found: Subscription | None = None
        if sub_id:
            found = await subscription_repository.get_by_stripe_subscription_id(db, sub_id)
        if found is None and customer:
            found = await subscription_repository.get_by_stripe_customer_id(db, customer)
        if found is None and user_id:
            found = await subscription_repository.get_for_user(db, user_id)
        return found

    async def _on_checkout_completed(self, db: AsyncSession, session: dict[str, Any]) -> None:
        """Link the Stripe customer/subscription ids onto the user's row.

        Authoritative status/plan/period come from ``customer.subscription.*``;
        here we just repair the customer↔user link on the placeholder row.
        """
        subscription = await self._resolve_subscription(db, session)
        if subscription is None:
            return
        fields: dict[str, Any] = {}
        if session.get("customer"):
            fields["stripe_customer_id"] = session["customer"]
        if session.get("subscription"):
            fields["stripe_subscription_id"] = session["subscription"]
        if fields:
            await subscription_repository.update(db, subscription, fields)

    async def _on_subscription_upsert(self, db: AsyncSession, sub_obj: dict[str, Any]) -> None:
        """Create or update the local subscription from a Stripe subscription object."""
        price_id = _first_price_id(sub_obj)
        plan = await plan_repository.get_by_stripe_price_id(db, price_id) if price_id else None

        period_start, period_end = _first_period(sub_obj)
        fields: dict[str, Any] = {
            "status": _map_status(sub_obj.get("status")),
            "stripe_subscription_id": sub_obj.get("id"),
            "current_period_start": _ts(period_start),
            "current_period_end": _ts(period_end),
            "cancel_at_period_end": bool(sub_obj.get("cancel_at_period_end")),
            "cancelled_at": _ts(sub_obj.get("canceled_at")),
        }
        if sub_obj.get("customer"):
            fields["stripe_customer_id"] = sub_obj["customer"]
        if plan is not None:
            fields["plan_id"] = plan.id
        # Only overwrite the cadence when we could actually read it, so a later
        # event that omits the price detail doesn't wipe a known value.
        interval = _first_interval(sub_obj)
        if interval is not None:
            fields["billing_interval"] = interval

        existing = await self._resolve_subscription(db, sub_obj)
        if existing is not None:
            await subscription_repository.update(db, existing, fields)
            return

        user_id = _extract_user_id(sub_obj)
        if user_id is None or plan is None:
            logger.warning(
                "Dropping subscription upsert: unresolved user_id=%s or plan (price=%s).",
                user_id,
                price_id,
            )
            return
        # ``fields`` already carries plan_id (plan is not None on this path).
        await subscription_repository.create(
            db,
            SubscriptionCreate(user_id=user_id, **fields),
        )

    async def _on_subscription_deleted(self, db: AsyncSession, sub_obj: dict[str, Any]) -> None:
        subscription = await self._resolve_subscription(db, sub_obj)
        if subscription is None:
            return
        await subscription_repository.update(
            db,
            subscription,
            {
                "status": SubscriptionStatus.CANCELLED,
                "cancelled_at": _ts(sub_obj.get("canceled_at")) or datetime.now(timezone.utc),
            },
        )

    async def _on_payment_failed(self, db: AsyncSession, invoice: dict[str, Any]) -> None:
        subscription = await self._resolve_subscription(db, invoice)
        if subscription is None:
            return
        await subscription_repository.update(
            db, subscription, {"status": SubscriptionStatus.PAST_DUE}
        )


billing_service = BillingService()
