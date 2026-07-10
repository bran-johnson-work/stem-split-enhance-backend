"""Thread-safe progress relay between the (sync) engine thread and the async loop.

The engine runs in a worker thread via ``asyncio.to_thread`` and reports progress
through a plain callback. It cannot touch the async DB session directly, so it writes
the latest ``(progress, label)`` here; the async loop drains it and persists it.
"""

import threading


class ProgressReporter:
    """Collects the most recent progress update from the engine thread.

    Coalescing by design: only the latest update matters, so a burst of engine
    callbacks between two DB writes collapses into one write.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._latest: tuple[float, str] | None = None
        self._dirty = False

    def __call__(self, progress: float, label: str) -> None:
        """Called from the engine thread on every pipeline step."""
        with self._lock:
            self._latest = (progress, label)
            self._dirty = True

    def take(self) -> tuple[float, str] | None:
        """Return the latest update once, or ``None`` if nothing changed since."""
        with self._lock:
            if not self._dirty or self._latest is None:
                return None
            self._dirty = False
            return self._latest
