"""API-side orchestration for processing jobs.

Runs inside the FastAPI process, so it must NOT import ``app.engine`` (that pulls the
heavy torch/demucs stack, which only the worker installs). Its job is to enqueue work
onto the Postgres-backed queue and to read job/stem state back out for the client.
"""

import logging
import uuid

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.models.processing_job import JobStatus, ProcessingJob
from app.models.project import ProjectStatus
from app.repositories.output_file_repository import output_file_repository
from app.repositories.processing_job_repository import processing_job_repository
from app.repositories.project_repository import project_repository
from app.schemas.job import ProcessingJobCreate, StemDownload

logger = logging.getLogger(__name__)


class JobService:
    """Enqueue processing work and expose job/stem status to the API."""

    def __init__(self) -> None:
        self.repository = processing_job_repository
        self.output_repository = output_file_repository

    async def enqueue(
        self, db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> ProcessingJob | None:
        """Queue a project for separation. Idempotent while a job is in flight.

        Verifies the project belongs to the user and has an uploaded input file,
        then inserts a PENDING job the worker will pick up. Returns the (possibly
        pre-existing) job, or ``None`` if the project isn't found for the user.
        Raises ``ValueError`` if the project has no input file to process.
        """
        project = await project_repository.get_for_user(db, project_id, user_id)
        if project is None:
            return None
        if not project.input_files:
            raise ValueError("Upload an audio file before starting processing.")

        active = await self.repository.get_active_for_project(db, project_id)
        if active is not None:
            return active  # already queued/processing — don't double-enqueue

        job = await self.repository.create(db, ProcessingJobCreate(project_id=project_id))
        await project_repository.update(db, project, {"status": ProjectStatus.QUEUED})
        await db.commit()
        await db.refresh(job)
        logger.info("job.enqueued id=%s project=%s", job.id, project_id)
        return job

    async def get_job(
        self, db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID, job_id: uuid.UUID
    ) -> ProcessingJob | None:
        """Get one job, verifying the project belongs to the user."""
        project = await project_repository.get_for_user(db, project_id, user_id)
        if project is None:
            return None
        return await self.repository.get_for_project(db, project_id=project_id, job_id=job_id)

    async def list_jobs(
        self, db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
    ):
        """List a project's jobs, or ``None`` if the project isn't the user's."""
        project = await project_repository.get_for_user(db, project_id, user_id)
        if project is None:
            return None
        return await self.repository.list_for_project(db, project_id)

    async def get_stem_downloads(
        self,
        db: AsyncSession,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        job_id: uuid.UUID,
        expires_in: int = 3600,
    ) -> list[StemDownload] | None:
        """Signed download URLs for a completed job's stems.

        Returns ``None`` when the project/job isn't found for the user, or an empty
        list when the job has produced no stems yet.
        """
        job = await self.get_job(db, project_id=project_id, user_id=user_id, job_id=job_id)
        if job is None:
            return None

        outputs = await self.output_repository.list_for_job(db, job_id)
        downloads: list[StemDownload] = []
        for out in outputs:
            url = await run_in_threadpool(
                storage.get_signed_url, storage.BUCKET_STEMS, out.storage_key, expires_in
            )
            downloads.append(
                StemDownload(
                    stem_name=out.stem_name,
                    is_enhanced=out.is_enhanced,
                    url=url,
                    expires_in=expires_in,
                )
            )
        return downloads


job_service = JobService()


# Terminal statuses reused by the worker to decide when a project is done.
TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})
