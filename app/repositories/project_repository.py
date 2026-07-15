import uuid
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.project import Project
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


project_repository = ProjectRepository(Project)
