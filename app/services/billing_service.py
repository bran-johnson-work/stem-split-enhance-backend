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

        fields: dict[str, Any] = {
            "status": _map_status(sub_obj.get("status")),
            "stripe_subscription_id": sub_obj.get("id"),
            "current_period_start": _ts(sub_obj.get("current_period_start")),
            "current_period_end": _ts(sub_obj.get("current_period_end")),
            "cancel_at_period_end": bool(sub_obj.get("cancel_at_period_end")),
            "cancelled_at": _ts(sub_obj.get("canceled_at")),
        }
        if sub_obj.get("customer"):
            fields["stripe_customer_id"] = sub_obj["customer"]
        if plan is not None:
            fields["plan_id"] = plan.id

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
