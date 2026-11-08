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

from app.core.config import Settings, get_settings
from app.engine import silence
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


def _gate_options(settings: Settings) -> dict | None:
    """Kwargs for :func:`silence.select_present`, or None when the content gate is off."""
    if not settings.stem_filter_enabled:
        return None
    return {
        "peak_floor_dbfs": settings.stem_silence_peak_dbfs,
        "relative_floor_db": settings.stem_silence_relative_rms_db,
        "window_seconds": settings.stem_silence_window_seconds,
        "always_keep": settings.stem_filter_always_keep_set,
    }


def _log_verdicts(verdicts: list[silence.StemVerdict]) -> None:
    """Record why each stem was kept or dropped — the audit trail for a missing stem.

    Logged at INFO for kept stems and WARNING for dropped ones: "where did my drums go"
    is the one support question this feature creates, and it has to be answerable from
    the worker log alone.
    """
    for v in verdicts:
        if v.keep:
            logger.info("silence.keep stem=%s %s", v.name, v.reason)
        else:
            logger.warning("silence.drop stem=%s %s", v.name, v.reason)


@dataclass(frozen=True)
class FinalizedStem:
    """One written stem WAV, ready for the worker to upload and record."""

    stem_name: str
    path: Path
    is_enhanced: bool
    file_size_bytes: int


def _separate_v1(
    input_path: Path, step: ProgressFn, want_instruments: bool
) -> tuple[dict[str, np.ndarray], int, "np.ndarray"]:
    """v1: HT-Demucs only. htdemucs_ft for vocals/drums/bass/other, htdemucs_6s for
    guitar/piano. Returns (stems, samplerate, decoded_mix)."""
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

    return stems, sr, wav.detach().cpu().numpy()


def _separate_v2(
    input_path: Path, step: ProgressFn, want_instruments: bool
) -> tuple[dict[str, np.ndarray], int, "np.ndarray"]:
    """v2: Mel-Band RoFormer vocals + HT-Demucs for everything else.

    The vocal model is where nearly all the quality gain is (~10.98 dB vs ~8.2-8.9 dB
    SDR), and no open model beats Demucs on bass/drums by a margin worth a second heavy
    pass — so Demucs keeps those. See docs/stem-quality-v2.md for why this and not the
    4-model ensemble.

    With ``V2_RECOMPUTE_OTHER`` on, ``other`` is rebuilt as
    ``mix - roformer_vocals - demucs_drums - demucs_bass`` rather than taken from Demucs.
    Demucs' own ``other`` carries its vocal-separation error as bleed, which is what made
    it the worst v1 stem; subtracting the much cleaner RoFormer vocal removes it.
    """
    from app.engine import roformer

    step(4.0, "Separating vocals (Mel-Band RoFormer)")
    rmodel, rdevice, rcfg = roformer.get_vocals_model()
    sr = int(rcfg["audio"]["sample_rate"])
    channels = int(rcfg["audio"].get("num_channels", 2))
    wav = load_audio(input_path, sr, channels)

    def on_chunk(done: int, total: int) -> None:
        step(6.0 + done / max(1, total) * 34.0, f"Separating vocals ({done}/{total})")

    vocals = roformer.separate_vocals(rmodel, rdevice, rcfg, wav, on_progress=on_chunk)
    stems: dict[str, np.ndarray] = {"vocals": vocals}

    step(42.0, "Separating rhythm section (HT-Demucs ft)")
    dmodel, ddevice = get_model_and_device()
    # Demucs runs at 44.1 kHz stereo, the same as the RoFormer config, so the decoded mix
    # is reused. If a future checkpoint changes either, re-decode rather than resample.
    if dmodel.samplerate == sr and dmodel.audio_channels == channels:
        dwav = wav
    else:
        dwav = load_audio(input_path, dmodel.samplerate, dmodel.audio_channels)

    def on_ft_done(done: int, total: int) -> None:
        step(44.0 + done / total * 20.0, f"Separating rhythm section ({done}/{total} models)")

    base = separate_sources(dmodel, ddevice, dwav, on_ft_done)
    for name in ("drums", "bass"):
        if base.get(name) is not None:
            stems[name] = base[name]

    mix = wav.detach().cpu().numpy()
    if get_settings().v2_recompute_other:
        residual = mix.copy()
        for name in ("vocals", "drums", "bass"):
            part = stems.get(name)
            if part is not None:
                n = min(residual.shape[-1], part.shape[-1])
                residual[..., :n] -= part[..., :n]
        stems["other"] = residual.astype(np.float32)
    elif base.get("other") is not None:
        stems["other"] = base["other"]

    if want_instruments:
        step(66.0, "Separating instruments (HT-Demucs 6s)")
        imodel, idevice = get_model_and_device(get_settings().instruments_model)
        same = imodel.samplerate == sr and imodel.audio_channels == channels
        iwav = wav if same else load_audio(input_path, imodel.samplerate, imodel.audio_channels)
        six = separate_sources(imodel, idevice, iwav)
        for name in ("guitar", "piano"):
            if six.get(name) is not None:
                stems[name] = six[name]

    return stems, sr, mix


#: Engine versions this build can run. ``separate_all`` rejects anything else rather than
#: silently falling back to v1 and mislabelling the output as the version that was asked for.
SEPARATORS = {"v1": _separate_v1, "v2": _separate_v2}


def separate_all(
    input_path: Path,
    on_step: ProgressFn | None = None,
    want_instruments: bool = True,
    engine_version: str = "v1",
) -> tuple[dict[str, np.ndarray], int]:
    """Separate one file into the stem set as raw (channels, samples) float32 arrays.

    ``engine_version`` selects the model stack: ``v1`` = HT-Demucs (as deployed today),
    ``v2`` = Mel-Band RoFormer vocals + HT-Demucs rhythm section. Returns
    ({stem_name: array}, samplerate). on_step(progress_0_100, label) reports progress.

    Everything after separation is shared by both versions: the content gate, the drum
    split, enhancement and writing. Stems the source track does not actually contain are
    dropped here rather than written as near-silent files (see :mod:`app.engine.silence`),
    so the returned dict is a subset of the catalogue whose size varies per track. The drum
    split is skipped entirely when the drums stem itself is judged empty — its band-split
    products would be nothing but filtered noise, and skipping saves the work.
    """

    def step(progress: float, label: str) -> None:
        if on_step:
            on_step(progress, label)

    version = (engine_version or "v1").strip().lower()
    separator = SEPARATORS.get(version)
    if separator is None:
        raise RuntimeError(
            f"Unknown engine_version {engine_version!r}; this build supports "
            f"{sorted(SEPARATORS)}. A job stamped with a version this image cannot run "
            "means WORKER_ENGINE_VERSIONS is letting it claim the wrong work."
        )

    stems, sr, mix = separator(input_path, step, want_instruments)

    # Drop the stems this track doesn't actually contain, before anything is enhanced or
    # written. The mix's own level is the reference the relative floor is measured against.
    gate = _gate_options(get_settings())
    mix_rms = 0.0
    if gate is not None:
        step(72.0, "Checking stems for audio content")
        mix_rms = silence.rms_dbfs(mix)
        stems, verdicts = silence.select_present(stems, sr, mix_rms, **gate)
        _log_verdicts(verdicts)

    if "drums" in stems:
        step(74.0, "Splitting drums (kick / snare / hi-hats)")
        subs = split_drums(stems["drums"], sr)
        if gate is not None:
            # Gated independently of the parent: a kit with no cymbals yields a real
            # kick and snare but an empty hi-hats band.
            subs, sub_verdicts = silence.select_present(subs, sr, mix_rms, **gate)
            _log_verdicts(sub_verdicts)
        stems.update(subs)

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
    level: str = "full",
) -> tuple[np.ndarray, bool]:
    """Return (deliverable_audio_channel_last, is_enhanced) ready for sf.write.

    Tonal stems get the restoration chain (at intensity ``level``: "full" or "light") when
    ``want_enhancement`` is set; drum sub-stems (and everything when enhancement is off) are
    only peak-normalized so their transients are not smeared by saturation/exciter processing.
    """
    if want_enhancement and name in ENHANCE_STEMS:
        out = enhance_stem(audio, sr, name, reference=reference, level=level)
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
    engine_version: str = "v1",
) -> tuple[list[FinalizedStem], int]:
    """Full pipeline for one file: separate -> enhance -> write stem WAVs into ``out_dir``.

    Returns (finalized_stems, samplerate). Each stem is written once as 24-bit PCM WAV
    (``{name}.wav``); the drum sub-stems and — when ``want_enhancement`` is off — every stem
    are delivered at their natural separated level. ``ref_window`` (start_s, end_s) enables
    the reference-guided matching against a clean section of the same track.
    ``engine_version`` picks the model stack (see :func:`separate_all`).
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    # Resolve the restoration intensity from Settings. "off" (or want_enhancement=False)
    # short-circuits the whole chain; "light"/"full" pick how aggressive enhance_stem is.
    level = (get_settings().enhancement_level or "full").strip().lower()
    if level not in ("full", "light", "off"):
        level = "full"
    if level == "off":
        want_enhancement = False

    def step(progress: float, label: str) -> None:
        if on_step:
            on_step(progress, label)

    raw_stems, sr = separate_all(
        input_path,
        on_step=on_step,
        want_instruments=want_instruments,
        engine_version=engine_version,
    )

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
        step(78.0 + idx / len(present) * 20.0, label)

        out, is_enhanced = finalize_stem(
            name, stem, sr, reference=reference, want_enhancement=want_enhancement, level=level
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

    logger.info(
        "pipeline.done version=%s stems=%s sr=%d",
        engine_version, [f.stem_name for f in finalized], sr,
    )
    return finalized, sr


def engine_config_snapshot(engine_version: str) -> dict:
    """The settings this run used, for ``processing_jobs.engine_config``.

    Recorded so an A/B result stays interpretable: a v2 stem rendered at overlap 2 is not
    the same artifact as one at overlap 4, and without this there is no way to tell them
    apart after the fact.
    """
    s = get_settings()
    snapshot: dict = {
        "engine_version": engine_version,
        "enhancement_level": s.enhancement_level if s.enable_enhancement else "off",
        "instruments": s.enable_instruments,
        "stem_filter": {
            "enabled": s.stem_filter_enabled,
            "peak_dbfs": s.stem_silence_peak_dbfs,
            "relative_db": s.stem_silence_relative_rms_db,
            "window_s": s.stem_silence_window_seconds,
        },
    }
    if engine_version == "v2":
        snapshot["vocals_model"] = "mel_band_roformer"
        snapshot["vocals_ckpt_sha256"] = s.v2_vocals_ckpt_sha256 or None
        snapshot["overlap"] = s.v2_overlap
        snapshot["batch_size"] = s.v2_batch_size
        snapshot["recompute_other"] = s.v2_recompute_other
        snapshot["rhythm_model"] = s.demucs_model
    else:
        snapshot["model"] = s.demucs_model
        snapshot["shifts"] = s.demucs_shifts
        snapshot["overlap"] = s.demucs_overlap
    return snapshot
