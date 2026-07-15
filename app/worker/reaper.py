"""Reaper: recover jobs stranded by a dead worker (RCA-1).

A worker claims a job (``PENDING -> PROCESSING``) in a committed transaction and
then heartbeats while it runs. If the process dies — OOM, SIGKILL after a deploy
grace period, a crash — no exception handler runs, so the job would sit in
``PROCESSING`` forever and ``get_active_for_project`` would block the project
from ever being re-enqueued.

This sweep closes that gap. Any ``PROCESSING`` job whose heartbeat has gone stale
is reclaimed: if it still has attempts left it goes back to ``PENDING`` for
another worker; once it has exhausted ``max_attempts`` it is marked ``FAILED`` so
a job that reliably kills its worker can't loop forever. The owning project is
moved back in lock-step (``QUEUED`` on requeue, ``FAILED`` when giving up).

Purely operational: no HTTP, no engine. The runner drives it on a timer and owns
the ``commit`` (mirroring how it drives ``claim_next_pending``).
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.processing_job import JobStatus
from app.models.project import ProjectStatus
from app.repositories.processing_job_repository import processing_job_repository
from app.repositories.project_repository import project_repository

logger = logging.getLogger(__name__)

REQUEUE_STEP = "Requeued after worker stall"
STALL_ERROR = "Worker stopped responding; gave up after exhausting retry attempts."


async def reclaim_stale_jobs(db: AsyncSession, *, timeout_seconds: float) -> int:
    """Requeue or fail every abandoned in-flight job. Returns how many it handled.

    The caller must ``commit``. Idempotent under concurrency: stale rows are
    locked ``FOR UPDATE SKIP LOCKED`` so two reapers never touch the same job.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
    stale = await processing_job_repository.list_stale_processing(db, cutoff)
    if not stale:
        return 0

    now = datetime.now(timezone.utc)
    for job in stale:
        project = await project_repository.get(db, job.project_id)
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.FAILED
            job.current_step = "Failed"
            job.error_message = STALL_ERROR
            job.completed_at = now
            if project is not None:
                project.status = ProjectStatus.FAILED
                db.add(project)
            logger.warning(
                "worker.reaper.failed id=%s project=%s attempts=%d/%d",
                job.id, job.project_id, job.attempts, job.max_attempts,
            )
        else:
            # Return the job to the queue for a fresh attempt. Reset the run-scoped
            # fields so the next worker starts clean; ``attempts`` is preserved
            # (already counted at claim) so the cap still bites.
            job.status = JobStatus.PENDING
            job.progress = 0.0
            job.started_at = None
            job.heartbeat_at = None
            job.error_message = None
            job.current_step = REQUEUE_STEP
            # Only walk the project back if it was mid-processing; leave any other
            # state (e.g. a user already deleted/replaced it) untouched.
            if project is not None and project.status == ProjectStatus.PROCESSING:
                project.status = ProjectStatus.QUEUED
                db.add(project)
            logger.info(
                "worker.reaper.requeued id=%s project=%s attempts=%d/%d",
                job.id, job.project_id, job.attempts, job.max_attempts,
            )
        db.add(job)

    return len(stale)
