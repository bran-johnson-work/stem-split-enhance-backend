"""Entrypoint: ``python -m app.worker``.

Runs the separation worker until interrupted (SIGINT/SIGTERM), draining the
Postgres-backed job queue. Deploy one or more of these on a CPU/GPU host alongside
the API; they coordinate purely through AWS RDS Postgres + S3 object storage, using
the same app.core.database engine and app.core.storage client the API does.
"""

import asyncio
import signal

from app.worker.runner import run


async def _main() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # Windows: signal handlers on the loop aren't supported
            pass
    await run(stop_event)


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
