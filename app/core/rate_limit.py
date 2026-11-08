import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    """In-process sliding-window rate limiter for brute-force protection.

    Tracks recent attempt timestamps per key (e.g. client IP + email) and blocks
    once the count within the window exceeds ``max_attempts``.

    Note: state is per-process. For multi-instance deployments back this with a
    shared store (e.g. Redis); the interface is intentionally storage-agnostic.
    """

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self._max_attempts = max_attempts
        self._window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> None:
        bucket = self._hits[key]
        cutoff = now - self._window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            self._hits.pop(key, None)

    def is_blocked(self, key: str) -> bool:
        """True if ``key`` has already reached the attempt limit in the window."""
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            return len(self._hits[key]) >= self._max_attempts if key in self._hits else False

    def register_failure(self, key: str) -> None:
        """Record a failed attempt against ``key``."""
        now = time.monotonic()
        with self._lock:
            self._prune(key, now)
            self._hits[key].append(now)

    def reset(self, key: str) -> None:
        """Clear a key's history (call on successful authentication)."""
        with self._lock:
            self._hits.pop(key, None)

    def clear(self) -> None:
        """Drop all tracked state (primarily for tests)."""
        with self._lock:
            self._hits.clear()
