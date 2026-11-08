"""Separation pipeline: input file -> written stem WAVs (migrated from the POC).

This is the single source of pipeline truth, framework-free. The worker downloads the
input to a scratch dir, calls :func:`separate_and_finalize`, and uploads the resulting
files to storage. No job state, no HTTP, no DB here.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from app.core.config import get_settings
from app.engine.dsp import (
    enhance_stem,
    get_model_and_device,
    load_audio,
    separate_sources,
    split_drums,
)

logger = logging.getLogger(__name__)

# The full POC stem set, in display order: vocals, the drum kit (full + kick/snare/hi-hats),
# bass, the two instruments (guitar/piano), and the residual.
FULL_STEM_ORDER = ["vocals", "drums", "kick", "snare", "hihats", "bass", "guitar", "piano", "other"]
# Stems that get the full restoration chain. The drum sub-stems are band-split products;
# further tonal processing smears their transients, so they are only peak-normalized.
ENHANCE_STEMS = frozenset({"vocals", "drums", "bass", "guitar", "piano", "other"})
MIN_REFERENCE_SAMPLES = 4096  # enough for a meaningful average spectrum

ProgressFn = Callable[[float, str], None]


@dataclass(frozen=True)
class FinalizedStem:
    """One written stem WAV, ready for the worker to upload and record."""

    stem_name: str
    path: Path
    is_enhanced: bool
    file_size_bytes: int


def separate_all(
    input_path: Path,
    on_step: ProgressFn | None = None,
    want_instruments: bool = True,
) -> tuple[dict[str, np.ndarray], int]:
    """Separate one file into the full POC stem set as raw (channels, samples) float32 arrays.

    htdemucs_ft gives vocals / drums / bass / other; htdemucs_6s adds guitar + piano (the
    2+ instruments); the drums stem is then DSP-split into kick / snare / hi-hats. Returns
    ({stem_name: array}, samplerate). on_step(progress_0_100, label) reports progress.
    """

    def step(progress: float, label: str) -> None:
        if on_step:
            on_step(progress, label)

    step(5.0, "Separating stems (HT-Demucs ft)")
    model, device = get_model_and_device()  # htdemucs_ft
    sr = model.samplerate
    wav = load_audio(input_path, sr, model.audio_channels)

    def on_ft_done(done: int, total: int) -> None:
        step(8.0 + done / total * 40.0, f"Separating stems ({done}/{total} models)")

    base = separate_sources(model, device, wav, on_ft_done)
    stems: dict[str, np.ndarray] = {
        name: base[name] for name in ("vocals", "drums", "bass", "other") if name in base
    }

    if want_instruments:
        step(50.0, "Separating instruments (HT-Demucs 6s)")
        imodel, idevice = get_model_and_device(get_settings().instruments_model)
        # 6s runs at the same rate/channels as ft, so the decoded mix is reused.
        same = imodel.samplerate == sr and imodel.audio_channels == model.audio_channels
        iwav = wav if same else load_audio(input_path, imodel.samplerate, imodel.audio_channels)
        six = separate_sources(imodel, idevice, iwav)
        for name in ("guitar", "piano"):
            if six.get(name) is not None:
                stems[name] = six[name]

    if "drums" in stems:
        step(64.0, "Splitting drums (kick / snare / hi-hats)")
        stems.update(split_drums(stems["drums"], sr))

    return stems, sr


def _safety_limit(audio: np.ndarray, ceiling: float = 0.99) -> np.ndarray:
    """Attenuate to avoid clipping, but never amplify. Used for the drum sub-stems, which skip
    the tonal chain. Keeps each part at its natural level so the kit's balance is preserved."""
    peak = float(np.max(np.abs(audio)))
    if peak > ceiling:
        return (audio * (ceiling / peak)).astype(np.float32)
    return audio.astype(np.float32)


def finalize_stem(
    name: str,
    audio: np.ndarray,
    sr: int,
    reference: np.ndarray | None = None,
    want_enhancement: bool = True,
) -> tuple[np.ndarray, bool]:
    """Return (deliverable_audio_channel_last, is_enhanced) ready for sf.write.

    Tonal stems get the full restoration chain when ``want_enhancement`` is set; drum
    sub-stems (and everything when enhancement is off) are only peak-normalized so their
    transients are not smeared by saturation/exciter processing.
    """
    if want_enhancement and name in ENHANCE_STEMS:
        out = enhance_stem(audio, sr, name, reference=reference)
        is_enhanced = True
    else:
        out = _safety_limit(audio)
        is_enhanced = False
    out = out.T if out.ndim == 2 else out
    return out, is_enhanced


def separate_and_finalize(
    input_path: Path,
    out_dir: Path,
    on_step: ProgressFn | None = None,
    *,
    want_instruments: bool = True,
    want_enhancement: bool = True,
    ref_window: tuple[float, float] | None = None,
) -> tuple[list[FinalizedStem], int]:
    """Full pipeline for one file: separate -> enhance -> write stem WAVs into ``out_dir``.

    Returns (finalized_stems, samplerate). Each stem is written once as 24-bit PCM WAV
    (``{name}.wav``); the drum sub-stems and — when ``want_enhancement`` is off — every stem
    are delivered at their natural separated level. ``ref_window`` (start_s, end_s) enables
    the reference-guided matching against a clean section of the same track.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    def step(progress: float, label: str) -> None:
        if on_step:
            on_step(progress, label)

    raw_stems, sr = separate_all(input_path, on_step=on_step, want_instruments=want_instruments)

    ref_s = ref_e = 0
    if ref_window is not None:
        ref_s, ref_e = int(ref_window[0] * sr), int(ref_window[1] * sr)

    present = [n for n in FULL_STEM_ORDER if raw_stems.get(n) is not None]
    finalized: list[FinalizedStem] = []
    for idx, name in enumerate(present):
        stem = raw_stems[name]  # (channels, samples) float32

        # Slice this stem's clean reference window (tonal stems only), clamped to length.
        reference = None
        if ref_window is not None and want_enhancement and name in ENHANCE_STEMS:
            s, e = max(0, ref_s), min(stem.shape[-1], ref_e)
            if e - s >= MIN_REFERENCE_SAMPLES:
                reference = stem[..., s:e]

        label = f"Enhancing {name}" + (" (reference-guided)" if reference is not None else "")
        step(66.0 + idx / len(present) * 32.0, label)

        out, is_enhanced = finalize_stem(
            name, stem, sr, reference=reference, want_enhancement=want_enhancement
        )
        path = out_dir / f"{name}.wav"
        sf.write(str(path), out, sr, subtype="PCM_24")
        finalized.append(
            FinalizedStem(
                stem_name=name,
                path=path,
                is_enhanced=is_enhanced,
                file_size_bytes=path.stat().st_size,
            )
        )

    logger.info("pipeline.done stems=%s sr=%d", [f.stem_name for f in finalized], sr)
    return finalized, sr
