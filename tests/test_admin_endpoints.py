import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.api.dependencies import get_current_user
from app.api.v1.endpoints import admin as admin_module
from app.main import app
from app.models.output_file import OutputFile
from app.models.project import Project, ProjectStatus
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
from app.services.admin_service import AdminService


@pytest.fixture
def admin_client(client, admin_user):
    """Client authenticated as an ADMIN."""
    app.dependency_overrides[get_current_user] = lambda: admin_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def user_client(client, sample_user):
    """Client authenticated as a plain USER."""
    app.dependency_overrides[get_current_user] = lambda: sample_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


def _admin_project(user_id: uuid.UUID, has_outputs: bool = True) -> AdminProjectResponse:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return AdminProjectResponse(
        id=uuid.uuid4(),
        user_id=user_id,
        owner_email="owner@example.com",
        name="My song",
        status=ProjectStatus.COMPLETED,
        output_file_count=4 if has_outputs else 0,
        has_outputs=has_outputs,
        created_at=now,
        updated_at=now,
    )


# --- guard behaviour ------------------------------------------------------


async def test_user_cannot_list_users(user_client):
    resp = await user_client.get("/api/v1/admin/users")
    assert resp.status_code == 403


async def test_user_cannot_list_projects(user_client):
    resp = await user_client.get("/api/v1/admin/projects")
    assert resp.status_code == 403


async def test_unauthenticated_admin_route_is_401(client):
    # No get_current_user override → real dependency rejects the missing token.
    resp = await client.get("/api/v1/admin/users")
    assert resp.status_code == 401


# --- admin can read -------------------------------------------------------


async def test_admin_lists_users(admin_client, monkeypatch, admin_user, sample_user):
    monkeypatch.setattr(
        admin_module.admin_service,
        "list_users",
        AsyncMock(return_value=[admin_user, sample_user]),
    )
    resp = await admin_client.get("/api/v1/admin/users")
    assert resp.status_code == 200
    body = resp.json()
    assert {u["email"] for u in body} == {admin_user.email, sample_user.email}
    # Role is exposed to the admin view.
    assert {u["role"] for u in body} == {"ADMIN", "USER"}


async def test_admin_lists_projects_with_output_availability(
    admin_client, monkeypatch, sample_user
):
    project = _admin_project(sample_user.id, has_outputs=True)
    monkeypatch.setattr(
        admin_module.admin_service, "list_projects", AsyncMock(return_value=[project])
    )
    resp = await admin_client.get("/api/v1/admin/projects")
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["status"] == "completed"
    assert row["has_outputs"] is True
    assert row["output_file_count"] == 4
    assert row["owner_email"] == "owner@example.com"


async def test_admin_reads_single_project(admin_client, monkeypatch, sample_user):
    project = _admin_project(sample_user.id)
    monkeypatch.setattr(
        admin_module.admin_service, "get_project", AsyncMock(return_value=project)
    )
    resp = await admin_client.get(f"/api/v1/admin/projects/{project.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(project.id)


async def test_admin_project_not_found_returns_404(admin_client, monkeypatch):
    monkeypatch.setattr(
        admin_module.admin_service, "get_project", AsyncMock(return_value=None)
    )
    resp = await admin_client.get(f"/api/v1/admin/projects/{uuid.uuid4()}")
    assert resp.status_code == 404


# --- stats endpoints ------------------------------------------------------


async def test_admin_reads_overview(admin_client, monkeypatch):
    overview = AdminOverviewResponse(
        users=UserStats(total=10, active=8, verified=6, admins=1),
        projects=ProjectStats(total=5, by_status={"completed": 5}),
        jobs=JobStats(total=5, by_status={"completed": 5}),
        subscriptions=SubscriptionStats(total=3, active=2, by_status={"active": 2}),
    )
    monkeypatch.setattr(
        admin_module.admin_service, "overview", AsyncMock(return_value=overview)
    )
    resp = await admin_client.get("/api/v1/admin/stats/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["users"]["total"] == 10
    assert body["subscriptions"]["active"] == 2


async def test_admin_reads_revenue(admin_client, monkeypatch):
    revenue = AdminRevenueResponse(
        mrr_cents=2750,
        arr_cents=33000,
        active_paid_subscriptions=2,
        currency="usd",
        by_plan=[PlanRevenue(plan_name="pro", display_name="Pro",
                             active_subscriptions=2, mrr_cents=2750)],
    )
    monkeypatch.setattr(
        admin_module.admin_service, "revenue", AsyncMock(return_value=revenue)
    )
    resp = await admin_client.get("/api/v1/admin/stats/revenue")
    assert resp.status_code == 200
    body = resp.json()
    assert body["mrr_cents"] == 2750
    assert body["by_plan"][0]["plan_name"] == "pro"


async def test_admin_reads_trends_with_days_param(admin_client, monkeypatch):
    trends = AdminTrendResponse(
        points=[AdminTrendPoint(date="2026-07-28", signups=3, projects_created=1)]
    )
    spy = AsyncMock(return_value=trends)
    monkeypatch.setattr(admin_module.admin_service, "trends", spy)
    resp = await admin_client.get("/api/v1/admin/stats/trends?days=7")
    assert resp.status_code == 200
    assert resp.json()["points"][0]["signups"] == 3
    # The query param is forwarded to the service.
    assert spy.await_args.kwargs["days"] == 7


async def test_trends_rejects_out_of_range_days(admin_client):
    resp = await admin_client.get("/api/v1/admin/stats/trends?days=0")
    assert resp.status_code == 422


async def test_user_cannot_read_stats(user_client):
    # The router-level ADMIN guard covers the new stats routes too.
    resp = await user_client.get("/api/v1/admin/stats/overview")
    assert resp.status_code == 403


# --- service output-availability derivation -------------------------------


def _orm_project(sample_user, outputs) -> Project:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    project = Project(
        id=uuid.uuid4(),
        user_id=sample_user.id,
        name="My song",
        status=ProjectStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )
    project.user = sample_user
    project.output_files = outputs
    return project


def test_service_reports_outputs_available(sample_user):
    outputs = [
        OutputFile(id=uuid.uuid4(), project_id=uuid.uuid4(), job_id=uuid.uuid4(),
                   stem_name="vocals", storage_key="k", file_size_bytes=1, is_enhanced=True)
    ]
    result = AdminService._to_admin_project(_orm_project(sample_user, outputs))
    assert result.has_outputs is True
    assert result.output_file_count == 1
    assert result.owner_email == sample_user.email


def test_service_reports_no_outputs(sample_user):
    result = AdminService._to_admin_project(_orm_project(sample_user, []))
    assert result.has_outputs is False
    assert result.output_file_count == 0
