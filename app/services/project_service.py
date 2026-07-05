import logging
import os
import uuid

from fastapi import UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.models.project import Project, ProjectStatus
from app.repositories.project_file_repository import project_file_repository
from app.repositories.project_repository import project_repository
from app.schemas.project import ProjectCreate

logger = logging.getLogger(__name__)


def _sanitize_filename(filename: str | None) -> str:
    """Reduce an uploaded filename to a safe basename (no path traversal)."""
    if not filename:
        return "audio"
    # Guard against both POSIX and Windows separators regardless of host OS.
    base = os.path.basename(filename.replace("\\", "/")).strip()
    return base or "audio"


class ProjectService:
    """Business logic for projects and their audio uploads."""

    def __init__(self) -> None:
        self.repository = project_repository
        self.file_repository = project_file_repository

    async def create_and_upload(
        self, db: AsyncSession, *, user_id: uuid.UUID, name: str, file: UploadFile
    ) -> Project:
        """Validate an uploaded audio file, store it in Supabase, and record it.

        Raises ``ValueError`` on invalid input (empty file, unsupported type, too
        large) so the endpoint can translate it into a 4xx response.
        """
        settings = get_settings()

        content_type = (file.content_type or "").lower()
        if content_type not in settings.allowed_audio_content_type_set:
            raise ValueError(f"Unsupported audio content type: {file.content_type or 'unknown'}")

        data = await file.read()
        if not data:
            raise ValueError("Uploaded file is empty")
        if len(data) > settings.max_upload_size_bytes:
            raise ValueError(
                f"File exceeds the maximum size of {settings.max_upload_size_bytes} bytes"
            )

        original_filename = _sanitize_filename(file.filename)

        # Create the project first so the storage key can be namespaced by its id.
        project = await self.repository.create(
            db,
            ProjectCreate(user_id=user_id, name=name, status=ProjectStatus.UPLOADING),
        )

        storage_key = f"{user_id}/{project.id}/{original_filename}"

        try:
            # The Supabase client is synchronous; keep the event loop responsive.
            await run_in_threadpool(
                storage.upload_file,
                storage.BUCKET_INPUTS,
                storage_key,
                data,
                content_type,
            )

            await self.file_repository.create_for_project(
                db,
                project_id=project.id,
                original_filename=original_filename,
                storage_key=storage_key,
                file_size_bytes=len(data),
                content_type=content_type,
            )
            project = await self.repository.update(db, project, {"status": ProjectStatus.QUEUED})
            await db.commit()
        except Exception:
            await db.rollback()
            # Best-effort cleanup so we don't leave an orphaned storage object.
            try:
                await run_in_threadpool(
                    storage.delete_file, storage.BUCKET_INPUTS, storage_key
                )
            except Exception:  # pragma: no cover - cleanup is best-effort
                logger.warning("Failed to clean up orphaned upload at %s", storage_key)
            raise

        await db.refresh(project, attribute_names=["input_file"])
        return project

    async def get_input_url(
        self, db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID, expires_in: int = 3600
    ) -> str | None:
        """Return a short-lived signed URL for a project's input audio, if any."""
        project = await self.repository.get_for_user(db, project_id, user_id)
        if project is None or project.input_file is None:
            return None
        return await run_in_threadpool(
            storage.get_signed_url,
            storage.BUCKET_INPUTS,
            project.input_file.storage_key,
            expires_in,
        )


project_service = ProjectService()
