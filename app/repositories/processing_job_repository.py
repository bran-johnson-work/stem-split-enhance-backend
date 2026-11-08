import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.processing_job import JobStatus, ProcessingJob
from app.repositories.base_repository import BaseRepository
from app.schemas.job import ProcessingJobCreate, ProcessingJobUpdate


class ProcessingJobRepository(
    BaseRepository[ProcessingJob, ProcessingJobCreate, ProcessingJobUpdate]
):
    """Data access for processing jobs, including the Postgres-backed work queue."""

    async def claim_next_pending(self, db: AsyncSession) -> ProcessingJob | None:
        """Atomically claim the oldest PENDING job and mark it PROCESSING.

        Uses ``FOR UPDATE SKIP LOCKED`` so many worker processes can poll the same
        table without ever handing the same job to two of them — Supabase Postgres
        is the queue, no Redis/broker required. The caller must ``commit`` to release
        the row lock. Returns ``None`` when the queue is empty.
        """
        result = await db.execute(
            select(ProcessingJob)
            .where(ProcessingJob.status == JobStatus.PENDING)
            .order_by(ProcessingJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = result.scalars().first()
        if job is None:
            return None

        job.status = JobStatus.PROCESSING
        job.started_at = datetime.now(timezone.utc)
        job.current_step = "Claimed by worker"
        db.add(job)
        await db.flush()
        return job

    async def get_for_project(
        self, db: AsyncSession, *, project_id: uuid.UUID, job_id: uuid.UUID
    ) -> ProcessingJob | None:
        """Get a single job by id, scoped to its owning project."""
        result = await db.execute(
            select(ProcessingJob).where(
                ProcessingJob.id == job_id,
                ProcessingJob.project_id == project_id,
            )
        )
        return result.scalars().first()

    async def list_for_project(
        self, db: AsyncSession, project_id: uuid.UUID
    ) -> Sequence[ProcessingJob]:
        """List a project's jobs, newest first."""
        result = await db.execute(
            select(ProcessingJob)
            .where(ProcessingJob.project_id == project_id)
            .order_by(ProcessingJob.created_at.desc())
        )
        return result.scalars().all()

    async def get_active_for_project(
        self, db: AsyncSession, project_id: uuid.UUID
    ) -> ProcessingJob | None:
        """Return a PENDING/PROCESSING job for the project, if one is in flight.

        Used to make ``/process`` idempotent — a second request while a job is
        already queued or running returns the existing job instead of duplicating work.
        """
        result = await db.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.project_id == project_id,
                ProcessingJob.status.in_((JobStatus.PENDING, JobStatus.PROCESSING)),
            )
            .order_by(ProcessingJob.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()


processing_job_repository = ProcessingJobRepository(ProcessingJob)
