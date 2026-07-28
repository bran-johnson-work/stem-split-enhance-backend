import uuid
from datetime import date, datetime

from pydantic import BaseModel, EmailStr

from app.models.project import ProjectStatus


class AdminProjectResponse(BaseModel):
    """Admin-facing view of a project: status plus output availability.

    Built explicitly by ``admin_service`` (not via ``from_attributes``) because
    ``owner_email`` and the output-availability fields are derived, not columns.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    owner_email: EmailStr
    name: str
    status: ProjectStatus
    output_file_count: int
    has_outputs: bool
    created_at: datetime
    updated_at: datetime


# --- Statistics / revenue / trends -------------------------------------------
# All built explicitly by ``admin_service`` from repository aggregates (not
# ``from_attributes``). ``by_status`` maps use the enum *values* (lowercase
# strings) as keys, with every status present (zero-filled) so the client can
# render a fixed set of buckets.


class UserStats(BaseModel):
    total: int
    active: int
    verified: int
    admins: int


class ProjectStats(BaseModel):
    total: int
    by_status: dict[str, int]


class JobStats(BaseModel):
    total: int
    by_status: dict[str, int]


class SubscriptionStats(BaseModel):
    total: int
    active: int
    by_status: dict[str, int]


class AdminOverviewResponse(BaseModel):
    """Platform-wide counts for the admin overview dashboard."""

    users: UserStats
    projects: ProjectStats
    jobs: JobStats
    subscriptions: SubscriptionStats


class PlanRevenue(BaseModel):
    """Revenue contribution of a single plan (active subscriptions only)."""

    plan_name: str
    display_name: str
    active_subscriptions: int
    mrr_cents: int


class AdminRevenueResponse(BaseModel):
    """Recurring revenue derived from ACTIVE subscriptions joined to their plan.

    Monetary values are in integer cents (minor units); the frontend converts to
    major units for display. Computed from ``subscriptions``/``plans`` because
    ``billing_events`` carries no amount column.
    """

    mrr_cents: int
    arr_cents: int
    active_paid_subscriptions: int
    currency: str
    by_plan: list[PlanRevenue]


class AdminTrendPoint(BaseModel):
    """One UTC day of activity: new signups and new projects."""

    date: date
    signups: int
    projects_created: int


class AdminTrendResponse(BaseModel):
    """Dense daily time series (one point per day in the window)."""

    points: list[AdminTrendPoint]
