import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.restoration import (
    RestorationJobResponse,
    RestorationServiceStatus,
    RestorationStartRequest,
)
from app.services.restoration_service import RestorationError, restoration_service

router = APIRouter()


@router.get("/restoration/status", response_model=RestorationServiceStatus)
async def restoration_status(
    current_user: User = Depends(get_current_user),
) -> Any:
    """Whether restoration is available, and on what hardware.

    Never fails: an unreachable service is a body the UI can render, not an error
    that would make the page look broken.
    """
    return await restoration_service.service_status()


@router.post(
    "/projects/{project_id}/restoration",
    response_model=RestorationJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_restoration(
    project_id: uuid.UUID,
    payload: RestorationStartRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Start one stage of restoration.

    Run ``v2`` first, poll it, then start ``v3`` with ``after_job_id`` set to the
    finished V2 job — that is what makes the run V2-then-V3 rather than two
    independent passes over the same original audio.

    Idempotent while a stage is in flight: a second call returns the running job.
    """
    try:
        job = await restoration_service.start(
            db, project_id=project_id, user_id=current_user.id, payload=payload
        )
    except RestorationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return restoration_service.to_response(job)


@router.get(
    "/projects/{project_id}/restoration",
    response_model=list[RestorationJobResponse],
)
async def list_restoration_jobs(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Every restoration run on this project, oldest first."""
    jobs = await restoration_service.list_for_project(
        db, project_id=project_id, user_id=current_user.id
    )
    return [restoration_service.to_response(job) for job in jobs]


@router.get(
    "/projects/{project_id}/restoration/{job_id}",
    response_model=RestorationJobResponse,
)
async def get_restoration_job(
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Poll one run. Refreshes from the upstream unless it has already finished."""
    job = await restoration_service.get(db, job_id=job_id, user_id=current_user.id)
    if job is None or job.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    try:
        job = await restoration_service.sync(db, job)
    except RestorationError as exc:
        # The stored row is still the truth we have; a transient upstream failure
        # should not turn a poll into an error the UI renders as a dead run.
        if exc.status_code not in (503, 504):
            raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return restoration_service.to_response(job)
