import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, new_uuid

if TYPE_CHECKING:
    from app.models.subscription import Subscription


class PlanName(str, enum.Enum):
    """Canonical plan slugs (mirrors ``plans.name``, kept lowercase to match the
    seeded rows). Not a DB ``Enum`` column — ``plans.name`` stays a plain String so
    plans remain data-driven — but the API takes/returns these values so the
    frontend has a fixed, self-documenting set instead of a free-form string.

    ``starter`` is intentionally omitted: it is a retired legacy tier, deactivated
    in the reseed migration and no longer offered for checkout.
    """

    FREE = "free"
    CREATOR = "creator"
    PRO = "pro"
    STUDIO = "studio"


class Plan(Base, TimestampMixin):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(
        String(50), unique=True, nullable=False
    )  # free | creator | pro | studio
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    price_monthly_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Annual price for the whole year (not per-month). 0 for the free plan.
    price_annual_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Legacy per-month *project* cap. No longer enforced — the metered unit is
    # now audio files per month (see ``max_files_per_month``); projects are
    # unlimited. Kept for backwards compatibility / display. None = unlimited.
    max_projects_per_month: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # None = unlimited
    # The metered unit: how many input audio files a user may upload per billing
    # period across all their projects. None = unlimited. Enforced in
    # ``ProjectService._enforce_file_quota``.
    max_files_per_month: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # None = unlimited
    max_file_size_mb: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    # One Stripe Product has one Price per billing interval; store both so checkout
    # can pick by the interval the user chose. Null until wired to a Stripe account.
    stripe_price_id_monthly: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_price_id_annual: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Display bullets + non-enforced feature flags (stem quality, download format,
    # priority, seats, api_access…). Advertised on the pricing page; only the
    # columns above (project count, file size) are hard-enforced in the MVP.
    features: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    subscriptions: Mapped[list["Subscription"]] = relationship(
        "Subscription", back_populates="plan"
    )
