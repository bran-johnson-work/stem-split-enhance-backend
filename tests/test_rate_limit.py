import app.core.rate_limit as rate_limit_module
from app.core.rate_limit import SlidingWindowRateLimiter


def test_allows_up_to_the_limit():
    rl = SlidingWindowRateLimiter(max_attempts=3, window_seconds=100)
    assert rl.is_blocked("k") is False
    rl.register_failure("k")
    rl.register_failure("k")
    assert rl.is_blocked("k") is False  # 2 < 3


def test_blocks_after_max_attempts():
    rl = SlidingWindowRateLimiter(max_attempts=3, window_seconds=100)
    for _ in range(3):
        rl.register_failure("k")
    assert rl.is_blocked("k") is True


def test_reset_clears_a_key():
    rl = SlidingWindowRateLimiter(max_attempts=2, window_seconds=100)
    rl.register_failure("k")
    rl.register_failure("k")
    assert rl.is_blocked("k") is True
    rl.reset("k")
    assert rl.is_blocked("k") is False


def test_keys_are_independent():
    rl = SlidingWindowRateLimiter(max_attempts=1, window_seconds=100)
    rl.register_failure("a")
    assert rl.is_blocked("a") is True
    assert rl.is_blocked("b") is False


def test_window_expiry_frees_the_key(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(rate_limit_module.time, "monotonic", lambda: now[0])
    rl = SlidingWindowRateLimiter(max_attempts=2, window_seconds=10)
    rl.register_failure("k")
    rl.register_failure("k")
    assert rl.is_blocked("k") is True
    now[0] += 11  # window elapsed
    assert rl.is_blocked("k") is False
