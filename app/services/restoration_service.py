"""Proxy to the external V2/V3 restoration service.

That service is a separate GPU host with no CORS and a static ``X-API-Key``. A
browser therefore cannot call it — shipping the key to the frontend would publish
it — so this backend is its only client and re-exposes the flow under the caller's
own session auth.

The upstream keeps jobs in memory and loses them on restart. Every poll is mirrored
into ``restoration_jobs`` so a finished result survives that, and so a job stays
attributable to a user and project after the fact.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.models.project import Project
from app.models.restoration_job import RestorationJob, RestorationStage, RestorationStatus
from app.repositories.project_repository import project_repository
from app.repositories.restoration_job_repository import restoration_job_repository
from app.schemas.restoration import (
    RestorationJobResponse,
    RestorationOutput,
    RestorationServiceStatus,
    RestorationStartRequest,
)
from app.services import restoration_progress
from app.services.project_service import FileTooLargeError, project_service

logger = logging.getLogger(__name__)

# Upstream statuses, mapped onto ours. Anything unrecognised is treated as still
# running rather than as success — inventing a DONE would show the user a result
# that does not exist.
_STATUS_MAP = {
    "queued": RestorationStatus.QUEUED,
    "running": RestorationStatus.RUNNING,
    "done": RestorationStatus.DONE,
    "failed": RestorationStatus.FAILED,
}


class RestorationError(Exception):
    """A restoration call failed in a way the caller should surface."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class RestorationDisabled(RestorationError):
    def __init__(self) -> None:
        super().__init__("Audio restoration is not configured on this server.", 503)


class RestorationService:
    def __init__(self) -> None:
        self.repository = restoration_job_repository
        # Shared health cache — see `service_status`. Process-local, like the login
        # rate limiter: with several API instances each keeps its own, which still
        # cuts the probe rate by the poll interval and is the right trade for one
        # cheap boolean.
        self._status_cache: tuple[str, RestorationServiceStatus] | None = None
        self._status_at: float = 0.0
        self._status_lock = asyncio.Lock()

    # --- upstream transport -------------------------------------------------

    def _settings(self):
        settings = get_settings()
        if not settings.restoration_enabled:
            raise RestorationDisabled()
        return settings

    def _client(self, settings) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=settings.restoration_base_url,
            headers={"X-API-Key": settings.restoration_api_key},
            timeout=settings.restoration_timeout_seconds,
        )

    async def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        settings = self._settings()
        try:
            async with self._client(settings) as client:
                response = await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise RestorationError("The restoration service did not respond in time.", 504) from exc
        except httpx.HTTPError as exc:
            # The URL and key are deployment config, not something a user can fix, so
            # the detail goes to the log and the caller gets a generic failure.
            logger.warning("Restoration service unreachable: %s", exc)
            raise RestorationError("The restoration service is unavailable.", 503) from exc

        if response.status_code >= 400:
            detail = _detail_of(response)
            # Preserved rather than folded into 502: upstream 404 means the job is
            # gone (it evicts at 200 jobs and loses everything on restart), and the
            # caller must be able to tell that apart from a transport failure.
            if response.status_code == 404:
                raise RestorationError(detail or "Job not found upstream.", 404)
            # 401 means *our* key is wrong. Surfacing "unauthorised" would suggest the
            # end user's session is at fault, which it is not.
            if response.status_code == 401:
                logger.error("Restoration service rejected our API key")
                raise RestorationError("The restoration service is misconfigured.", 502)
            if response.status_code == 503:
                raise RestorationError(detail or "The restoration service is busy.", 503)
            raise RestorationError(detail or "The restoration service rejected the request.", 502)

        try:
            body = response.json()
        except ValueError as exc:
            # A 200 that isn't JSON means the URL points at something that is not this
            # API — pointing it at a Streamlit UI is the easy mistake, since Streamlit
            # answers *every* path with its HTML shell, including /health. Treat it as
            # unreachable: a misconfigured URL must read as "service down", not crash
            # the status endpoint that exists to report exactly that.
            logger.error(
                "Restoration service returned %s, not JSON, for %s — is RESTORATION_API_URL "
                "pointing at the API?",
                response.headers.get("content-type", "?"),
                path,
            )
            raise RestorationError(
                "The restoration service returned an unexpected response. "
                "Check RESTORATION_API_URL points at the restoration API.",
                502,
            ) from exc

        if not isinstance(body, dict):
            raise RestorationError("The restoration service returned an unexpected response.", 502)
        return body

    # --- status -------------------------------------------------------------

    async def service_status(self) -> RestorationServiceStatus:
        """Health of the upstream, as something the UI can gate a button on.

        Cached for a few seconds and shared across callers. Every open restoration
        page re-checks this on a timer, and each check was a fresh HTTPS round trip
        to the GPU host — a host that is, by design, busy running one job at a time
        and answering that probe on the same event loop. With more than a handful of
        viewers the health probes alone were a measurable share of its traffic, and
        the answer does not change between two viewers a second apart.

        A single in-flight refresh is shared rather than raced, so N simultaneous
        cache misses make one upstream call, not N.
        """
        settings = get_settings()
        if not settings.restoration_enabled:
            return RestorationServiceStatus(
                enabled=False, reachable=False, detail="Not configured on this server."
            )

        ttl = settings.restoration_status_cache_seconds
        # Keyed by the upstream it describes, so pointing the deployment at a
        # different host cannot be answered out of the previous host's cache.
        key = settings.restoration_base_url

        cached = self._cached_status(key, ttl)
        if cached is not None:
            return cached

        async with self._status_lock:
            # Re-check inside the lock: whoever held it may have just refreshed,
            # and this waiter should take that result rather than issue its own.
            cached = self._cached_status(key, ttl)
            if cached is not None:
                return cached
            status = await self._fetch_status()
            self._status_cache = (key, status)
            self._status_at = time.monotonic()
            return status

    def _cached_status(self, key: str, ttl: float) -> RestorationServiceStatus | None:
        if self._status_cache is None:
            return None
        cached_key, status = self._status_cache
        if cached_key != key or time.monotonic() - self._status_at >= ttl:
            return None
        return status

    async def _fetch_status(self) -> RestorationServiceStatus:
        try:
            # `/health` is the one open route upstream — no key needed, but sending
            # ours costs nothing and keeps one code path.
            data = await self._request("GET", "/health")
        except RestorationError as exc:
            return RestorationServiceStatus(enabled=True, reachable=False, detail=str(exc))
        return RestorationServiceStatus(
            enabled=True,
            reachable=bool(data.get("ok")),
            cuda=bool(data.get("cuda")),
            gpu=data.get("gpu"),
            apollo=bool(data.get("apollo")),
            bucket_configured=bool(data.get("bucket_configured")),
            queue_depth=int(data.get("queue_depth") or 0),
            device=str(data.get("device") or ""),
            precision=str(data.get("precision") or ""),
            # Absent on an older upstream, which had no preload step and so was never
            # cold in this sense. Default to warm so it does not raise a false alarm.
            warm=bool(data.get("warm", True)),
        )

    # --- starting a stage ---------------------------------------------------

    async def start(
        self,
        db: AsyncSession,
        *,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        payload: RestorationStartRequest,
    ) -> RestorationJob:
        """Submit one stage and record it. Idempotent while that stage is in flight."""
        self._settings()

        project = await project_repository.get_for_user(db, project_id, user_id)
        if project is None:
            raise RestorationError("Project not found", 404)

        active = await self.repository.get_active(db, project_id, payload.stage)
        if active is not None:
            # The upstream runs one job at a time on one GPU; a duplicate submission
            # would queue behind its own twin and double the wait for nothing.
            return active

        source_key, source_bucket, parent = await self._resolve_source(
            db, project=project, user_id=user_id, payload=payload
        )

        body: dict[str, Any] = {
            "source_key": source_key,
            "source_bucket": source_bucket,
            "key_prefix": f"{user_id}/{project_id}/restored",
            "quality": payload.quality,
        }
        if payload.stage is RestorationStage.V3:
            body |= {
                "tempo": payload.tempo,
                "denoise": payload.denoise,
                "bandwidth": payload.bandwidth,
            }
            path = "/v3/restore-from-s3"
        else:
            path = "/v2/repair-from-s3"

        data = await self._request("POST", path, json=body)
        remote_id = data.get("job_id")
        if not remote_id:
            raise RestorationError("The restoration service returned no job id.", 502)

        job = await self.repository.create(
            db,
            project_id=project_id,
            user_id=user_id,
            stage=payload.stage,
            status=_STATUS_MAP.get(str(data.get("status", "")).lower(), RestorationStatus.QUEUED),
            remote_job_id=str(remote_id),
            source_key=source_key,
            parent_id=parent.id if parent is not None else None,
            options={k: v for k, v in body.items() if k not in {"source_key", "source_bucket"}},
        )
        await db.commit()
        await db.refresh(job)
        return job

    async def _resolve_source(
        self,
        db: AsyncSession,
        *,
        project: Project,
        user_id: uuid.UUID,
        payload: RestorationStartRequest,
    ) -> tuple[str, str, RestorationJob | None]:
        """Where this stage reads from: a parent's output, or the project's input."""
        if payload.after_job_id is not None:
            parent = await self.repository.get_for_user(db, payload.after_job_id, user_id)
            if parent is None:
                raise RestorationError("Previous restoration job not found", 404)
            if parent.project_id != project.id:
                raise RestorationError("That job belongs to a different project", 400)
            if parent.status is not RestorationStatus.DONE or not parent.output_key:
                # This is the chain's one hard ordering constraint: V3's input is a
                # file V2 has not written yet.
                raise RestorationError(
                    "The previous stage has not finished — its output does not exist yet.", 409
                )
            return parent.output_key, parent.output_bucket or storage.BUCKET_STEMS, parent

        if not project.input_files:
            # The browser uploaded straight to S3, so the bytes exist but nothing has
            # recorded them yet — `ProjectFile` rows are written by `complete_upload`,
            # and a restoration run deliberately does not call the endpoint that wraps
            # it (that endpoint also enqueues a *separation* job, which is a different
            # pipeline and would burn GPU nobody asked for).
            #
            # So register them here, through the same service call: it lists the real
            # objects rather than trusting the client, re-checks each size against the
            # plan, and is idempotent. Only the enqueue is skipped.
            try:
                project = await project_service.complete_upload(
                    db, project_id=project.id, user_id=user_id
                ) or project
            except FileTooLargeError as exc:
                raise RestorationError(str(exc), 413) from exc
            except ValueError as exc:
                raise RestorationError(str(exc), 400) from exc

        if not project.input_files:
            raise RestorationError("Upload an audio file before starting restoration.", 400)

        if payload.file_id is not None:
            chosen = next((f for f in project.input_files if f.id == payload.file_id), None)
            if chosen is None:
                raise RestorationError("File not found on this project", 404)
        else:
            chosen = project.input_files[0]
        return chosen.storage_key, storage.BUCKET_INPUTS, None

    # --- polling ------------------------------------------------------------

    async def sync(self, db: AsyncSession, job: RestorationJob) -> RestorationJob:
        """Refresh one job from the upstream and persist what changed.

        A finished job is never re-fetched: the upstream evicts jobs (200 retained,
        then LRU) and loses all of them on restart, so asking again risks trading a
        stored result for a 404.
        """
        if job.is_finished:
            return job

        try:
            data = await self._request("GET", f"/jobs/{job.remote_job_id}")
        except RestorationError as exc:
            if exc.status_code != 404:
                raise
            # The upstream forgot this job — it restarted, or evicted it. It is never
            # coming back, so fail the row now. Left QUEUED it would poll forever and
            # the UI would show a spinner with no end.
            job.status = RestorationStatus.FAILED
            job.error_message = (
                "The restoration service restarted before this run finished. "
                "Start it again."
            )
            job.completed_at = datetime.now(timezone.utc)
            db.add(job)
            await db.commit()
            await db.refresh(job)
            return job

        status = _STATUS_MAP.get(str(data.get("status", "")).lower())
        if status is None:
            logger.warning("Unknown restoration status %r for job %s", data.get("status"), job.id)
            return job

        job.status = status
        job.progress = list(data.get("progress") or [])
        # Not a column: how many runs are ahead of this one is true for as long as
        # this response takes to reach the browser and no longer, so persisting it
        # would only let a stale number outlive its meaning. Carried on the instance
        # for `to_response` to read on this request alone.
        job.queue_position = int(data.get("queue_position") or 0)
        job.report = data.get("report")
        job.quality = data.get("quality")
        job.error_message = data.get("error")

        outputs = data.get("outputs") or {}
        # The upstream labels the artifact by stage ("v2"/"v3"). Take the matching
        # one, but fall back to whatever single output it returned.
        chosen = outputs.get(job.stage.value) or (
            next(iter(outputs.values())) if len(outputs) == 1 else None
        )
        if chosen and chosen.get("key"):
            job.output_key = chosen["key"]
            job.output_bucket = data.get("bucket") or storage.BUCKET_STEMS

        if job.is_finished and job.completed_at is None:
            job.completed_at = datetime.now(timezone.utc)

        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job

    async def get(
        self, db: AsyncSession, *, job_id: uuid.UUID, user_id: uuid.UUID
    ) -> RestorationJob | None:
        return await self.repository.get_for_user(db, job_id, user_id)

    async def list_for_project(
        self, db: AsyncSession, *, project_id: uuid.UUID, user_id: uuid.UUID
    ):
        return await self.repository.list_for_project(db, project_id, user_id)

    # --- presentation -------------------------------------------------------

    def to_response(self, job: RestorationJob) -> RestorationJobResponse:
        """Attach a freshly-signed URL rather than storing one.

        The upstream mints an hour-long URL when the job finishes. A stored copy
        would be dead by the time a user reopened the page, so the URL is signed
        here, per request, from the key.
        """
        settings = get_settings()
        progress = list(job.progress or [])
        output = None
        if job.output_key:
            expires_in = settings.restoration_url_expire_seconds
            output = RestorationOutput(
                key=job.output_key,
                url=storage.get_signed_url(
                    job.output_bucket or storage.BUCKET_STEMS, job.output_key, expires_in
                ),
                expires_in=expires_in,
            )
        started = job.created_at
        end = job.completed_at or datetime.now(timezone.utc)
        # `created_at` is timezone-aware from the column, but a row built in a test or
        # refreshed from a driver that dropped the offset would make this subtraction
        # raise — and a progress field must never be the thing that fails a poll.
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        return RestorationJobResponse(
            id=job.id,
            project_id=job.project_id,
            stage=job.stage,
            status=job.status,
            parent_id=job.parent_id,
            progress=progress,
            current_step=progress[-1] if progress else "",
            progress_pct=restoration_progress.estimate(job.stage, job.status, progress),
            queue_position=int(getattr(job, "queue_position", 0) or 0),
            elapsed_seconds=max(0, int((end - started).total_seconds())),
            is_finished=job.is_finished,
            output=output,
            report=job.report,
            quality=job.quality,
            error_message=job.error_message,
            created_at=job.created_at,
            updated_at=job.updated_at,
            completed_at=job.completed_at,
        )


def _detail_of(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
    return ""


restoration_service = RestorationService()
