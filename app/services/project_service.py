import logging
import os
import uuid
from datetime import datetime, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.models.project import Project, ProjectStatus
from app.models.subscription import SubscriptionStatus
from app.repositories.plan_repository import plan_repository
from app.repositories.project_file_repository import project_file_repository
from app.repositories.project_repository import project_repository
from app.repositories.subscription_repository import subscription_repository
from app.schemas.project import ProjectCreate

logger = logging.getLogger(__name__)

# Plan a user falls back to when they have no (active) subscription row.
DEFAULT_PLAN_NAME = "free"


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

    async def _enforce_project_quota(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        """Reject creation when the user is at their plan's monthly project cap.

        The window is the active subscription's billing period when present,
        otherwise the current calendar month (UTC) against the ``free`` plan.
        Plans with ``max_projects_per_month is None`` are unlimited. Counting is
        an indexed ``COUNT`` on ``projects`` — cheap because it is bounded per
        user; swap for a usage-ledger table if billing-period contention appears.
        """
        subscription = await subscription_repository.get_for_user(db, user_id)
        now = datetime.now(timezone.utc)

        if (
            subscription is not None
            and subscription.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)
            and subscription.current_period_start is not None
        ):
            plan = subscription.plan
            window_start = subscription.current_period_start
        else:
            plan = await plan_repository.get_by_name(db, DEFAULT_PLAN_NAME)
            window_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        limit = plan.max_projects_per_month if plan is not None else None
        if limit is None:
            # Unlimited plan, or the free plan is unseeded — don't hard-block uploads.
            if plan is None:
                logger.warning(
                    "No '%s' plan found while enforcing quota; allowing upload.",
                    DEFAULT_PLAN_NAME,
                )
            return

        used = await self.repository.count_for_user_since(db, user_id, window_start)
        if used >= limit:
            raise ValueError(
                f"Monthly project limit reached ({limit}). Upgrade your plan to create more."
            )

    async def create_project(
        self, db: AsyncSession, *, user_id: uuid.UUID, name: str
    ) -> Project:
        """Create an empty project (no file yet), enforcing the monthly quota.

        A project is the metered unit, so the quota check lives here — the
        subsequent presigned upload(s) into this project are free. The project
        starts ``PENDING`` and moves to ``UPLOADING``/``QUEUED`` as files arrive.
        Raises ``ValueError`` when the quota is exceeded.
        """
        await self._enforce_project_quota(db, user_id)
        project = await self.repository.create(
            db,
            ProjectCreate(user_id=user_id, name=name, status=ProjectStatus.PENDING),
        )
        await db.commit()
        await db.refresh(project, attribute_names=["input_files"])
        return project

    async def create_file_upload_url(
        self,
        db: AsyncSession,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        filename: str,
        content_type: str,
    ) -> tuple[Project, dict[str, str]] | None:
        """Mint a presigned URL to upload a file into an *existing* project.

        The project must already exist and belong to the user (create it first
        via :meth:`create_project`); no quota is consumed here. Returns ``None``
        when the project isn't found. Raises ``ValueError`` on an unsupported
        content type. Delegates to :meth:`create_upload_url` with the project
        pinned, keeping a single presign code path.
        """
        return await self.create_upload_url(
            db,
            user_id=user_id,
            name="",  # ignored: the project already exists, so name isn't used
            filename=filename,
            content_type=content_type,
            project_id=project_id,
        )

    async def delete_input_file(
        self,
        db: AsyncSession,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        file_id: uuid.UUID,
    ) -> bool | None:
        """Delete one uploaded file from a project (storage object + DB row).

        Returns ``True`` on success, ``None`` when the project isn't found for
        the user, and ``False`` when the file isn't found under the project.
        Storage is removed before the row so a failure can't orphan the DB.
        """
        project = await self.repository.get_for_user(db, project_id, user_id)
        if project is None:
            return None

        target = await self.file_repository.get_for_project(
            db, project_id=project_id, file_id=file_id
        )
        if target is None:
            return False

        await run_in_threadpool(
            storage.delete_file, storage.BUCKET_INPUTS, target.storage_key
        )
        await db.delete(target)
        await db.commit()
        return True

    async def delete_project(
        self, db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool | None:
        """Delete a project entirely: its storage objects and the DB row (cascade).

        Purges everything under the project's storage prefix in both the input
        and stem buckets, then deletes the project (input files, jobs and output
        files cascade at the DB level). Returns ``True`` on success or ``None``
        when the project isn't found for the user.
        """
        project = await self.repository.get_for_user(db, project_id, user_id)
        if project is None:
            return None

        prefix = f"{user_id}/{project.id}"
        for bucket in (storage.BUCKET_INPUTS, storage.BUCKET_STEMS):
            try:
                await run_in_threadpool(storage.delete_prefix, bucket, prefix)
            except Exception:  # pragma: no cover - storage cleanup is best-effort
                logger.warning("Failed to purge storage prefix %s in %s", prefix, bucket)

        await db.delete(project)
        await db.commit()
        return True

    async def create_upload_url(
        self,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        name: str,
        filename: str,
        content_type: str,
        project_id: uuid.UUID | None = None,
    ) -> tuple[Project, dict[str, str]] | None:
        """Create a project and mint a presigned URL for a *direct* client upload.

        The bytes go client -> Supabase; the API only validates the declared
        content type and hands back a one-time upload URL. The final file
        metadata is recorded later by :meth:`complete_upload`.

        When ``project_id`` is given the URL targets an existing project (adding
        another file to it); otherwise a new project is created after a
        monthly-quota check. Returns ``None`` when ``project_id`` is supplied but
        not found for the user. Raises ``ValueError`` on an unsupported content
        type or when the quota is exceeded.
        """
        settings = get_settings()

        content_type = (content_type or "").lower()
        if content_type not in settings.allowed_audio_content_type_set:
            raise ValueError(f"Unsupported audio content type: {content_type or 'unknown'}")

        original_filename = _sanitize_filename(filename)

        if project_id is not None:
            project = await self.repository.get_for_user(db, project_id, user_id)
            if project is None:
                return None
            project = await self.repository.update(
                db, project, {"status": ProjectStatus.UPLOADING}
            )
        else:
            await self._enforce_project_quota(db, user_id)
            # Create the project first so the storage key can be namespaced by its id.
            project = await self.repository.create(
                db,
                ProjectCreate(user_id=user_id, name=name, status=ProjectStatus.UPLOADING),
            )
        storage_key = f"{user_id}/{project.id}/{original_filename}"

        signed = await run_in_threadpool(
            storage.create_signed_upload_url,
            storage.BUCKET_INPUTS,
            storage_key,
        )
        await db.commit()
        await db.refresh(project)

        return project, {"storage_key": storage_key, **signed}

    async def complete_upload(
        self, db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> Project | None:
        """Confirm a presigned upload finished, recording the stored file(s).

        Verifies the objects actually landed in storage (never trusting client
        input) and records each one under the project (one-to-many), reading its
        real size / content type, before moving the project to ``QUEUED``.
        Idempotent: re-recording an existing storage key refreshes it in place.
        Returns ``None`` if the project isn't found for the user; raises
        ``ValueError`` if no uploaded file exists under the project prefix.
        """
        project = await self.repository.get_for_user(db, project_id, user_id)
        if project is None:
            return None

        prefix = f"{user_id}/{project.id}"
        objects = await run_in_threadpool(
            storage.list_objects, storage.BUCKET_INPUTS, prefix
        )
        stored = [o for o in objects if o.get("name")]
        if not stored:
            if project.input_files:
                return project  # nothing new in storage but already recorded — idempotent
            raise ValueError("No uploaded file found for this project")

        for obj in stored:
            metadata = obj.get("metadata") or {}
            filename = obj["name"]
            await self.file_repository.upsert_for_project(
                db,
                project_id=project.id,
                original_filename=filename,
                storage_key=f"{prefix}/{filename}",
                file_size_bytes=int(metadata.get("size") or 0),
                content_type=metadata.get("mimetype") or "application/octet-stream",
            )
        if project.status != ProjectStatus.QUEUED:
            project = await self.repository.update(db, project, {"status": ProjectStatus.QUEUED})
        await db.commit()

        await db.refresh(project, attribute_names=["input_files"])
        return project

    async def get_input_url(
        self,
        db: AsyncSession,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        file_id: uuid.UUID | None = None,
        expires_in: int = 3600,
    ) -> str | None:
        """Return a short-lived signed URL for a project's input audio, if any.

        With ``file_id`` the URL points at that specific file (must belong to the
        project); without it, the most recently uploaded file is used.
        """
        project = await self.repository.get_for_user(db, project_id, user_id)
        if project is None or not project.input_files:
            return None

        if file_id is not None:
            target = next((f for f in project.input_files if f.id == file_id), None)
        else:
            target = max(project.input_files, key=lambda f: f.created_at)
        if target is None:
            return None

        return await run_in_threadpool(
            storage.get_signed_url,
            storage.BUCKET_INPUTS,
            target.storage_key,
            expires_in,
        )


project_service = ProjectService()
