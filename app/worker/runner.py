"""The worker loop: prewarm the model, then drain the Postgres job queue forever."""

import asyncio
import logging

from app.core.config import Settings, get_settings
from app.core.database import AsyncSessionLocal
from app.core.logging import configure_logging
from app.repositories.processing_job_repository import processing_job_repository
from app.worker.processor import process_job
from app.worker.reaper import reclaim_stale_jobs

logger = logging.getLogger(__name__)


async def _reaper_loop(stop_event: asyncio.Event, settings: Settings) -> None:
    """Periodically reclaim jobs abandoned by dead workers (RCA-1).

    Runs alongside the claim loop on the same ``stop_event`` so it shuts down
    cleanly with the worker. Each sweep is its own short transaction; an error is
    logged and retried on the next interval rather than taking the worker down.
    Safe to run on every worker — the sweep locks rows ``SKIP LOCKED``.
    """
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as db:
                count = await reclaim_stale_jobs(
                    db, timeout_seconds=settings.job_stale_timeout_seconds
                )
                await db.commit()
                if count:
                    logger.info("worker.reaper.swept count=%d", count)
        except Exception:
            logger.exception("worker.reaper.error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.reaper_interval_seconds)
        except asyncio.TimeoutError:
            pass


async def _prewarm(versions: list[str]) -> None:
    """Load the model weights into memory once at startup (first run downloads them).

    Done before the loop so the first job doesn't pay the download/load cost mid-progress.
    Only the models this fleet's ``versions`` actually need are loaded — a v1 container
    must not pull the ~600 MB RoFormer checkpoint it will never run, and a v2-only
    container should surface a bad checkpoint at boot rather than on a customer's job.

    A failure here is logged but not fatal — the first job will retry.
    """
    from app.engine.dsp import get_model_and_device

    settings = get_settings()
    # Both versions use Demucs: v1 for every stem, v2 for the rhythm section.
    try:
        logger.info("worker.prewarm.start versions=%s", versions or ["*"])
        await asyncio.to_thread(get_model_and_device)  # htdemucs_ft
        if settings.enable_instruments:
            await asyncio.to_thread(get_model_and_device, settings.instruments_model)
    except Exception:
        logger.exception("worker.prewarm.demucs_failed (will retry on first job)")

    if not versions or "v2" in versions:
        try:
            from app.engine import roformer

            await asyncio.to_thread(roformer.get_vocals_model)
        except Exception:
            logger.exception("worker.prewarm.roformer_failed (will retry on first job)")

    logger.info("worker.prewarm.ready")


async def run(stop_event: asyncio.Event | None = None) -> None:
    """Claim-and-process loop. Sleeps ``worker_poll_interval_seconds`` when idle."""
    settings = get_settings()
    configure_logging(settings.log_level)
    # Which engine versions this container claims. The v1 and v2 services share one
    # Postgres queue and are told apart only by this list — a v2 job claimed by a v1
    # image would run the wrong model and label the output as v2, so it is a
    # correctness boundary rather than a tuning knob.
    versions = settings.worker_engine_version_list
    await _prewarm(versions)
    logger.info(
        "worker.started poll=%.1fs engine_versions=%s",
        settings.worker_poll_interval_seconds, versions or ["*"],
    )

    stop_event = stop_event or asyncio.Event()

    # Background sweep that recovers jobs stranded by a dead worker (RCA-1).
    reaper: asyncio.Task | None = None
    if settings.worker_run_reaper:
        reaper = asyncio.create_task(_reaper_loop(stop_event, settings))

    try:
        while not stop_event.is_set():
            job_id = None
            version = None
            # Claim in its own short transaction so the row lock is released immediately.
            async with AsyncSessionLocal() as db:
                job = await processing_job_repository.claim_next_pending(db, versions)
                if job is not None:
                    job_id = job.id
                    version = job.engine_version
                    await db.commit()
                else:
                    await db.commit()

            if job_id is None:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=settings.worker_poll_interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                continue

            logger.info("worker.claimed id=%s version=%s", job_id, version)
            async with AsyncSessionLocal() as db:
                await process_job(db, job_id)
    finally:
        if reaper is not None:
            await reaper  # shares stop_event, so it unblocks and exits cleanly
