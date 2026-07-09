import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.job import JobResponse, JobStemsResponse
from app.services.job_service import job_service

router = APIRouter()


@router.post(
    "/{project_id}/process",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_processing(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Queue a project's uploaded audio for separation.

    Idempotent while a job is already pending/processing (returns that job). A
    worker picks the job up from the Postgres queue; poll ``/jobs/{job_id}`` for
    progress and fetch results from ``/jobs/{job_id}/stems`` once completed.
    """
    try:
        job = await job_service.enqueue(db, project_id=project_id, user_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return job


@router.get("/{project_id}/jobs", response_model=list[JobResponse])
async def list_jobs(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """List a project's processing jobs, newest first."""
    jobs = await job_service.list_jobs(db, project_id=project_id, user_id=current_user.id)
    if jobs is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return jobs


@router.get("/{project_id}/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Poll one job's status/progress."""
    job = await job_service.get_job(
        db, project_id=project_id, user_id=current_user.id, job_id=job_id
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


@router.get("/{project_id}/jobs/{job_id}/stems", response_model=JobStemsResponse)
async def get_job_stems(
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    expires_in: int = Query(3600, gt=0, le=604800),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Signed download URLs for a completed job's stems.

    Returns an empty ``stems`` list while the job is still running (or produced
    nothing). URLs expire after ``expires_in`` seconds (bounded 1s..7d).
    """
    job = await job_service.get_job(
        db, project_id=project_id, user_id=current_user.id, job_id=job_id
    )
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    downloads = await job_service.get_stem_downloads(
        db,
        project_id=project_id,
        user_id=current_user.id,
        job_id=job_id,
        expires_in=expires_in,
    )
    return JobStemsResponse(job_id=job.id, status=job.status, stems=downloads or [])
