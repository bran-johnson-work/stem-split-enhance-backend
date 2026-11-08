"""Empty-stem filtering (app.engine.silence).

Guarded with importorskip so the lean API test environment (requirements.txt only, no
numpy) skips this module instead of erroring; it runs wherever the worker deps are
present. Nothing here needs torch — the gate is deliberately numpy-only.
"""

import pytest

pytest.importorskip("numpy")

import numpy as np  # noqa: E402

from app.engine import silence  # noqa: E402

SR = 44100
DURATION = 8  # seconds — long enough for several 1 s measurement windows


def _tone(seconds: float = DURATION, amp: float = 0.5, freq: float = 220.0) -> np.ndarray:
    """A stereo tone at ``amp`` — stands in for a stem with real content."""
    t = np.arange(int(SR * seconds), dtype=np.float32) / SR
    mono = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    return np.stack([mono, mono], axis=0)


def _noise(seconds: float = DURATION, amp: float = 1e-4) -> np.ndarray:
    """Low-level noise — stands in for the residual a model emits for an absent source."""
    rng = np.random.default_rng(0)
    mono = (rng.standard_normal(int(SR * seconds)) * amp).astype(np.float32)
    return np.stack([mono, mono], axis=0)


def _sparse(seconds: float = DURATION, amp: float = 0.4, active: float = 0.5) -> np.ndarray:
    """Loud content for ``active`` seconds, digital silence for the rest.

    The case a plain overall-RMS gate gets wrong: an instrument playing only in the
    bridge has negligible average energy but is unmistakably present.
    """
    out = np.zeros((2, int(SR * seconds)), dtype=np.float32)
    out[:, : int(SR * active)] = _tone(active, amp)
    return out


# ---- measurement primitives ----


def test_dbfs_floors_digital_silence():
    assert silence.dbfs(0.0) == silence.DBFS_FLOOR
    assert silence.dbfs(1.0) == pytest.approx(0.0)
    assert silence.dbfs(0.5) == pytest.approx(-6.02, abs=0.05)


def test_rms_dbfs_of_silence_is_floored():
    assert silence.rms_dbfs(np.zeros((2, SR), dtype=np.float32)) == silence.DBFS_FLOOR


def test_rms_dbfs_of_full_scale_sine_is_minus_three():
    # A sine at amplitude 1.0 has RMS 1/sqrt(2) => -3.01 dBFS.
    assert silence.rms_dbfs(_tone(amp=1.0)) == pytest.approx(-3.01, abs=0.05)


def test_empty_array_measures_as_floor():
    assert silence.rms_dbfs(np.zeros((2, 0), dtype=np.float32)) == silence.DBFS_FLOOR
    assert silence.loudest_window_dbfs(np.zeros(0, dtype=np.float32), SR) == silence.DBFS_FLOOR


def test_loudest_window_beats_overall_rms_on_sparse_content():
    sparse = _sparse(active=0.5)  # 0.5 s of content in 8 s
    overall = silence.rms_dbfs(sparse)
    loudest = silence.loudest_window_dbfs(sparse, SR)
    # Averaged over the whole track the content nearly vanishes; the loudest window keeps it.
    assert loudest > overall + 5.0


def test_trailing_content_is_not_missed():
    """Content confined to a trailing partial window must still register."""
    total = int(SR * 3.5)
    out = np.zeros((2, total), dtype=np.float32)
    out[:, -int(SR * 0.4) :] = _tone(0.4, amp=0.5)
    # 3.5 s at a 1 s window = 3 full windows (all silent) + a tail that holds every sample.
    assert silence.loudest_window_dbfs(out, SR) > -30.0


def test_signal_shorter_than_one_window_still_measures():
    short = _tone(seconds=0.25, amp=0.5)
    assert silence.loudest_window_dbfs(short, SR) == pytest.approx(silence.rms_dbfs(short), abs=0.5)


def test_mono_input_is_accepted():
    mono = _tone()[0]
    assert silence.measure(mono, SR).peak_dbfs == pytest.approx(-6.02, abs=0.1)


# ---- the keep/drop decision ----


def test_real_stem_is_kept():
    mix_rms = silence.rms_dbfs(_tone(amp=0.7))
    verdict = silence.judge("vocals", silence.measure(_tone(), SR), mix_rms)
    assert verdict.keep


def test_silent_stem_is_dropped_on_the_absolute_floor():
    mix_rms = silence.rms_dbfs(_tone(amp=0.7))
    energy = silence.measure(np.zeros((2, SR * 4), dtype=np.float32), SR)
    verdict = silence.judge("guitar", energy, mix_rms)
    assert not verdict.keep
    assert "below floor" in verdict.reason


def test_low_level_residual_is_dropped():
    """The actual bug: an absent source comes back as quiet bleed, not as an empty file."""
    mix_rms = silence.rms_dbfs(_tone(amp=0.7))
    verdict = silence.judge("piano", silence.measure(_noise(), SR), mix_rms)
    assert not verdict.keep


def test_sparse_stem_survives_the_relative_floor():
    """An instrument playing 0.5 s in 8 s is real content and must not be pruned."""
    mix_rms = silence.rms_dbfs(_tone(amp=0.7))
    verdict = silence.judge("piano", silence.measure(_sparse(), SR), mix_rms)
    assert verdict.keep


def test_relative_floor_is_level_independent():
    """The same performance mastered 20 dB quieter must reach the same verdict.

    This is why the gate is not a bare absolute threshold.
    """
    quiet_mix = _tone(amp=0.7 * 0.1)
    quiet_stem = _tone(amp=0.5 * 0.1)
    verdict = silence.judge(
        "vocals", silence.measure(quiet_stem, SR), silence.rms_dbfs(quiet_mix)
    )
    assert verdict.keep


def test_loud_residual_on_a_quiet_track_is_still_dropped():
    """Clears the absolute floor, but is negligible in the context of its own mix."""
    mix_rms = silence.rms_dbfs(_tone(amp=0.9))
    residual = _noise(amp=1.5e-3)  # peak around -45 dBFS: above the -50 floor
    energy = silence.measure(residual, SR)
    assert energy.peak_dbfs > -50.0
    assert not silence.judge("drums", energy, mix_rms).keep


# ---- selection over a full stem set ----


def _stem_set() -> dict[str, np.ndarray]:
    """A piano-ballad shape: real vocals/piano/other, absent drums/bass/guitar."""
    return {
        "vocals": _tone(amp=0.5, freq=330.0),
        "drums": _noise(),
        "bass": np.zeros((2, SR * DURATION), dtype=np.float32),
        "guitar": _noise(),
        "piano": _sparse(amp=0.4, active=3.0),
        "other": _tone(amp=0.2, freq=550.0),
    }


def test_select_present_keeps_only_real_stems():
    stems = _stem_set()
    mix_rms = silence.rms_dbfs(_tone(amp=0.8))
    kept, verdicts = silence.select_present(stems, SR, mix_rms)

    assert set(kept) == {"vocals", "piano", "other"}
    assert len(verdicts) == len(stems)  # every input stem is accounted for
    assert {v.name for v in verdicts if not v.keep} == {"drums", "bass", "guitar"}


def test_select_present_returns_the_original_arrays():
    stems = _stem_set()
    kept, _ = silence.select_present(stems, SR, silence.rms_dbfs(_tone(amp=0.8)))
    assert kept["vocals"] is stems["vocals"]


def test_always_keep_pins_a_stem_that_would_be_dropped():
    stems = _stem_set()
    mix_rms = silence.rms_dbfs(_tone(amp=0.8))
    kept, verdicts = silence.select_present(
        stems, SR, mix_rms, always_keep=frozenset({"drums"})
    )
    assert "drums" in kept
    assert "bass" not in kept
    assert next(v for v in verdicts if v.name == "drums").reason.startswith("pinned")


def test_all_empty_falls_back_to_keeping_everything():
    """A degenerate mix means the measurement is untrustworthy — don't return zero stems.

    A job with no outputs at all reads as a backend failure, not as an answer.
    """
    stems = {"vocals": _noise(), "drums": _noise()}
    kept, verdicts = silence.select_present(stems, SR, silence.rms_dbfs(_tone(amp=0.9)))
    assert set(kept) == {"vocals", "drums"}
    assert all(v.keep for v in verdicts)


def test_empty_input_stays_empty():
    kept, verdicts = silence.select_present({}, SR, -14.0)
    assert kept == {}
    assert verdicts == []


def test_thresholds_are_tunable():
    stems = _stem_set()
    mix_rms = silence.rms_dbfs(_tone(amp=0.8))
    # A stricter relative floor prunes the quiet "other" stem (~-12 dB vs the mix) while
    # still keeping the vocals (~-4 dB) and the sparse piano (~-6 dB).
    kept, _ = silence.select_present(stems, SR, mix_rms, relative_floor_db=-8.0)
    assert "vocals" in kept
    assert "piano" in kept
    assert "other" not in kept
