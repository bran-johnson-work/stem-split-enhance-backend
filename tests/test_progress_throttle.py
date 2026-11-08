"""Progress-write debounce tests (RCA-3): _ProgressThrottle decision table."""

from app.worker.processor import _ProgressThrottle


def _throttle() -> _ProgressThrottle:
    return _ProgressThrottle(min_delta=5.0, min_interval=10.0)


def test_first_update_always_writes():
    t = _throttle()
    assert t.should_write(0.0, "Downloading", now=0.0) is True


def test_label_change_writes_even_on_tiny_delta():
    t = _throttle()
    t.should_write(50.0, "Separating", now=0.0)
    # Same tiny progress bump but a new stage label -> write.
    assert t.should_write(50.1, "Enhancing vocals", now=0.5) is True


def test_small_delta_same_label_is_suppressed():
    t = _throttle()
    t.should_write(50.0, "Separating", now=0.0)
    # +2% (< 5) on the same label, well within the interval -> skip.
    assert t.should_write(52.0, "Separating", now=1.0) is False


def test_large_delta_same_label_writes():
    t = _throttle()
    t.should_write(50.0, "Separating", now=0.0)
    assert t.should_write(56.0, "Separating", now=1.0) is True  # +6% >= 5


def test_interval_elapsed_writes_a_liveness_tick():
    t = _throttle()
    t.should_write(50.0, "Separating", now=0.0)
    # Tiny delta, same label, but 10s have passed -> heartbeat write.
    assert t.should_write(51.0, "Separating", now=10.0) is True


def test_suppressed_update_does_not_advance_baseline():
    t = _throttle()
    t.should_write(50.0, "Separating", now=0.0)
    assert t.should_write(52.0, "Separating", now=1.0) is False  # skipped
    # Baseline stayed at 50, so a later 55 is a full +5 from it -> writes.
    assert t.should_write(55.0, "Separating", now=2.0) is True
