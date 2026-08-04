"""API-side orchestration for processing jobs.

Runs inside the FastAPI process, so it must NOT import ``app.engine`` (that pulls the
heavy torch/demucs stack, which only the worker installs). Its job is to enqueue work
onto the Postgres-backed queue and to read job/stem state back out for the client.
"""

import hashlib
import logging
import uuid
from pathlib import Path

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import Settings, get_settings
from app.models.output_file import STEM_ORDER
from app.models.processing_job import JobStatus, ProcessingJob
from app.models.project import ProjectStatus
from app.repositories.output_file_repository import output_file_repository
from app.repositories.processing_job_repository import processing_job_repository
from app.repositories.project_repository import project_repository
from app.schemas.job import (
    ProcessingJobCreate,
    StemDownload,
    StemsCompareResponse,
    VersionStems,
)

logger = logging.getLogger(__name__)


def _ab_bucket(project_id: uuid.UUID) -> int:
    """Stable 0-99 bucket for a project.

    Hashed rather than random so a project's A/B assignment never changes: with
    ``random()`` a retry could land a project on the other side of the rollout and the
    user would watch their version flip between attempts. SHA-256 (not ``hash()``) because
    Python salts string hashing per process, so ``hash()`` would differ across API
    replicas and give the same project two different answers.
    """
    digest = hashlib.sha256(str(project_id).encode()).digest()
    return int.from_bytes(digest[:4], "big") % 100


def _versions_for(project_id: uuid.UUID, user_id: uuid.UUID, settings: Settings) -> list[str]:
    """Engine versions to enqueue for one request.

    Normally a single version (``ENGINE_VERSION_DEFAULT``). Under A/B, eligible projects
    get **both** v1 and v2 so the UI can compare them — which costs two full renders, so
    eligibility is gated by an explicit allowlist or a percentage bucket.
    """
    default = (settings.engine_version_default or "v1").strip().lower()
    if not settings.engine_ab_enabled:
        return [default]
    if str(user_id).lower() in settings.ab_user_allowlist_set:
        return ["v1", "v2"]
    if _ab_bucket(project_id) < max(0, min(100, settings.engine_ab_percent)):
        return ["v1", "v2"]
    return [default]


class JobService:
    """Enqueue processing work and expose job/stem status to the API."""

    def __init__(self) -> None:
        self.repository = processing_job_repository
        self.output_repository = output_file_repository

    async def enqueue(
        self, db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
    ) -> ProcessingJob | None:
        """Queue a project for separation. Idempotent while a job is in flight.

        Verifies the project belongs to the user and has an uploaded input file, then
        inserts a PENDING job per selected engine version. Returns the job the client
        should poll — the default version's, so an A/B project still reports the progress
        of the version it will actually be shown first. Returns ``None`` if the project
        isn't found for the user, and raises ``ValueError`` if it has no input file.

        Under A/B this enqueues **two** jobs (v1 and v2). They are separate rows claimed by
        separate worker fleets, which is what makes the v2 rollout safe: if v2 fails, the
        v1 job still delivers stems with no retry logic and no half-written output set.
        """
        project = await project_repository.get_for_user(db, project_id, user_id)
        if project is None:
            return None
        if not project.input_files:
            raise ValueError("Upload an audio file before starting processing.")

        settings = get_settings()
        versions = _versions_for(project_id, user_id, settings)
        default = (settings.engine_version_default or "v1").strip().lower()

        jobs: dict[str, ProcessingJob] = {}
        created = False
        for version in versions:
            # Scoped per version: under dual-run a project legitimately has a v1 and a v2
            # job in flight together, and an unscoped check would skip the second.
            active = await self.repository.get_active_for_project(db, project_id, version)
            if active is not None:
                jobs[version] = active
                continue
            jobs[version] = await self.repository.create(
                db, ProcessingJobCreate(project_id=project_id, engine_version=version)
            )
            created = True

        if created:
            await project_repository.update(db, project, {"status": ProjectStatus.QUEUED})
        await db.commit()
        for job in jobs.values():
            await db.refresh(job)

        primary = jobs.get(default) or next(iter(jobs.values()))
        logger.info(
            "job.enqueued project=%s versions=%s primary=%s",
            project_id, sorted(jobs), primary.id,
        )
        return primary

    async def get_job(
        self, db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID, job_id: uuid.UUID
    ) -> ProcessingJob | None:
        """Get one job, verifying the project belongs to the user."""
        project = await project_repository.get_for_user(db, project_id, user_id)
        if project is None:
            return None
        return await self.repository.get_for_project(db, project_id=project_id, job_id=job_id)

    async def list_jobs(
        self, db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
    ):
        """List a project's jobs, or ``None`` if the project isn't the user's."""
        project = await project_repository.get_for_user(db, project_id, user_id)
        if project is None:
            return None
        return await self.repository.list_for_project(db, project_id)

    async def get_stem_downloads(
        self,
        db: AsyncSession,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        job_id: uuid.UUID,
        expires_in: int = 3600,
    ) -> list[StemDownload] | None:
        """Signed download URLs for a completed job's stems.

        Returns ``None`` when the project/job isn't found for the user, or an empty
        list when the job has produced no stems yet.
        """
        job = await self.get_job(db, project_id=project_id, user_id=user_id, job_id=job_id)
        if job is None:
            return None
        return await self._downloads_for_job(db, job_id, expires_in)


    async def _downloads_for_job(
        self, db: AsyncSession, job_id: uuid.UUID, expires_in: int
    ) -> list[StemDownload]:
        """Signed (or file://) download refs for one job's stems, without ownership checks.

        Private: every caller must already have verified the project belongs to the user.
        """
        local_dir = get_settings().stems_local_dir
        outputs = await self.output_repository.list_for_job(db, job_id)
        downloads: list[StemDownload] = []
        for out in outputs:
            if local_dir:
                url = (Path(local_dir) / out.storage_key).resolve().as_uri()
                expires = 0
            else:
                url = await run_in_threadpool(
                    storage.get_signed_url, storage.BUCKET_STEMS, out.storage_key, expires_in
                )
                expires = expires_in
            downloads.append(
                StemDownload(
                    stem_name=out.stem_name,
                    is_enhanced=out.is_enhanced,
                    url=url,
                    expires_in=expires,
                )
            )
        return downloads

    async def get_stems_comparison(
        self,
        db: AsyncSession,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        expires_in: int = 3600,
    ) -> StemsCompareResponse | None:
        """Every engine version that has run for this project, with signed stem URLs.

        One call returns everything the side-by-side view needs — the UI should not have to
        orchestrate a job list plus N stem lookups to render a comparison. ``None`` when the
        project isn't the user's.

        A version still rendering (or failed) comes back with an empty ``stems`` list and
        its real ``status``, so the UI can offer the finished version immediately instead of
        waiting on the slower one — v2 takes roughly twice as long as v1.
        """
        project = await project_repository.get_for_user(db, project_id, user_id)
        if project is None:
            return None

        jobs = await self.repository.latest_per_version(db, project_id)
        versions: list[VersionStems] = []
        seen: set[str] = set()
        for job in jobs:
            stems = await self._downloads_for_job(db, job.id, expires_in)
            seen.update(s.stem_name for s in stems)
            versions.append(
                VersionStems(
                    engine_version=job.engine_version or "v1",
                    job_id=job.id,
                    status=job.status,
                    engine_config=job.engine_config,
                    stems=stems,
                )
            )

        # Union across versions, in catalogue order: the versions can disagree about which
        # stems exist (different models => different content-gate verdicts), and the UI
        # needs every name to render an explicit "not in this version" cell.
        ordered = [n for n in STEM_ORDER if n in seen]
        ordered += sorted(seen.difference(STEM_ORDER))
        return StemsCompareResponse(
            project_id=project_id, stem_names=ordered, versions=versions
        )


job_service = JobService()


# Terminal statuses reused by the worker to decide when a project is done.
TERMINAL_JOB_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})
