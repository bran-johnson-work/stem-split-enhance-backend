"""Process a single claimed job: download -> separate/enhance -> upload -> record."""

import asyncio
import logging
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.engine.pipeline import (
    FinalizedStem,
    engine_config_snapshot,
    separate_and_finalize,
)
from app.models.processing_job import JobStatus
from app.models.project import ProjectStatus
from app.repositories.output_file_repository import output_file_repository
from app.repositories.processing_job_repository import processing_job_repository
from app.repositories.project_file_repository import project_file_repository
from app.repositories.project_repository import project_repository
from app.worker.progress import ProgressReporter

logger = logging.getLogger(__name__)


async def _heartbeat_loop(
    job_id: uuid.UUID, stop: asyncio.Event, interval: float
) -> None:
    """Refresh the job's liveness stamp every ``interval`` seconds until stopped.

    Runs on the event loop (which is free while the engine grinds inside
    ``asyncio.to_thread``) in its *own* short-lived session, independent of the
    main processing session. A transient DB error is logged and retried on the
    next tick — a blip must not kill the job or the heartbeat. This is what lets
    the reaper distinguish a live-but-slow worker from a dead one (RCA-1).
    """
    while not stop.is_set():
        try:
            async with AsyncSessionLocal() as hb_db:
                await processing_job_repository.record_heartbeat(hb_db, job_id)
                await hb_db.commit()
        except Exception:
            logger.warning("worker.heartbeat.failed id=%s", job_id, exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


class _ProgressThrottle:
    """Decide when a progress update is worth a DB write (RCA-3 debounce).

    Progress is high-frequency, low-value, loss-tolerant data — persisting every
    tick means a re-planned SELECT+UPDATE+COMMIT through the pooler once a second.
    Instead we only write on updates that actually matter to a watcher:

    * the first update, or any change of ``step`` label (a stage boundary),
    * a progress jump of at least ``min_delta`` percentage points, or
    * ``min_interval`` seconds since the last write (a liveness tick so a long
      single stage doesn't look frozen).

    ``now`` is injectable so the time-based rule is testable without sleeping.
    """

    def __init__(self, *, min_delta: float, min_interval: float) -> None:
        self._min_delta = min_delta
        self._min_interval = min_interval
        self._last_progress: float | None = None
        self._last_step: str | None = None
        self._last_write: float = 0.0

    def should_write(
        self, progress: float, step: str, *, now: float | None = None
    ) -> bool:
        now = time.monotonic() if now is None else now
        first = self._last_step is None
        if (
            first
            or step != self._last_step
            or abs(progress - (self._last_progress or 0.0)) >= self._min_delta
            or (now - self._last_write) >= self._min_interval
        ):
            self._last_progress = progress
            self._last_step = step
            self._last_write = now
            return True
        return False


async def _set_progress(db: AsyncSession, job_id: uuid.UUID, progress: float, step: str) -> None:
    """Persist one progress update as a single UPDATE (RCA-3: no read-modify-write)."""
    await processing_job_repository.update_progress(
        db, job_id, progress=progress, current_step=step
    )
    await db.commit()


async def _emit_progress(
    db: AsyncSession,
    job_id: uuid.UUID,
    throttle: "_ProgressThrottle",
    progress: float,
    step: str,
) -> None:
    """Persist a progress update only when the throttle deems it worth a write."""
    if throttle.should_write(progress, step):
        await _set_progress(db, job_id, progress, step)


async def _mark_failed(
    db: AsyncSession, job_id: uuid.UUID, project_id: uuid.UUID, error: str
) -> None:
    job = await processing_job_repository.get(db, job_id)
    if job is not None:
        job.status = JobStatus.FAILED
        job.current_step = "Failed"
        job.error_message = error
        job.completed_at = datetime.now(timezone.utc)
        db.add(job)
    project = await project_repository.get(db, project_id)
    if project is not None:
        project.status = ProjectStatus.FAILED
        db.add(project)
    await db.commit()


async def process_job(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Run the full pipeline for one already-claimed (PROCESSING) job.

    Any exception is recorded on the job/project as FAILED rather than raised, so a
    single bad input never takes the worker down.
    """
    settings = get_settings()
    job = await processing_job_repository.get(db, job_id)
    if job is None:
        logger.warning("worker.job_missing id=%s", job_id)
        return
    project = await project_repository.get(db, job.project_id)
    if project is None:
        await _mark_failed(db, job_id, job.project_id, "Project no longer exists")
        return

    scratch = Path(
        tempfile.mkdtemp(prefix=f"stemjob_{str(job_id)[:8]}_", dir=settings.job_scratch_dir or None)
    )
    # Heartbeat for the whole run so the reaper won't reclaim this job while it's
    # genuinely progressing; stopped in ``finally`` once the job reaches a
    # terminal state (or raises).
    stop_heartbeat = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat_loop(job_id, stop_heartbeat, settings.worker_heartbeat_interval_seconds)
    )
    # Debounce progress writes so we don't hammer the DB once a second (RCA-3).
    throttle = _ProgressThrottle(
        min_delta=settings.worker_progress_min_delta,
        min_interval=settings.worker_progress_min_interval_seconds,
    )
    try:
        # 1. Locate the input file (most recently uploaded to the project).
        input_files = await project_file_repository.list_for_project(db, project.id)
        if not input_files:
            await _mark_failed(db, job_id, project.id, "No input file to process")
            return
        input_file = max(input_files, key=lambda f: f.created_at)

        await project_repository.update(db, project, {"status": ProjectStatus.PROCESSING})
        await db.commit()

        # 2. Download the input from Storage into the scratch dir.
        await _emit_progress(db, job_id, throttle, 2.0, "Downloading input audio")
        data = await asyncio.to_thread(
            storage.download_file, storage.BUCKET_INPUTS, input_file.storage_key
        )
        suffix = Path(input_file.original_filename).suffix.lower() or ".wav"
        input_path = scratch / f"input{suffix}"
        input_path.write_bytes(data)

        # 3. Run the engine off the event loop; relay progress into the DB while it runs.
        #    The version comes from the job row, not from this container's config: the
        #    claim filter already guaranteed this image can run it, and honouring the row
        #    is what keeps the recorded version and the actual models in agreement.
        engine_version = (job.engine_version or "v1").strip().lower()
        out_dir = scratch / "stems"
        reporter = ProgressReporter()
        task = asyncio.create_task(
            asyncio.to_thread(
                separate_and_finalize,
                input_path,
                out_dir,
                reporter,
                want_instruments=settings.enable_instruments,
                want_enhancement=settings.enable_enhancement,
                engine_version=engine_version,
            )
        )
        while not task.done():
            update = reporter.take()
            if update is not None:
                await _emit_progress(db, job_id, throttle, update[0], update[1])
            await asyncio.sleep(settings.worker_progress_poll_seconds)
        finalized, _sr = await task

        # 4. Persist each stem (S3 bucket, or a local folder if configured) and record it.
        #    v1 keeps the historical un-versioned prefix so already-delivered stems and
        #    their signed URLs stay valid; only newer versions get a segment of their own.
        #    Both stay under {user}/{project}/, so the delete_project prefix purge still
        #    reaches them.
        prefix = f"{project.user_id}/{project.id}/stems"
        if engine_version != "v1":
            prefix = f"{prefix}/{engine_version}"
        for idx, stem in enumerate(finalized):
            await _emit_progress(
                db,
                job_id,
                throttle,
                98.0 + idx / max(1, len(finalized)) * 1.5,
                f"Saving {stem.stem_name}",
            )
            storage_key = f"{prefix}/{stem.stem_name}.wav"
            await asyncio.to_thread(_persist_stem, settings, storage_key, stem)
            await _record_output(db, project.id, job_id, storage_key, stem)
        await db.commit()

        # 5. Mark the job and project completed.
        job = await processing_job_repository.get(db, job_id)
        if job is not None:
            job.status = JobStatus.COMPLETED
            job.progress = 100.0
            job.current_step = "Done"
            job.completed_at = datetime.now(timezone.utc)
            # Stamp what actually ran, so an A/B comparison is still interpretable once
            # the settings have moved on.
            job.engine_config = engine_config_snapshot(engine_version)
            db.add(job)
        project = await project_repository.get(db, project.id)
        if project is not None:
            project.status = ProjectStatus.COMPLETED
            db.add(project)
        await db.commit()
        logger.info(
            "worker.job_completed id=%s version=%s stems=%d",
            job_id, engine_version, len(finalized),
        )
    except Exception as exc:  # worker boundary: record on the job, keep the loop alive
        logger.exception("worker.job_failed id=%s", job_id)
        await db.rollback()
        await _mark_failed(db, job_id, project.id, f"{type(exc).__name__}: {exc}")
    finally:
        stop_heartbeat.set()
        await heartbeat
        shutil.rmtree(scratch, ignore_errors=True)


def _persist_stem(settings: Settings, storage_key: str, stem: FinalizedStem) -> None:
    """Write one finalized stem to its destination (runs off the event loop).

    Uploads to the S3 stems bucket by default. When ``stems_local_dir`` is
    configured, copies the file to ``<stems_local_dir>/<storage_key>`` on local
    disk instead, keeping stems off object storage entirely.
    """
    if settings.stems_local_dir:
        dest = Path(settings.stems_local_dir) / storage_key
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(stem.path, dest)
    else:
        storage.upload_file(storage.BUCKET_STEMS, storage_key, stem.path.read_bytes(), "audio/wav")


async def _record_output(
    db: AsyncSession,
    project_id: uuid.UUID,
    job_id: uuid.UUID,
    storage_key: str,
    stem: FinalizedStem,
) -> None:
    await output_file_repository.create_for_job(
        db,
        project_id=project_id,
        job_id=job_id,
        stem_name=stem.stem_name,
        storage_key=storage_key,
        file_size_bytes=stem.file_size_bytes,
        is_enhanced=stem.is_enhanced,
    )
