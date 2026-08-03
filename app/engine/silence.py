"""Content detection for separated stems — decide which stems actually carry audio.

A separation model always emits every source it was trained on. Ask ``htdemucs_6s``
for guitar on a solo-piano recording and it does not return an empty file: it returns
a low-level residual of bleed, noise and reverb tails. Delivering that as a "guitar
stem" is misleading — the user uploaded a piano ballad and got nine files, six of
which are inaudible junk. This module measures each stem's energy and reports which
ones are real, so the pipeline can drop the rest before they are ever written.

The decision needs two thresholds, because either one alone fails:

* An **absolute** peak floor catches true silence but is defeated by mastering level:
  the same performance mastered 12 dB quieter would start failing the gate.
* A **relative** floor (stem energy vs. the mix it came from) is level-independent but
  would keep a loud-ish bleed residual on a very quiet track.

A stem is kept only when it clears both. The relative measure uses each stem's
*loudest window* rather than its overall RMS: an instrument playing only in the bridge
has very low average energy across a 4-minute track but is unmistakably present, and
an overall-RMS gate would throw it away.

numpy only — no torch, no scipy — so this imports and tests in the lean API
environment as well as on the worker.
"""

import logging
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

# Reported instead of -inf for digital silence, so the value stays sortable/formattable.
DBFS_FLOOR = -160.0

# Defaults, mirrored by the Settings fields that override them. Deliberately permissive:
# wrongly dropping a stem the user can hear is a much worse failure than shipping one
# extra near-silent file, so the floors sit well below anything musically audible.
DEFAULT_PEAK_FLOOR_DBFS = -50.0
DEFAULT_RELATIVE_FLOOR_DB = -38.0
DEFAULT_WINDOW_SECONDS = 1.0


@dataclass(frozen=True)
class StemEnergy:
    """Loudness measurements for one stem, all in dBFS (0 dBFS = full scale)."""

    peak_dbfs: float
    rms_dbfs: float
    loudest_window_dbfs: float


@dataclass(frozen=True)
class StemVerdict:
    """Why one stem was kept or dropped — carried out to the logs."""

    name: str
    keep: bool
    reason: str
    energy: StemEnergy
    relative_db: float


def dbfs(amplitude: float) -> float:
    """Convert a linear amplitude (1.0 = full scale) to dBFS, flooring digital silence."""
    if amplitude <= 0.0:
        return DBFS_FLOOR
    return max(DBFS_FLOOR, 20.0 * float(np.log10(amplitude)))


def to_mono(audio: np.ndarray) -> np.ndarray:
    """Collapse (channels, samples) to (samples,); pass a 1-D array through.

    Averaging channels is what we want here even though it can cancel a hard-panned
    out-of-phase signal: such a signal is also inaudible in the mono fold-down the
    listener may well hear, and the peak measurement is taken on the full array anyway.
    """
    arr = np.asarray(audio)
    if arr.ndim == 2:
        return arr.mean(axis=0)
    return arr.reshape(-1)


def rms_dbfs(audio: np.ndarray) -> float:
    """Overall RMS level of a stem (or the mix) in dBFS."""
    mono = to_mono(audio)
    if mono.size == 0:
        return DBFS_FLOOR
    return dbfs(float(np.sqrt(np.mean(mono.astype(np.float64) ** 2))))


def loudest_window_dbfs(
    audio: np.ndarray, sr: int, window_seconds: float = DEFAULT_WINDOW_SECONDS
) -> float:
    """RMS of the loudest ``window_seconds`` block, in dBFS.

    This is the measurement that makes sparse-but-real stems survive the gate. Windows
    do not overlap, except that the final ``window`` samples are always evaluated as one
    block too — otherwise content confined to a trailing partial window would be missed.
    """
    mono = to_mono(audio).astype(np.float64)
    if mono.size == 0:
        return DBFS_FLOOR

    window = max(1, int(round(sr * window_seconds)))
    if mono.size <= window:
        return dbfs(float(np.sqrt(np.mean(mono**2))))

    count = mono.size // window
    frames = mono[: count * window].reshape(count, window)
    peak_rms = float(np.sqrt(np.mean(frames**2, axis=1)).max())
    if mono.size % window:
        tail = mono[-window:]
        peak_rms = max(peak_rms, float(np.sqrt(np.mean(tail**2))))
    return dbfs(peak_rms)


def measure(
    audio: np.ndarray, sr: int, window_seconds: float = DEFAULT_WINDOW_SECONDS
) -> StemEnergy:
    """Measure peak, overall RMS and loudest-window RMS for one stem."""
    arr = np.asarray(audio)
    peak = float(np.max(np.abs(arr))) if arr.size else 0.0
    return StemEnergy(
        peak_dbfs=dbfs(peak),
        rms_dbfs=rms_dbfs(arr),
        loudest_window_dbfs=loudest_window_dbfs(arr, sr, window_seconds),
    )


def judge(
    name: str,
    energy: StemEnergy,
    mix_rms_dbfs: float,
    *,
    peak_floor_dbfs: float = DEFAULT_PEAK_FLOOR_DBFS,
    relative_floor_db: float = DEFAULT_RELATIVE_FLOOR_DB,
) -> StemVerdict:
    """Decide whether one measured stem carries real content.

    Kept only when it clears *both* floors: an absolute peak floor (catches silence and
    near-silence outright) and its loudest window sitting no more than
    ``relative_floor_db`` below the mix's overall level (catches a residual that is
    non-trivial in absolute terms but negligible in the context of its own track).
    """
    relative_db = energy.loudest_window_dbfs - mix_rms_dbfs

    if energy.peak_dbfs < peak_floor_dbfs:
        reason = f"peak {energy.peak_dbfs:.1f} dBFS below floor {peak_floor_dbfs:.1f}"
        return StemVerdict(name, False, reason, energy, relative_db)
    if relative_db < relative_floor_db:
        reason = (
            f"loudest {energy.loudest_window_dbfs:.1f} dBFS is {relative_db:.1f} dB "
            f"below the mix (floor {relative_floor_db:.1f})"
        )
        return StemVerdict(name, False, reason, energy, relative_db)

    reason = f"peak {energy.peak_dbfs:.1f} dBFS, {relative_db:.1f} dB vs mix"
    return StemVerdict(name, True, reason, energy, relative_db)


def select_present(
    stems: dict[str, np.ndarray],
    sr: int,
    mix_rms_dbfs: float,
    *,
    peak_floor_dbfs: float = DEFAULT_PEAK_FLOOR_DBFS,
    relative_floor_db: float = DEFAULT_RELATIVE_FLOOR_DB,
    window_seconds: float = DEFAULT_WINDOW_SECONDS,
    always_keep: frozenset[str] = frozenset(),
) -> tuple[dict[str, np.ndarray], list[StemVerdict]]:
    """Filter ``stems`` down to those carrying real content.

    Returns ``(kept_stems, verdicts)``; ``verdicts`` covers every input stem, in input
    order, so the caller can log exactly what was dropped and why. Names in
    ``always_keep`` are never dropped (an operational escape hatch for pinning a stem
    the product contract requires).

    If the gate would reject *every* stem, nothing is dropped. An all-empty verdict
    means the mix reference is degenerate — a silent or corrupt upload — so the
    measurement is not trustworthy enough to return an empty result, and a job with
    zero outputs looks like a backend failure rather than an answer.
    """
    verdicts: list[StemVerdict] = []
    for name, audio in stems.items():
        energy = measure(audio, sr, window_seconds)
        verdict = judge(
            name,
            energy,
            mix_rms_dbfs,
            peak_floor_dbfs=peak_floor_dbfs,
            relative_floor_db=relative_floor_db,
        )
        if not verdict.keep and name in always_keep:
            verdict = StemVerdict(
                name, True, f"pinned ({verdict.reason})", energy, verdict.relative_db
            )
        verdicts.append(verdict)

    if stems and not any(v.keep for v in verdicts):
        logger.warning(
            "silence.all_stems_empty mix_rms=%.1f dBFS — keeping every stem; "
            "the input is probably silent or corrupt",
            mix_rms_dbfs,
        )
        return dict(stems), [
            StemVerdict(
                v.name, True, f"kept (all stems failed: {v.reason})", v.energy, v.relative_db
            )
            for v in verdicts
        ]

    kept = {v.name: stems[v.name] for v in verdicts if v.keep}
    return kept, verdicts
