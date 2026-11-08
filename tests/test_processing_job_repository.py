"""ProcessingJobRepository queue-mechanics tests (RCA-1 lease additions)."""

import uuid
from datetime import datetime, timezone

from app.models.processing_job import JobStatus, ProcessingJob
from app.repositories.processing_job_repository import processing_job_repository


def _pending_job() -> ProcessingJob:
    return ProcessingJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        status=JobStatus.PENDING,
        progress=0.0,
        current_step="",
        attempts=0,
        max_attempts=3,
    )


async def test_claim_marks_processing_stamps_heartbeat_and_counts_attempt(
    fake_db, execute_result
):
    job = _pending_job()
    fake_db.execute.return_value = execute_result(job)

    claimed = await processing_job_repository.claim_next_pending(fake_db)

    assert claimed is job
    assert job.status == JobStatus.PROCESSING
    assert job.attempts == 1  # counted so the reaper can cap requeues
    assert job.started_at is not None
    assert job.heartbeat_at is not None  # stale window starts at claim
    fake_db.flush.assert_awaited_once()


async def test_claim_returns_none_when_queue_empty(fake_db, execute_result):
    fake_db.execute.return_value = execute_result(None)
    assert await processing_job_repository.claim_next_pending(fake_db) is None


async def test_record_heartbeat_issues_update_without_select(fake_db):
    await processing_job_repository.record_heartbeat(fake_db, uuid.uuid4())
    # One UPDATE, no read-modify-write round trip.
    fake_db.execute.assert_awaited_once()


async def test_update_progress_issues_single_update_without_select(fake_db):
    await processing_job_repository.update_progress(
        fake_db, uuid.uuid4(), progress=42.0, current_step="Enhancing vocals"
    )
    # RCA-3: exactly one statement (the UPDATE) — no preceding SELECT/get().
    fake_db.execute.assert_awaited_once()


async def test_list_stale_processing_returns_scalars(fake_db, execute_result):
    jobs = [_pending_job()]
    fake_db.execute.return_value = execute_result(jobs)

    got = await processing_job_repository.list_stale_processing(
        fake_db, datetime.now(timezone.utc)
    )

    assert got == jobs
    fake_db.execute.assert_awaited_once()
