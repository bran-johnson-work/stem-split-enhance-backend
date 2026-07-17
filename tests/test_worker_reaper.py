"""Reaper unit tests (RCA-1): abandoned PROCESSING jobs are requeued or failed.

Follows the suite's no-DB/no-network style: the session is a mock and the
repository calls the reaper makes are patched, so we assert on the ORM state
transitions the reaper applies rather than on any real database.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from app.models.processing_job import JobStatus, ProcessingJob
from app.models.project import Project, ProjectStatus
from app.worker import reaper


def _job(attempts: int, max_attempts: int = 3) -> ProcessingJob:
    return ProcessingJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        status=JobStatus.PROCESSING,
        progress=50.0,
        current_step="Working",
        attempts=attempts,
        max_attempts=max_attempts,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        heartbeat_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _project(project_id: uuid.UUID, status: ProjectStatus) -> Project:
    return Project(id=project_id, user_id=uuid.uuid4(), name="p", status=status)


async def test_requeues_job_under_attempt_cap(fake_db, monkeypatch):
    job = _job(attempts=1)
    project = _project(job.project_id, ProjectStatus.PROCESSING)
    monkeypatch.setattr(
        reaper.processing_job_repository,
        "list_stale_processing",
        AsyncMock(return_value=[job]),
    )
    monkeypatch.setattr(
        reaper.project_repository, "get", AsyncMock(return_value=project)
    )

    count = await reaper.reclaim_stale_jobs(fake_db, timeout_seconds=90)

    assert count == 1
    # Job goes back to the queue with run-scoped state reset, attempts preserved.
    assert job.status == JobStatus.PENDING
    assert job.progress == 0.0
    assert job.started_at is None
    assert job.heartbeat_at is None
    assert job.error_message is None
    assert job.attempts == 1
    # Project walks back from PROCESSING to QUEUED in lock-step.
    assert project.status == ProjectStatus.QUEUED


async def test_fails_job_at_attempt_cap(fake_db, monkeypatch):
    job = _job(attempts=3, max_attempts=3)
    project = _project(job.project_id, ProjectStatus.PROCESSING)
    monkeypatch.setattr(
        reaper.processing_job_repository,
        "list_stale_processing",
        AsyncMock(return_value=[job]),
    )
    monkeypatch.setattr(
        reaper.project_repository, "get", AsyncMock(return_value=project)
    )

    count = await reaper.reclaim_stale_jobs(fake_db, timeout_seconds=90)

    assert count == 1
    assert job.status == JobStatus.FAILED
    assert job.error_message == reaper.STALL_ERROR
    assert job.completed_at is not None
    assert project.status == ProjectStatus.FAILED


async def test_requeue_leaves_non_processing_project_untouched(fake_db, monkeypatch):
    # e.g. the user deleted/replaced the project while the worker was dying.
    job = _job(attempts=1)
    project = _project(job.project_id, ProjectStatus.COMPLETED)
    monkeypatch.setattr(
        reaper.processing_job_repository,
        "list_stale_processing",
        AsyncMock(return_value=[job]),
    )
    monkeypatch.setattr(
        reaper.project_repository, "get", AsyncMock(return_value=project)
    )

    await reaper.reclaim_stale_jobs(fake_db, timeout_seconds=90)

    assert job.status == JobStatus.PENDING
    assert project.status == ProjectStatus.COMPLETED  # not walked back


async def test_noop_when_nothing_stale(fake_db, monkeypatch):
    monkeypatch.setattr(
        reaper.processing_job_repository,
        "list_stale_processing",
        AsyncMock(return_value=[]),
    )
    got = AsyncMock()
    monkeypatch.setattr(reaper.project_repository, "get", got)

    count = await reaper.reclaim_stale_jobs(fake_db, timeout_seconds=90)

    assert count == 0
    got.assert_not_awaited()
