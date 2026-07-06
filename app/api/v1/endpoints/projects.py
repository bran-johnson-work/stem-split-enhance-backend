import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.project import (
    InputUrlResponse,
    ProjectResponse,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.services.project_service import project_service

router = APIRouter()


@router.post("/upload", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def upload_audio(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    project_id: uuid.UUID | None = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Upload an audio file to a new project, or add it to an existing one.

    Pass ``project_id`` to attach the file to an existing project (one-to-many);
    omit it to create a new project (subject to the plan's monthly quota).
    """
    project_name = (name or "").strip() or (file.filename or "Untitled project")
    try:
        project = await project_service.create_and_upload(
            db, user_id=current_user.id, name=project_name, file=file, project_id=project_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.post(
    "/upload-url", response_model=UploadUrlResponse, status_code=status.HTTP_201_CREATED
)
async def create_upload_url(
    payload: UploadUrlRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Create a project and return a one-time URL to upload its audio directly.

    The client PUTs the file to the returned ``url`` (with ``token``), then calls
    ``/{project_id}/upload-complete`` to finalise it.
    """
    project_name = (payload.name or "").strip() or (payload.filename or "Untitled project")
    try:
        result = await project_service.create_upload_url(
            db,
            user_id=current_user.id,
            name=project_name,
            filename=payload.filename,
            content_type=payload.content_type,
            project_id=payload.project_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project, signed = result
    return UploadUrlResponse(
        project_id=project.id,
        storage_key=signed["storage_key"],
        url=signed["url"],
        token=signed["token"],
    )


@router.post("/{project_id}/upload-complete", response_model=ProjectResponse)
async def complete_upload(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Finalise a presigned upload: verify the file landed and queue the project."""
    try:
        project = await project_service.complete_upload(
            db, project_id=project_id, user_id=current_user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    skip: int = 0,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """List the authenticated user's projects."""
    return await project_service.repository.list_for_user(
        db, current_user.id, skip=skip, limit=limit
    )


@router.get("/{project_id}", response_model=ProjectResponse)
async def read_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Get one of the authenticated user's projects."""
    project = await project_service.repository.get_for_user(db, project_id, current_user.id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.get("/{project_id}/input-url", response_model=InputUrlResponse)
async def get_input_url(
    project_id: uuid.UUID,
    file_id: uuid.UUID | None = None,
    expires_in: int = 3600,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Return a short-lived signed URL to download a project's original audio.

    Pass ``file_id`` to target a specific input file; otherwise the most
    recently uploaded file is returned.
    """
    url = await project_service.get_input_url(
        db,
        project_id=project_id,
        user_id=current_user.id,
        file_id=file_id,
        expires_in=expires_in,
    )
    if url is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project or input file not found"
        )
    return InputUrlResponse(url=url, expires_in=expires_in)
