import uuid
from datetime import datetime
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project
from app.models.project_file import ProjectFile
from app.repositories.base_repository import BaseRepository
from app.schemas.project import ProjectCreate, ProjectUpdate


class ProjectFileRepository(BaseRepository[ProjectFile, ProjectCreate, ProjectUpdate]):
    """Data access for the audio files uploaded to a project (one-to-many)."""

    async def count_for_user_since(
        self, db: AsyncSession, user_id: uuid.UUID, since: datetime
    ) -> int:
        """Count a user's uploaded input files created at/after ``since``.

        Joins ``project_files`` to ``projects`` to scope by owner; this is the
        monthly-quota counter (the metered unit is audio files, not projects).
        Backed by the ``project_files.project_id`` index and the projects owner
        index. A file's ``created_at`` is set when it is recorded at
        upload-complete, so this counts genuinely-uploaded files only.
        """
        result = await db.execute(
            select(func.count())
            .select_from(ProjectFile)
            .join(Project, ProjectFile.project_id == Project.id)
            .where(Project.user_id == user_id, ProjectFile.created_at >= since)
        )
        return int(result.scalar_one() or 0)

    async def get_for_project(
        self, db: AsyncSession, *, project_id: uuid.UUID, file_id: uuid.UUID
    ) -> ProjectFile | None:
        """Get a single file by id, scoped to its owning project."""
        result = await db.execute(
            select(ProjectFile).where(
                ProjectFile.id == file_id,
                ProjectFile.project_id == project_id,
            )
        )
        return result.scalars().first()

    async def list_for_project(
        self, db: AsyncSession, project_id: uuid.UUID
    ) -> Sequence[ProjectFile]:
        """List a project's uploaded input files, oldest first."""
        result = await db.execute(
            select(ProjectFile)
            .where(ProjectFile.project_id == project_id)
            .order_by(ProjectFile.created_at)
        )
        return result.scalars().all()

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

    async def upsert_for_project(
        self,
        db: AsyncSession,
        *,
        project_id: uuid.UUID,
        original_filename: str,
        storage_key: str,
        file_size_bytes: int,
        content_type: str,
    ) -> ProjectFile:
        """Insert a file row, or update it in place if the storage key already exists.

        Keeps the one-to-many table idempotent: a re-upload to the same key
        (e.g. same filename) refreshes metadata instead of creating a duplicate,
        while a new key adds another file to the project.
        """
        result = await db.execute(
            select(ProjectFile).where(
                ProjectFile.project_id == project_id,
                ProjectFile.storage_key == storage_key,
            )
        )
        existing = result.scalars().first()
        if existing is not None:
            existing.original_filename = original_filename
            existing.file_size_bytes = file_size_bytes
            existing.content_type = content_type
            db.add(existing)
            await db.flush()
            await db.refresh(existing)
            return existing

        return await self.create_for_project(
            db,
            project_id=project_id,
            original_filename=original_filename,
            storage_key=storage_key,
            file_size_bytes=file_size_bytes,
            content_type=content_type,
        )


project_file_repository = ProjectFileRepository(ProjectFile)
