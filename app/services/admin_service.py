import uuid
from typing import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.models.user import User
from app.repositories.project_repository import project_repository
from app.repositories.user_repository import user_repository
from app.schemas.admin import AdminProjectResponse


class AdminService:
    """Read-only aggregation for the internal admin utility (SOW 1.G).

    No writes: admins get visibility into all users/projects, their status, and
    output availability, but never mutate another user's resources.
    """

    def __init__(self) -> None:
        self.user_repository = user_repository
        self.project_repository = project_repository

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
