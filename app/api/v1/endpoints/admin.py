import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_role
from app.core.database import get_db
from app.models.user import UserRole
from app.schemas.admin import (
    AdminOverviewResponse,
    AdminProjectResponse,
    AdminRevenueResponse,
    AdminTrendResponse,
)
from app.schemas.user import UserResponse
from app.services.admin_service import admin_service

# Router-level guard: every route here requires ADMIN. Applying it on the router
# (rather than per-endpoint) guarantees no admin route can be added unguarded.
router = APIRouter(dependencies=[Depends(require_role(UserRole.ADMIN))])


@router.get("/users", response_model=list[UserResponse])
async def list_all_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """List all users (admin read-only)."""
    return await admin_service.list_users(db, skip=skip, limit=limit)


@router.get("/stats/overview", response_model=AdminOverviewResponse)
async def read_overview_stats(
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Platform-wide counts: users, projects, jobs, subscriptions (admin only)."""
    return await admin_service.overview(db)


@router.get("/stats/revenue", response_model=AdminRevenueResponse)
async def read_revenue_stats(
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Recurring revenue (MRR/ARR + per-plan breakdown) from active subscriptions."""
    return await admin_service.revenue(db)


@router.get("/stats/trends", response_model=AdminTrendResponse)
async def read_trend_stats(
    days: int = Query(30, ge=1, le=365),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Daily signups and project creations over the last ``days`` (admin only)."""
    return await admin_service.trends(db, days=days)


@router.get("/projects", response_model=list[AdminProjectResponse])
async def list_all_projects(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """List all projects with status and output availability (admin read-only)."""
    return await admin_service.list_projects(db, skip=skip, limit=limit)


@router.get("/projects/{project_id}", response_model=AdminProjectResponse)
async def read_any_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Get any project's status and output availability (admin read-only)."""
    project = await admin_service.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project
