import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.processing_job import JobStatus
from app.models.project import Project, ProjectStatus
from app.models.subscription import SubscriptionStatus
from app.models.user import User
from app.repositories.processing_job_repository import processing_job_repository
from app.repositories.project_repository import project_repository
from app.repositories.subscription_repository import subscription_repository
from app.repositories.user_repository import user_repository
from app.schemas.admin import (
    AdminOverviewResponse,
    AdminProjectResponse,
    AdminRevenueResponse,
    AdminTrendPoint,
    AdminTrendResponse,
    JobStats,
    PlanRevenue,
    ProjectStats,
    SubscriptionStats,
    UserStats,
)

# The reporting currency. Plans are priced in USD cents; there is no
# multi-currency support, so this is a constant rather than a per-plan field.
_CURRENCY = "usd"


class AdminService:
    """Read-only aggregation for the internal admin utility (SOW 1.G).

    No writes: admins get visibility into all users/projects, their status,
    output availability, and platform-wide statistics, but never mutate another
    user's resources.
    """

    def __init__(self) -> None:
        self.user_repository = user_repository
        self.project_repository = project_repository
        self.job_repository = processing_job_repository
        self.subscription_repository = subscription_repository

    async def list_users(
        self, db: AsyncSession, skip: int = 0, limit: int = 100
    ) -> Sequence[User]:
        return await self.user_repository.get_multi(db, skip=skip, limit=limit)

    async def list_projects(
        self, db: AsyncSession, skip: int = 0, limit: int = 100
    ) -> list[AdminProjectResponse]:
        projects = await self.project_repository.list_all(db, skip=skip, limit=limit)
        return [self._to_admin_project(p) for p in projects]

    async def get_project(
        self, db: AsyncSession, project_id: uuid.UUID
    ) -> AdminProjectResponse | None:
        project = await self.project_repository.get_any(db, project_id)
        return self._to_admin_project(project) if project else None

    # -- Statistics -------------------------------------------------------

    async def overview(self, db: AsyncSession) -> AdminOverviewResponse:
        """Platform-wide counts for the admin overview dashboard."""
        projects_by_status = await self.project_repository.count_by_status(db)
        jobs_by_status = await self.job_repository.count_by_status(db)
        subs_by_status = await self.subscription_repository.count_by_status(db)

        return AdminOverviewResponse(
            users=UserStats(
                total=await self.user_repository.count_all(db),
                active=await self.user_repository.count_active(db),
                verified=await self.user_repository.count_verified(db),
                admins=await self.user_repository.count_admins(db),
            ),
            projects=ProjectStats(
                total=sum(projects_by_status.values()),
                by_status=self._fill_status(projects_by_status, ProjectStatus),
            ),
            jobs=JobStats(
                total=sum(jobs_by_status.values()),
                by_status=self._fill_status(jobs_by_status, JobStatus),
            ),
            subscriptions=SubscriptionStats(
                total=sum(subs_by_status.values()),
                active=subs_by_status.get(SubscriptionStatus.ACTIVE, 0),
                by_status=self._fill_status(subs_by_status, SubscriptionStatus),
            ),
        )

    async def revenue(self, db: AsyncSession) -> AdminRevenueResponse:
        """Recurring revenue from ACTIVE subscriptions joined to their plan.

        MRR normalises annual plans to a monthly figure
        (``price_annual_cents // 12``). Only paid plans (monthly contribution > 0)
        count as "paid subscriptions"; an active free-plan row contributes 0.
        """
        subscriptions = await self.subscription_repository.list_active_with_plan(db)

        # Aggregate per plan so the breakdown is stable regardless of row order.
        per_plan: dict[str, PlanRevenue] = {}
        mrr_cents = 0
        active_paid = 0
        for sub in subscriptions:
            plan = sub.plan
            if plan is None:  # defensive: an active sub should always have a plan
                continue
            monthly = self._monthly_cents(
                plan.price_monthly_cents, plan.price_annual_cents, sub.billing_interval
            )
            mrr_cents += monthly
            if monthly > 0:
                active_paid += 1
            entry = per_plan.get(plan.name)
            if entry is None:
                per_plan[plan.name] = PlanRevenue(
                    plan_name=plan.name,
                    display_name=plan.display_name,
                    active_subscriptions=1,
                    mrr_cents=monthly,
                )
            else:
                entry.active_subscriptions += 1
                entry.mrr_cents += monthly

        by_plan = sorted(per_plan.values(), key=lambda p: p.mrr_cents, reverse=True)
        return AdminRevenueResponse(
            mrr_cents=mrr_cents,
            arr_cents=mrr_cents * 12,
            active_paid_subscriptions=active_paid,
            currency=_CURRENCY,
            by_plan=by_plan,
        )

    async def trends(self, db: AsyncSession, days: int = 30) -> AdminTrendResponse:
        """Dense daily signups + project-creation series over the last ``days``.

        The window starts at midnight UTC ``days - 1`` days ago, so the series has
        exactly ``days`` points ending today. Days with no activity are zero-filled
        (the repositories only return non-empty buckets).
        """
        today = datetime.now(timezone.utc).date()
        start_date = today - timedelta(days=days - 1)
        since = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc)

        signups = self._by_date(await self.user_repository.signups_by_day(db, since))
        projects = self._by_date(await self.project_repository.created_by_day(db, since))

        points = [
            AdminTrendPoint(
                date=day,
                signups=signups.get(day, 0),
                projects_created=projects.get(day, 0),
            )
            for day in (start_date + timedelta(days=i) for i in range(days))
        ]
        return AdminTrendResponse(points=points)

    # -- Helpers ----------------------------------------------------------

    @staticmethod
    def _monthly_cents(monthly: int, annual: int, billing_interval: str | None) -> int:
        """A subscription's normalised monthly revenue in cents.

        Annual plans are billed once a year, so their MRR contribution is the
        annual price spread across 12 months.
        """
        if billing_interval == "annual":
            return annual // 12
        return monthly

    @staticmethod
    def _fill_status(counts: dict, enum_cls) -> dict[str, int]:
        """Zero-fill a status->count map so every enum value is present, keyed by
        the enum *value* (the lowercase string the API and frontend share)."""
        return {member.value: counts.get(member, 0) for member in enum_cls}

    @staticmethod
    def _by_date(rows: list[tuple[datetime, int]]) -> dict[date, int]:
        """Index ``(day_datetime, count)`` rows by calendar date for O(1) lookup."""
        return {dt.date(): count for dt, count in rows}

    @staticmethod
    def _to_admin_project(project: Project) -> AdminProjectResponse:
        outputs = project.output_files or []
        return AdminProjectResponse(
            id=project.id,
            user_id=project.user_id,
            owner_email=project.user.email,
            name=project.name,
            status=project.status,
            output_file_count=len(outputs),
            has_outputs=len(outputs) > 0,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )


admin_service = AdminService()
