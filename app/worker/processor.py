"""Process a single claimed job: download -> separate/enhance -> upload -> record."""

import asyncio
import logging
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import storage
from app.core.config import get_settings
from app.engine.pipeline import FinalizedStem, separate_and_finalize
from app.models.processing_job import JobStatus
from app.models.project import ProjectStatus
from app.repositories.output_file_repository import output_file_repository
from app.repositories.processing_job_repository import processing_job_repository
from app.repositories.project_file_repository import project_file_repository
from app.repositories.project_repository import project_repository
from app.worker.progress import ProgressReporter

logger = logging.getLogger(__name__)


async def _set_progress(db: AsyncSession, job_id: uuid.UUID, progress: float, step: str) -> None:
    job = await processing_job_repository.get(db, job_id)
    if job is None:
        return
    job.progress = max(0.0, min(100.0, progress))
    job.current_step = step
    db.add(job)
    await db.commit()


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
        await _set_progress(db, job_id, 2.0, "Downloading input audio")
        data = await asyncio.to_thread(
            storage.download_file, storage.BUCKET_INPUTS, input_file.storage_key
        )
        suffix = Path(input_file.original_filename).suffix.lower() or ".wav"
        input_path = scratch / f"input{suffix}"
        input_path.write_bytes(data)

        # 3. Run the engine off the event loop; relay progress into the DB while it runs.
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
            )
        )
        while not task.done():
            update = reporter.take()
            if update is not None:
                await _set_progress(db, job_id, update[0], update[1])
            await asyncio.sleep(1.0)
        finalized, _sr = await task

        # 4. Upload each stem to the stems bucket and record it.
        prefix = f"{project.user_id}/{project.id}/stems"
        for idx, stem in enumerate(finalized):
            await _set_progress(
                db, job_id, 98.0 + idx / max(1, len(finalized)) * 1.5, f"Uploading {stem.stem_name}"
            )
            storage_key = f"{prefix}/{stem.stem_name}.wav"
            await asyncio.to_thread(
                storage.upload_file,
                storage.BUCKET_STEMS,
                storage_key,
                stem.path.read_bytes(),
                "audio/wav",
            )
            await _record_output(db, project.id, job_id, storage_key, stem)
        await db.commit()

        # 5. Mark the job and project completed.
        job = await processing_job_repository.get(db, job_id)
        if job is not None:
            job.status = JobStatus.COMPLETED
            job.progress = 100.0
            job.current_step = "Done"
            job.completed_at = datetime.now(timezone.utc)
            db.add(job)
        project = await project_repository.get(db, project.id)
        if project is not None:
            project.status = ProjectStatus.COMPLETED
            db.add(project)
        await db.commit()
        logger.info("worker.job_completed id=%s stems=%d", job_id, len(finalized))
    except Exception as exc:  # worker boundary: record on the job, keep the loop alive
        logger.exception("worker.job_failed id=%s", job_id)
        await db.rollback()
        await _mark_failed(db, job_id, project.id, f"{type(exc).__name__}: {exc}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


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
