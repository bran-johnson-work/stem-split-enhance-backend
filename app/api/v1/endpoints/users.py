import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.user import (
    AvatarUploadUrlRequest,
    AvatarUploadUrlResponse,
    UserResponse,
    UserUpdate,
)
from app.services.user_service import user_service

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def read_current_user(current_user: User = Depends(get_current_user)) -> Any:
    """Get current authenticated user."""
    return await user_service.serialize(current_user)


@router.post(
    "/me/avatar/upload-url",
    response_model=AvatarUploadUrlResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_avatar_upload_url(
    payload: AvatarUploadUrlRequest,
    current_user: User = Depends(get_current_user),
) -> Any:
    """Mint a presigned POST to upload a new profile avatar directly to S3.

    The client POSTs the image to the returned ``url`` as multipart/form-data
    with the returned ``fields``, then calls ``POST /users/me/avatar/complete``.
    """
    try:
        signed = await user_service.create_avatar_upload_url(
            user=current_user, content_type=payload.content_type
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return AvatarUploadUrlResponse(
        storage_key=signed["storage_key"], url=signed["url"], fields=signed["fields"]
    )


@router.post("/me/avatar/complete", response_model=UserResponse)
async def complete_avatar_upload(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Finalise an avatar upload: verify the image landed and record it."""
    try:
        user = await user_service.complete_avatar_upload(db, user=current_user)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return await user_service.serialize(user)


@router.delete("/me/avatar", response_model=UserResponse)
async def delete_avatar(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Remove the current user's profile avatar."""
    user = await user_service.remove_avatar(db, user=current_user)
    return await user_service.serialize(user)


@router.get("/{user_id}", response_model=UserResponse)
async def read_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Get user by ID (own account only)."""
    # Object-level authorization: a user may only read their own record. Without
    # this any authenticated user could enumerate every account's email/status.
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to access this user"
        )
    user = await user_service.repository.get(db, user_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return await user_service.serialize(user)


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: uuid.UUID,
    user_in: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Any:
    """Update user profile."""
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to update this user"
        )

    try:
        updated_user = await user_service.update_user(db, user_id=user_id, user_in=user_in)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if not updated_user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return await user_service.serialize(updated_user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a user."""
    if current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized to delete this user"
        )

    deleted = await user_service.repository.delete(db, user_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await db.commit()
