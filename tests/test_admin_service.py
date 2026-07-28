"""Unit tests for AdminService aggregation logic (revenue math, trend
densification, status zero-filling). Repositories are mocked — no DB."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.processing_job import JobStatus
from app.models.project import ProjectStatus
from app.models.subscription import SubscriptionStatus
from app.services.admin_service import admin_service


def _sub(name, display, monthly, annual, interval):
    """A minimal stand-in for an ACTIVE Subscription with its plan loaded."""
    plan = SimpleNamespace(
        name=name,
        display_name=display,
        price_monthly_cents=monthly,
        price_annual_cents=annual,
    )
    return SimpleNamespace(plan=plan, billing_interval=interval)


# --- revenue ---------------------------------------------------------------


async def test_revenue_sums_monthly_and_annual(monkeypatch, fake_db):
    subs = [
        _sub("pro", "Pro", 1500, 15000, "monthly"),        # +1500
        _sub("pro", "Pro", 1500, 15000, "annual"),         # +1250 (15000 // 12)
        _sub("free", "Free", 0, 0, "monthly"),             # +0, not "paid"
    ]
    monkeypatch.setattr(
        admin_service.subscription_repository,
        "list_active_with_plan",
        AsyncMock(return_value=subs),
    )

    revenue = await admin_service.revenue(fake_db)

    assert revenue.mrr_cents == 2750
    assert revenue.arr_cents == 2750 * 12
    assert revenue.active_paid_subscriptions == 2  # free plan excluded
    assert revenue.currency == "usd"
    # Aggregated per plan, sorted by MRR desc.
    assert [p.plan_name for p in revenue.by_plan] == ["pro", "free"]
    pro = revenue.by_plan[0]
    assert pro.active_subscriptions == 2
    assert pro.mrr_cents == 2750


async def test_revenue_is_zero_with_no_active_subscriptions(monkeypatch, fake_db):
    monkeypatch.setattr(
        admin_service.subscription_repository,
        "list_active_with_plan",
        AsyncMock(return_value=[]),
    )
    revenue = await admin_service.revenue(fake_db)
    assert revenue.mrr_cents == 0
    assert revenue.arr_cents == 0
    assert revenue.active_paid_subscriptions == 0
    assert revenue.by_plan == []


# --- overview --------------------------------------------------------------


async def test_overview_zero_fills_every_status(monkeypatch, fake_db):
    monkeypatch.setattr(
        admin_service.user_repository, "count_all", AsyncMock(return_value=4)
    )
    monkeypatch.setattr(
        admin_service.user_repository, "count_active", AsyncMock(return_value=3)
    )
    monkeypatch.setattr(
        admin_service.user_repository, "count_verified", AsyncMock(return_value=2)
    )
    monkeypatch.setattr(
        admin_service.user_repository, "count_admins", AsyncMock(return_value=1)
    )
    monkeypatch.setattr(
        admin_service.project_repository,
        "count_by_status",
        AsyncMock(return_value={ProjectStatus.COMPLETED: 2, ProjectStatus.FAILED: 1}),
    )
    monkeypatch.setattr(
        admin_service.job_repository,
        "count_by_status",
        AsyncMock(return_value={JobStatus.COMPLETED: 2}),
    )
    monkeypatch.setattr(
        admin_service.subscription_repository,
        "count_by_status",
        AsyncMock(return_value={SubscriptionStatus.ACTIVE: 5}),
    )

    overview = await admin_service.overview(fake_db)

    assert overview.users.total == 4
    assert overview.users.admins == 1
    # Totals are summed from the grouped counts.
    assert overview.projects.total == 3
    assert overview.subscriptions.active == 5
    # Every enum value is present, absent ones zero-filled and keyed by value.
    assert set(overview.projects.by_status) == {s.value for s in ProjectStatus}
    assert overview.projects.by_status["completed"] == 2
    assert overview.projects.by_status["pending"] == 0
    assert set(overview.jobs.by_status) == {s.value for s in JobStatus}
    assert set(overview.subscriptions.by_status) == {s.value for s in SubscriptionStatus}


# --- trends ----------------------------------------------------------------


async def test_trends_densifies_to_one_point_per_day(monkeypatch, fake_db):
    today = datetime.now(timezone.utc).date()
    today_midnight = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)

    monkeypatch.setattr(
        admin_service.user_repository,
        "signups_by_day",
        AsyncMock(return_value=[(today_midnight, 5)]),
    )
    monkeypatch.setattr(
        admin_service.project_repository,
        "created_by_day",
        AsyncMock(return_value=[(today_midnight, 2)]),
    )

    trends = await admin_service.trends(fake_db, days=3)

    # Exactly `days` points, oldest first, ending today.
    assert len(trends.points) == 3
    assert trends.points[-1].date == today
    assert trends.points[0].date == today - timedelta(days=2)
    # Today's non-empty bucket lands; empty days are zero-filled.
    assert trends.points[-1].signups == 5
    assert trends.points[-1].projects_created == 2
    assert trends.points[0].signups == 0
    assert trends.points[0].projects_created == 0
