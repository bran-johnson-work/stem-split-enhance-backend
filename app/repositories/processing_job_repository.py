import uuid
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import func, select, update
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

        now = datetime.now(timezone.utc)
        job.status = JobStatus.PROCESSING
        job.started_at = now
        # Stamp liveness immediately so the reaper's stale window starts from the
        # claim, and count the attempt so the reaper can cap poison-pill requeues.
        job.heartbeat_at = now
        job.attempts = (job.attempts or 0) + 1
        job.current_step = "Claimed by worker"
        db.add(job)
        await db.flush()
        return job

    async def update_progress(
        self, db: AsyncSession, job_id: uuid.UUID, *, progress: float, current_step: str
    ) -> None:
        """Write progress/step in a single UPDATE — no read-modify-write (RCA-3).

        Scoped to ``status == PROCESSING`` so a late progress write can't clobber
        a job that has already completed, failed, or been requeued by the reaper.
        Progress is clamped to 0..100. The caller owns the ``commit`` and decides
        *when* a write is worth doing (see the throttle in app.worker.processor).
        """
        await db.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == job_id,
                ProcessingJob.status == JobStatus.PROCESSING,
            )
            .values(progress=max(0.0, min(100.0, progress)), current_step=current_step)
        )

    async def record_heartbeat(self, db: AsyncSession, job_id: uuid.UUID) -> None:
        """Refresh the liveness stamp for an in-flight job — one UPDATE, no SELECT.

        Scoped to ``status == PROCESSING`` so a heartbeat that races a completion
        (or a reaper requeue) can't resurrect a job's timestamp after it has left
        the processing state. Called periodically by the owning worker.
        """
        await db.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == job_id,
                ProcessingJob.status == JobStatus.PROCESSING,
            )
            .values(heartbeat_at=datetime.now(timezone.utc))
        )

    async def list_stale_processing(
        self, db: AsyncSession, cutoff: datetime
    ) -> Sequence[ProcessingJob]:
        """Return PROCESSING jobs whose liveness stamp predates ``cutoff``.

        A job is stale when ``COALESCE(heartbeat_at, started_at)`` is older than
        the cutoff — i.e. its worker has stopped heartbeating (crashed/killed).
        Rows are locked ``FOR UPDATE SKIP LOCKED`` so concurrent reapers on
        multiple workers never act on the same job twice.
        """
        result = await db.execute(
            select(ProcessingJob)
            .where(
                ProcessingJob.status == JobStatus.PROCESSING,
                func.coalesce(ProcessingJob.heartbeat_at, ProcessingJob.started_at)
                < cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        return result.scalars().all()

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

    async def count_by_status(self, db: AsyncSession) -> dict[JobStatus, int]:
        """Job counts grouped by status (admin health view). Absent statuses are
        missing; the service fills them with 0."""
        result = await db.execute(
            select(ProcessingJob.status, func.count()).group_by(ProcessingJob.status)
        )
        return {status: int(count) for status, count in result.all()}

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
