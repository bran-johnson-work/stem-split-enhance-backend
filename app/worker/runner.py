"""The worker loop: prewarm the model, then drain the Postgres job queue forever."""

import asyncio
import logging

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.core.logging import configure_logging
from app.repositories.processing_job_repository import processing_job_repository
from app.worker.processor import process_job

logger = logging.getLogger(__name__)


async def _prewarm() -> None:
    """Load the model weights into memory once at startup (first run downloads them).

    Done before the loop so the first job doesn't pay the ~400 MB download/load cost
    mid-progress. A failure here is logged but not fatal — the first job will retry.
    """
    from app.engine.dsp import get_model_and_device

    settings = get_settings()
    try:
        logger.info("worker.prewarm.start")
        await asyncio.to_thread(get_model_and_device)  # htdemucs_ft
        if settings.enable_instruments:
            await asyncio.to_thread(get_model_and_device, settings.instruments_model)
        logger.info("worker.prewarm.ready")
    except Exception:
        logger.exception("worker.prewarm.failed (will retry on first job)")


async def run(stop_event: asyncio.Event | None = None) -> None:
    """Claim-and-process loop. Sleeps ``worker_poll_interval_seconds`` when idle."""
    settings = get_settings()
    configure_logging(settings.log_level)
    await _prewarm()
    logger.info("worker.started poll=%.1fs", settings.worker_poll_interval_seconds)

    stop_event = stop_event or asyncio.Event()
    while not stop_event.is_set():
        job_id = None
        # Claim in its own short transaction so the row lock is released immediately.
        async with AsyncSessionLocal() as db:
            job = await processing_job_repository.claim_next_pending(db)
            if job is not None:
                job_id = job.id
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

        logger.info("worker.claimed id=%s", job_id)
        async with AsyncSessionLocal() as db:
            await process_job(db, job_id)
