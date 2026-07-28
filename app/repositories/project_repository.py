import uuid
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.project import Project, ProjectStatus
from app.repositories.base_repository import BaseRepository
from app.schemas.project import ProjectCreate, ProjectUpdate


class ProjectRepository(BaseRepository[Project, ProjectCreate, ProjectUpdate]):
    """Project-specific data access."""

    async def get_for_user(
        self, db: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> Project | None:
        """Get a single project owned by ``user_id`` with its input files eagerly loaded."""
        result = await db.execute(
            select(Project)
            .where(Project.id == project_id, Project.user_id == user_id)
            .options(selectinload(Project.input_files))
        )
        return result.scalars().first()

    async def list_for_user(
        self, db: AsyncSession, user_id: uuid.UUID, skip: int = 0, limit: int = 100
    ) -> Sequence[Project]:
        """List a user's projects, newest first, with input files eagerly loaded."""
        result = await db.execute(
            select(Project)
            .where(Project.user_id == user_id)
            .options(selectinload(Project.input_files))
            .order_by(Project.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()

    async def list_all(
        self, db: AsyncSession, skip: int = 0, limit: int = 100
    ) -> Sequence[Project]:
        """List every project (admin), newest first, with owner and outputs eager-loaded."""
        result = await db.execute(
            select(Project)
            .options(selectinload(Project.user), selectinload(Project.output_files))
            .order_by(Project.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()

    async def get_any(self, db: AsyncSession, project_id: uuid.UUID) -> Project | None:
        """Get any project by id (admin), with owner and outputs eager-loaded."""
        result = await db.execute(
            select(Project)
            .where(Project.id == project_id)
            .options(selectinload(Project.user), selectinload(Project.output_files))
        )
        return result.scalars().first()

    async def count_for_user_since(
        self, db: AsyncSession, user_id: uuid.UUID, since: datetime
    ) -> int:
        """Count a user's projects created at/after ``since`` (for quota enforcement).

        Backed by the composite ``(user_id, created_at)`` index on ``projects``.
        """
        result = await db.execute(
            select(func.count())
            .select_from(Project)
            .where(Project.user_id == user_id, Project.created_at >= since)
        )
        return int(result.scalar_one() or 0)

    async def count_all(self, db: AsyncSession) -> int:
        """Total number of projects (all users)."""
        result = await db.execute(select(func.count()).select_from(Project))
        return int(result.scalar_one() or 0)

    async def count_by_status(self, db: AsyncSession) -> dict[ProjectStatus, int]:
        """Project counts grouped by status. Absent statuses are simply missing —
        the service fills them with 0 so every enum value is represented."""
        result = await db.execute(
            select(Project.status, func.count()).group_by(Project.status)
        )
        return {status: int(count) for status, count in result.all()}

    async def created_by_day(
        self, db: AsyncSession, since: datetime
    ) -> list[tuple[datetime, int]]:
        """Count projects created per UTC day at/after ``since``, oldest first.

        Same shape as ``UserRepository.signups_by_day`` — only non-empty days are
        returned; the service densifies the series.
        """
        day = func.date_trunc("day", Project.created_at)
        result = await db.execute(
            select(day, func.count())
            .where(Project.created_at >= since)
            .group_by(day)
            .order_by(day)
        )
        return [(row[0], int(row[1])) for row in result.all()]


project_repository = ProjectRepository(Project)
