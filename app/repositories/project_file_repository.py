import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project_file import ProjectFile
from app.repositories.base_repository import BaseRepository
from app.schemas.project import ProjectCreate, ProjectUpdate


class ProjectFileRepository(BaseRepository[ProjectFile, ProjectCreate, ProjectUpdate]):
    """Data access for the original uploaded audio file of a project."""

    async def create_for_project(
        self,
        db: AsyncSession,
        *,
        project_id: uuid.UUID,
        original_filename: str,
        storage_key: str,
        file_size_bytes: int,
        content_type: str,
    ) -> ProjectFile:
        db_obj = ProjectFile(
            project_id=project_id,
            original_filename=original_filename,
            storage_key=storage_key,
            file_size_bytes=file_size_bytes,
            content_type=content_type,
        )
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj


project_file_repository = ProjectFileRepository(ProjectFile)
