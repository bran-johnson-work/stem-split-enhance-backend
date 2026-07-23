import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.models.plan import Plan
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


class FileTooLargeError(ValueError):
    """Raised when an upload exceeds the user's plan file-size limit.

    Subclasses ``ValueError`` so existing ``except ValueError`` paths still catch
    it, while endpoints can map it specifically to HTTP 413.
    """


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

    async def _effective_plan_and_window(
        self, db: AsyncSession, user_id: uuid.UUID
    ) -> tuple[Plan | None, datetime]:
        """Resolve the user's effective plan and the start of the current usage window.

        An ACTIVE/TRIALING subscription's plan is authoritative; otherwise the user
        is on the ``free`` plan. The usage window is the subscription's billing
        period when known, else the current calendar month (UTC). One subscription
        lookup feeds both the project-quota gate and the file-size gate.
        """
        subscription = await subscription_repository.get_for_user(db, user_id)
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # A paid subscriber's plan applies even when the billing period is unknown:
        # ``current_period_start`` only scopes the counting window, not *which* plan
        # is in force. It is routinely NULL — newer Stripe API versions drop it from
        # the subscription object (it lives on the items now), and a manually-created
        # subscription may never set it. Gating the plan on it silently downgraded
        # such a subscriber to the ``free`` cap; fall the *window* back instead.
        if (
            subscription is not None
            and subscription.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)
            and subscription.plan is not None
        ):
            return subscription.plan, subscription.current_period_start or month_start

        plan = await plan_repository.get_by_name(db, DEFAULT_PLAN_NAME)
        return plan, month_start

    async def _effective_plan(self, db: AsyncSession, user_id: uuid.UUID) -> Plan | None:
        """The user's effective plan (active subscription's plan, else ``free``)."""
        plan, _ = await self._effective_plan_and_window(db, user_id)
        return plan

    def _enforce_file_size(self, plan: Plan | None, size_bytes: int) -> None:
        """Reject an upload whose size exceeds the plan's per-file limit.

        A missing plan (``free`` unseeded) or a non-positive/unknown size is a
        no-op — we only hard-block when we have both a real limit and a real size.
        Raises :class:`FileTooLargeError`.
        """
        if plan is None or size_bytes <= 0:
            return
        limit_bytes = plan.max_file_size_mb * 1024 * 1024
        if size_bytes > limit_bytes:
            raise FileTooLargeError(
                f"File exceeds your plan's {plan.max_file_size_mb} MB upload limit."
            )

    def _max_upload_bytes(self, plan: Plan | None) -> int:
        """The byte ceiling to bake into a presigned upload policy for this plan.

        The plan is authoritative, mirroring :meth:`_enforce_file_size` and
        :meth:`complete_upload`, which both gate on the plan alone — if this
        disagreed with them, a user would get a URL they can't actually use (or
        vice versa). ``settings.max_upload_size_bytes`` is only a fallback for when
        the plan is unresolvable (the ``free`` row unseeded), because a presign
        policy needs a concrete number where those two checks can simply no-op.

        Note this is deliberately *not* clamped by ``max_upload_size_bytes``: that
        setting existed to respect Supabase's global storage cap, and S3 has no
        such platform ceiling. Clamping would silently cut every paid tier down to
        the free-tier default.
        """
        if plan is None:
            return get_settings().max_upload_size_bytes
        return plan.max_file_size_mb * 1024 * 1024

    async def _enforce_file_quota(self, db: AsyncSession, user_id: uuid.UUID) -> None:
        """Reject a new upload when the user is at their plan's monthly file cap.

        The metered unit is *audio files per month* (a project can hold many),
        so the count is over ``project_files``, not projects. The window is the
        active subscription's billing period when present, otherwise the current
        calendar month (UTC) against the ``free`` plan. Plans with
        ``max_files_per_month is None`` are unlimited. Called before minting each
        presigned upload URL, so an over-quota file is blocked *before* any bytes
        are uploaded. Raises ``ValueError`` when the cap is reached.
        """
        plan, window_start = await self._effective_plan_and_window(db, user_id)

        limit = plan.max_files_per_month if plan is not None else None
        if limit is None:
            # Unlimited plan, or the free plan is unseeded — don't hard-block uploads.
            if plan is None:
                logger.warning(
                    "No '%s' plan found while enforcing quota; allowing upload.",
                    DEFAULT_PLAN_NAME,
                )
            return

        used = await self.file_repository.count_for_user_since(db, user_id, window_start)
        if used >= limit:
            raise ValueError(
                f"Monthly upload limit reached ({limit} audio files). "
                "Upgrade your plan to upload more."
            )

    async def create_project(
        self, db: AsyncSession, *, user_id: uuid.UUID, name: str
    ) -> Project:
        """Create an empty project (no file yet).

        Projects are unlimited — the metered unit is audio files, enforced when
        each file is uploaded (see :meth:`_enforce_file_quota`). The project
        starts ``PENDING`` and moves to ``UPLOADING``/``QUEUED`` as files arrive.
        """
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
        declared_size_bytes: int | None = None,
    ) -> tuple[Project, dict[str, Any]] | None:
        """Mint a presigned URL to upload a file into an *existing* project.

        The project must already exist and belong to the user (create it first
        via :meth:`create_project`). Each file counts against the plan's monthly
        file cap, enforced here before the URL is minted. Returns ``None`` when
        the project isn't found. Raises ``ValueError`` on an unsupported content
        type or when the monthly file quota is exceeded, or
        :class:`FileTooLargeError` when a declared size exceeds the plan limit.
        Delegates to :meth:`create_upload_url` with the project pinned, keeping a
        single presign code path.
        """
        return await self.create_upload_url(
            db,
            user_id=user_id,
            name="",  # ignored: the project already exists, so name isn't used
            filename=filename,
            content_type=content_type,
            project_id=project_id,
            declared_size_bytes=declared_size_bytes,
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

        try:
            await run_in_threadpool(
                storage.delete_file, storage.BUCKET_INPUTS, target.storage_key
            )
        except Exception:  # pragma: no cover - storage cleanup is best-effort
            logger.warning("Failed to delete storage object %s", target.storage_key)
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
        declared_size_bytes: int | None = None,
    ) -> tuple[Project, dict[str, Any]] | None:
        """Create a project and mint a presigned POST for a *direct* client upload.

        The bytes go client -> S3; the API only validates the declared content
        type and hands back a one-time upload POST. The final file metadata is
        recorded later by :meth:`complete_upload`.

        The POST is minted *per plan*: the policy's ``content-length-range`` is the
        caller's own ``max_file_size_mb`` (see :meth:`_max_upload_bytes`), so S3
        rejects an over-plan upload itself rather than accepting the bytes and
        having us delete them afterwards.

        When ``project_id`` is given the URL targets an existing project (adding
        another file to it); otherwise a new project is created after a
        monthly-quota check. Returns ``None`` when ``project_id`` is supplied but
        not found for the user. Raises ``ValueError`` on an unsupported content
        type or when the quota is exceeded, or :class:`FileTooLargeError` when a
        declared size exceeds the plan limit.

        ``declared_size_bytes`` is a fast-fail hint: it lets us reject an
        oversized upload *before* minting the URL, but a client can under-declare
        or omit it — the authoritative size check runs on the real stored bytes
        in :meth:`complete_upload`.
        """
        settings = get_settings()

        content_type = (content_type or "").lower()
        if content_type not in settings.allowed_audio_content_type_set:
            raise ValueError(f"Unsupported audio content type: {content_type or 'unknown'}")

        # The plan is always resolved now, not just when the client declared a
        # size: S3 has no bucket-level size cap, so the user's plan limit has to be
        # baked into the presign policy for storage to enforce it at upload time.
        plan = await self._effective_plan(db, user_id)
        if declared_size_bytes is not None:
            self._enforce_file_size(plan, declared_size_bytes)
        max_size_bytes = self._max_upload_bytes(plan)

        original_filename = _sanitize_filename(filename)

        if project_id is not None:
            project = await self.repository.get_for_user(db, project_id, user_id)
            if project is None:
                return None
            # Every file counts against the monthly cap, including ones added to
            # an existing project — check before minting so the upload is blocked
            # up front rather than after the bytes land.
            await self._enforce_file_quota(db, user_id)
            project = await self.repository.update(
                db, project, {"status": ProjectStatus.UPLOADING}
            )
        else:
            await self._enforce_file_quota(db, user_id)
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
            content_type=content_type,
            max_size_bytes=max_size_bytes,
            expires_in=settings.s3_upload_url_expire_seconds,
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
        ``ValueError`` if no uploaded file exists under the project prefix, or
        :class:`FileTooLargeError` if a stored file exceeds the plan's size limit.
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

        # Authoritative file-size enforcement, on the real stored bytes. The
        # presign policy's content-length-range already rejects an oversize upload
        # at S3, but a URL minted before a plan downgrade (or against a stale
        # ceiling) can still outlive that limit — so re-check and purge here
        # before recording anything.
        plan = await self._effective_plan(db, user_id)
        limit_bytes = plan.max_file_size_mb * 1024 * 1024 if plan is not None else None
        if limit_bytes is not None:
            oversize = [
                o
                for o in stored
                if int((o.get("metadata") or {}).get("size") or 0) > limit_bytes
            ]
            if oversize:
                for o in oversize:
                    try:
                        await run_in_threadpool(
                            storage.delete_file,
                            storage.BUCKET_INPUTS,
                            f"{prefix}/{o['name']}",
                        )
                    except Exception:  # pragma: no cover - best-effort cleanup
                        logger.warning("Failed to delete oversize upload %s", o.get("name"))
                raise FileTooLargeError(
                    f"File exceeds your plan's {plan.max_file_size_mb} MB upload limit."
                )

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
