import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.plan import PlanName
from app.models.subscription import SubscriptionStatus


class BillingInterval(str, enum.Enum):
    """Billing cadence a plan can be purchased on."""

    MONTHLY = "monthly"
    ANNUAL = "annual"


# --- Public API DTOs ------------------------------------------------------


class PlanResponse(BaseModel):
    """A billing plan as shown on the pricing page."""

    id: uuid.UUID
    name: str
    display_name: str
    price_monthly_cents: int
    price_annual_cents: int
    # Legacy project cap (no longer enforced; kept for display). None = unlimited.
    max_projects_per_month: int | None
    # Audio files a user may upload per billing period. None = unlimited.
    max_files_per_month: int | None = None
    max_file_size_mb: int
    features: dict | None
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class CheckoutRequest(BaseModel):
    """Client asks to subscribe to ``plan_name`` on a billing ``interval``.

    The client never sends a Stripe price id — the server resolves it from the
    plan row so a user cannot subscribe to a price they weren't offered.
    """

    plan_name: PlanName
    interval: BillingInterval = BillingInterval.MONTHLY


class CheckoutResponse(BaseModel):
    checkout_url: str


class PortalResponse(BaseModel):
    portal_url: str


class SubscriptionResponse(BaseModel):
    """The caller's effective subscription state.

    Never ``null`` for a normal account: a user with no subscription row is
    reported as being on the free plan by default (``has_subscription=False``,
    ``status=None``), so the frontend always has a plan to render.
    """

    # False => no paid/managed subscription; the user is on the free plan by
    # default. True => a real Stripe-backed subscription row exists.
    has_subscription: bool
    status: SubscriptionStatus | None
    plan: PlanResponse
    current_period_start: datetime | None
    current_period_end: datetime | None
    # True => active but will not renew; ends at ``current_period_end``.
    cancel_at_period_end: bool
    cancelled_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class CancelRequest(BaseModel):
    """Cancel the caller's subscription. Defaults to end-of-period (keep access)."""

    immediate: bool = False


class InvoiceResponse(BaseModel):
    """One billing receipt, sourced live from Stripe.

    Amounts are **dollars** (Stripe reports cents; the service converts) so the
    client formats them the same as every other price. The two URLs are Stripe's
    own: ``hosted_invoice_url`` is a full receipt page, ``invoice_pdf`` a direct
    PDF download — both short-lived, so they are fetched on demand, never stored.
    """

    id: str
    # When the invoice was finalised, ISO-8601.
    created_at: datetime
    amount: float
    currency: str
    # Normalised to the set the UI renders: paid | open | failed.
    status: str
    description: str | None
    hosted_invoice_url: str | None
    invoice_pdf: str | None


# --- Internal persistence schemas (feed BaseRepository create/update) ------


class SubscriptionCreate(BaseModel):
    user_id: uuid.UUID
    plan_id: uuid.UUID
    status: SubscriptionStatus = SubscriptionStatus.INCOMPLETE
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    cancelled_at: datetime | None = None


class SubscriptionUpdate(BaseModel):
    plan_id: uuid.UUID | None = None
    status: SubscriptionStatus | None = None
    stripe_customer_id: str | None = None
    stripe_subscription_id: str | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool | None = None
    cancelled_at: datetime | None = None


class BillingEventCreate(BaseModel):
    stripe_event_id: str
    event_type: str
    payload: dict
    user_id: uuid.UUID | None = None
    processed_at: datetime | None = None
