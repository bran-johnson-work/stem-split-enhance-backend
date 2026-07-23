import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.project import (
    FileUploadUrlRequest,
    InputUrlResponse,
    ProjectCreateRequest,
    ProjectResponse,
    UploadUrlRequest,
    UploadUrlResponse,
)
from app.services.project_service import FileTooLargeError, project_service

router = APIRouter()


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    payload: ProjectCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Create an empty project, then upload file(s) into it via a presigned URL.

    Projects are unlimited; the metered unit is audio files. The monthly file
    quota is enforced when each file's upload URL is requested at
    ``/{project_id}/files/upload-url``, not here.
    """
    project_name = (payload.name or "").strip() or "Untitled project"
    try:
        project = await project_service.create_project(
            db, user_id=current_user.id, name=project_name
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return project


@router.post(
    "/upload-url", response_model=UploadUrlResponse, status_code=status.HTTP_201_CREATED
)
async def create_upload_url(
    payload: UploadUrlRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Create a project and return a presigned POST to upload its audio directly.

    The client POSTs the file to the returned ``url`` as multipart/form-data with
    the returned ``fields``, then calls ``/{project_id}/upload-complete``.
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
            declared_size_bytes=payload.file_size_bytes,
        )
    except FileTooLargeError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project, signed = result
    return UploadUrlResponse(
        project_id=project.id,
        storage_key=signed["storage_key"],
        url=signed["url"],
        fields=signed["fields"],
    )


@router.post(
    "/{project_id}/files/upload-url",
    response_model=UploadUrlResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_upload_url(
    project_id: uuid.UUID,
    payload: FileUploadUrlRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Mint a presigned POST to upload a file into an existing project.

    The client POSTs the file to the returned ``url`` as multipart/form-data with
    the returned ``fields``, then calls ``/{project_id}/upload-complete``.
    """
    try:
        result = await project_service.create_file_upload_url(
            db,
            project_id=project_id,
            user_id=current_user.id,
            filename=payload.filename,
            content_type=payload.content_type,
            declared_size_bytes=payload.file_size_bytes,
        )
    except FileTooLargeError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project, signed = result
    return UploadUrlResponse(
        project_id=project.id,
        storage_key=signed["storage_key"],
        url=signed["url"],
        fields=signed["fields"],
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
    except FileTooLargeError as exc:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
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
    # Bounded 1s..7d so a caller can't request a negative/absurd TTL.
    expires_in: int = Query(3600, gt=0, le=604800),
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


@router.delete("/{project_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_file(
    project_id: uuid.UUID,
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a single uploaded file from a project (storage object + record)."""
    result = await project_service.delete_input_file(
        db, project_id=project_id, user_id=current_user.id, file_id=file_id
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    if result is False:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a project and all of its storage (inputs and stems)."""
    result = await project_service.delete_project(
        db, project_id=project_id, user_id=current_user.id
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
