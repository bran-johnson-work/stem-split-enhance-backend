import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.project import InputUrlResponse, ProjectResponse
from app.services.project_service import project_service

router = APIRouter()


@router.post("/upload", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def upload_audio(
    file: UploadFile = File(...),
    name: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Upload an audio file, store it in Supabase, and create a project for it."""
    project_name = (name or "").strip() or (file.filename or "Untitled project")
    try:
        project = await project_service.create_and_upload(
            db, user_id=current_user.id, name=project_name, file=file
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
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
    expires_in: int = 3600,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Return a short-lived signed URL to download a project's original audio."""
    url = await project_service.get_input_url(
        db, project_id=project_id, user_id=current_user.id, expires_in=expires_in
    )
    if url is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project or input file not found"
        )
    return InputUrlResponse(url=url, expires_in=expires_in)
