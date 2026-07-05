import uuid
from typing import Sequence

from sqlalchemy import select
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
        """Get a single project owned by ``user_id`` with its input file eagerly loaded."""
        result = await db.execute(
            select(Project)
            .where(Project.id == project_id, Project.user_id == user_id)
            .options(selectinload(Project.input_file))
        )
        return result.scalars().first()

    async def list_for_user(
        self, db: AsyncSession, user_id: uuid.UUID, skip: int = 0, limit: int = 100
    ) -> Sequence[Project]:
        """List a user's projects, newest first, with input files eagerly loaded."""
        result = await db.execute(
            select(Project)
            .where(Project.user_id == user_id)
            .options(selectinload(Project.input_file))
            .order_by(Project.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        return result.scalars().all()


project_repository = ProjectRepository(Project)
