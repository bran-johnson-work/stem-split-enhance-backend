import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.output_file import OutputFile
from app.repositories.base_repository import BaseRepository
from app.schemas.job import ProcessingJobCreate, ProcessingJobUpdate


class OutputFileRepository(BaseRepository[OutputFile, ProcessingJobCreate, ProcessingJobUpdate]):
    """Data access for the separated/enhanced stem files a job produces."""

    async def create_for_job(
        self,
        db: AsyncSession,
        *,
        project_id: uuid.UUID,
        job_id: uuid.UUID,
        stem_name: str,
        storage_key: str,
        file_size_bytes: int | None,
        is_enhanced: bool,
    ) -> OutputFile:
        db_obj = OutputFile(
            project_id=project_id,
            job_id=job_id,
            stem_name=stem_name,
            storage_key=storage_key,
            file_size_bytes=file_size_bytes,
            is_enhanced=is_enhanced,
        )
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def list_for_job(self, db: AsyncSession, job_id: uuid.UUID) -> Sequence[OutputFile]:
        result = await db.execute(
            select(OutputFile)
            .where(OutputFile.job_id == job_id)
            .order_by(OutputFile.stem_name)
        )
        return result.scalars().all()

    async def list_for_project(
        self, db: AsyncSession, project_id: uuid.UUID
    ) -> Sequence[OutputFile]:
        result = await db.execute(
            select(OutputFile)
            .where(OutputFile.project_id == project_id)
            .order_by(OutputFile.stem_name)
        )
        return result.scalars().all()


output_file_repository = OutputFileRepository(OutputFile)
