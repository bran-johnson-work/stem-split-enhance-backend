"""Audio compute for the separation engine (migrated from the POC).

- HT-Demucs (htdemucs_ft) stem separation via the demucs 4.0.1 low-level API.
- Enhancement targeting Suno artifacts: spectral denoise, de-click, reference-guided
  spectral matching, harmonic exciter, multiband saturation.

Pure compute only: no job state, no HTTP, no DB. The caller (app.worker) drives progress.
"""

import logging
import os

# Let any op MPS (Apple Silicon GPU) does not implement fall back to CPU instead of
# crashing. Must be set before torch initializes its MPS backend, hence before import.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import threading  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402

import noisereduce as nr  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
from demucs.apply import BagOfModels, apply_model  # noqa: E402
from demucs.audio import AudioFile, convert_audio  # noqa: E402
from demucs.pretrained import REMOTE_ROOT, get_model  # noqa: E402
from scipy import signal  # noqa: E402

from app.core.config import get_settings  # noqa: E402

logger = logging.getLogger(__name__)

# First run downloads two models: htdemucs_ft (bag of 4, ~324 MB) + htdemucs_6s (~80 MB).
# Used only to size the one-time download message; the real readiness signal is the load completing.
WEIGHTS_MB = 400

# One entry per model name (htdemucs_ft for vocals/drums/bass, htdemucs_6s for guitar/piano).
# Both can be resident at once: their weights are small (<0.5 GB total) and apply_model
# chunks the audio, so peak VRAM is bounded by the segment, not the sum of the weights.
_models: dict[str, tuple[object, str]] = {}
_load_lock = threading.Lock()


def _mps_available() -> bool:
    """True on Apple Silicon with a working MPS backend; False everywhere else.

    Guarded so it can't raise on platforms where torch has no ``mps`` backend.
    """
    mps = getattr(torch.backends, "mps", None)
    return bool(mps is not None and mps.is_available())


def _pick_device() -> str:
    """Resolve the compute device from ``settings.worker_device``.

    - ``auto`` (default): best available — CUDA GPU > Apple MPS > CPU.
    - ``cuda`` / ``cuda:N``: *require* that GPU. If CUDA isn't visible we raise
      rather than silently fall back to CPU — on a rented AWS GPU box a silent CPU
      fallback would run 10-50x slower with no signal, wasting the instance.
    - ``mps``: require Apple MPS (raises if unavailable).
    - ``cpu``: force CPU (works everywhere, just slower).
    """
    configured = (get_settings().worker_device or "auto").strip().lower()

    if configured in ("", "auto"):
        if torch.cuda.is_available():
            return "cuda"
        return "mps" if _mps_available() else "cpu"

    if configured.startswith("cuda"):
        if not torch.cuda.is_available():
            raise RuntimeError(
                f"WORKER_DEVICE={configured!r} requires CUDA, but torch.cuda.is_available() "
                "is False. On an AWS GPU host check: this is a GPU instance, the NVIDIA "
                "driver + nvidia-container-toolkit are installed, the container was started "
                "with `--gpus all`, and torch is a CUDA build (GPU wheels, not the cpu index)."
            )
        return configured

    if configured == "mps":
        if not _mps_available():
            raise RuntimeError("WORKER_DEVICE='mps' requested but the MPS backend is unavailable.")
        return "mps"

    if configured == "cpu":
        return "cpu"

    raise RuntimeError(
        f"Unrecognized WORKER_DEVICE={configured!r}; use one of: auto | cpu | cuda | cuda:N | mps."
    )


def weights_cached() -> bool:
    """True if every weight file for the configured model is already in the torch-hub cache.

    Best-effort and side-effect free (never downloads): it reads the bag's signatures from
    demucs' bundled remote spec and checks the checkpoints dir. Any failure (non-bag model,
    missing spec, parse error) returns False, so the caller just shows the download message.
    """
    try:
        import yaml  # transitive via demucs; only needed to read the bag spec

        name = get_settings().demucs_model
        spec = yaml.safe_load((REMOTE_ROOT / f"{name}.yaml").read_text())
        signatures = spec.get("models") or []
        if not signatures:
            return False
        checkpoints = Path(torch.hub.get_dir()) / "checkpoints"
        return all(any(checkpoints.glob(f"{sig}*.th")) for sig in signatures)
    except Exception:
        return False


def get_model_and_device(name: str | None = None):
    """Lazy-load a demucs model by name once per process; cache it keyed by name.

    demucs 4.0.1 on PyPI ships only the low-level API (no demucs.api), so separation
    uses get_model + apply_model directly. The default (htdemucs_ft) is a bag of 4 models,
    chosen for the best vocal quality the POC emphasizes; htdemucs_6s is loaded alongside
    it to add the guitar and piano stems. The lock makes each load idempotent so the
    startup pre-warm and a job can never download/load the same model twice.
    """
    name = name or get_settings().demucs_model
    cached = _models.get(name)
    if cached is not None:
        return cached

    with _load_lock:
        cached = _models.get(name)
        if cached is not None:  # another thread finished while we waited
            return cached
        device = _pick_device()
        if device.startswith("cuda"):
            # Fixed-size chunks from apply_model -> cuDNN autotuner pays off after warmup.
            torch.backends.cudnn.benchmark = True
            try:
                logger.info("model.device.cuda name=%s", torch.cuda.get_device_name(0))
            except Exception:  # pragma: no cover - purely informational
                pass
        logger.info("model.loading name=%s device=%s (first run downloads weights)", name, device)
        model = get_model(name)
        model.to(device)
        model.eval()
        _models[name] = (model, device)
        return _models[name]


def load_audio(path: Path, samplerate: int, channels: int) -> torch.Tensor:
    """Decode any input to a (channels, samples) tensor at the model's rate.

    Prefers libsndfile (soundfile): WAV/FLAC/OGG/MP3 with no system dependency. Falls
    back to demucs' AudioFile (ffmpeg) for formats libsndfile cannot read, e.g. M4A.
    """
    try:
        data, in_sr = sf.read(str(path), dtype="float32", always_2d=True)  # (frames, channels)
        wav = torch.from_numpy(data.T.copy())  # (channels, frames)
    except sf.LibsndfileError:
        logger.warning("audio.libsndfile_failed path=%s, falling back to ffmpeg", path.name)
        wav = AudioFile(str(path)).read(streams=0, samplerate=samplerate, channels=channels)
        in_sr = samplerate
    return convert_audio(wav, in_sr, samplerate, channels)


def separate_sources(
    model,
    device: str,
    wav: torch.Tensor,
    on_model_done: Callable[[int, int], None] | None = None,
) -> dict[str, np.ndarray]:
    """Run HT-Demucs on a (channels, samples) tensor; return {source: (channels, samples)}.

    Replicates demucs' own bag-of-models weighted averaging so the result is identical
    to demucs.apply.apply_model, while letting the caller observe per-model progress
    (apply_model in 4.0.1 exposes no progress callback).
    """
    ref = wav.mean(0)
    wav = (wav - ref.mean()) / (ref.std() + 1e-8)
    mix = wav[None]  # (1, channels, samples); apply_model moves chunks to device

    n_models = len(model.models) if isinstance(model, BagOfModels) else 1
    with torch.no_grad():
        if isinstance(model, BagOfModels):
            estimates = 0.0
            totals = [0.0] * len(model.sources)
            for i, (sub, weights) in enumerate(zip(model.models, model.weights, strict=False)):
                out = apply_model(sub, mix, shifts=1, split=True, overlap=0.25, device=device)
                for k, w in enumerate(weights):
                    out[:, k, :, :] *= w
                    totals[k] += w
                estimates = estimates + out
                del out
                if on_model_done:
                    on_model_done(i + 1, n_models)
            for k in range(estimates.shape[1]):
                estimates[:, k, :, :] /= totals[k]
        else:
            estimates = apply_model(model, mix, shifts=1, split=True, overlap=0.25, device=device)
            if on_model_done:
                on_model_done(1, 1)

    # Denormalize, then map demucs' source order -> numpy (channels, samples) per stem.
    rstd, rmean = float(ref.std()), float(ref.mean())
    sources = (estimates[0] * rstd + rmean).cpu().numpy()
    return {name: sources[i] for i, name in enumerate(model.sources)}


# ---- Drum component split: kick / snare / hi-hats (POC drum sub-stems) ----


def _sos_band(sr: int, low: float | None = None, high: float | None = None, order: int = 4):
    """Butterworth SOS for a low/high/band-pass, normalized to Nyquist. Low+high -> band."""
    nyq = sr / 2.0
    if low is not None and high is not None:
        return signal.butter(order, [low / nyq, high / nyq], btype="band", output="sos")
    if low is not None:
        return signal.butter(order, low / nyq, btype="high", output="sos")
    return signal.butter(order, high / nyq, btype="low", output="sos")


def _split_drums_mono(y: np.ndarray, sr: int) -> dict[str, np.ndarray]:
    """Band-split one channel of a drum stem into kick / snare / hi-hats.

    Heuristic and intentionally simple (the repo's pragmatic-DSP line, same approach as the
    drumsep project): each element is pulled from the frequency range where its energy
    concentrates. The snare combines its low-mid body with its high-frequency crack; the
    kick band is excluded from the snare so low thumps do not bleed up. Bands overlap, so
    the parts are perceptual splits, not a lossless decomposition that sums back to the input.
    """
    kick = signal.sosfilt(_sos_band(sr, high=110.0), y)  # sub / low thump
    body = signal.sosfilt(_sos_band(sr, low=160.0, high=450.0), y)  # snare fundamental
    crack = signal.sosfilt(_sos_band(sr, low=1800.0, high=6500.0), y)  # snare crack / snares
    snare = body + 0.8 * crack
    hihats = signal.sosfilt(_sos_band(sr, low=7000.0), y)  # hats / cymbal sizzle
    return {
        "kick": kick.astype(np.float32),
        "snare": snare.astype(np.float32),
        "hihats": hihats.astype(np.float32),
    }


def split_drums(drums: np.ndarray, sr: int) -> dict[str, np.ndarray]:
    """Split a separated drums stem into kick / snare / hi-hats.

    Accepts (channels, samples) or (samples,) and preserves the channel layout. Pure DSP,
    no extra model: chosen over LarsNet (CC BY-NC license, 16 kHz output) so the result is
    license-clean and stays at the track's full sample rate.
    """
    if drums.ndim == 2:
        per_channel = [
            _split_drums_mono(drums[c].astype(np.float32), sr) for c in range(drums.shape[0])
        ]
        return {
            name: np.stack([ch[name] for ch in per_channel], axis=0).astype(np.float32)
            for name in ("kick", "snare", "hihats")
        }
    return _split_drums_mono(drums.astype(np.float32), sr)


# ---- Enhancement (addresses the four POC requirements) ----


def _soft_saturate(x: np.ndarray, drive: float = 2.5) -> np.ndarray:
    """Gentle tanh saturation for harmonic density."""
    return np.tanh(x * drive) / drive


def _multiband_saturation(y: np.ndarray, sr: int) -> np.ndarray:
    """3-band split + per-band saturation (density, reduced muddiness/sparseness)."""
    nyq = sr / 2.0
    low = 250.0 / nyq
    mid = 3000.0 / nyq

    sos_low = signal.butter(4, low, btype="low", output="sos")
    sos_mid = signal.butter(4, [low, mid], btype="band", output="sos")
    sos_high = signal.butter(4, mid, btype="high", output="sos")

    low_b = signal.sosfilt(sos_low, y)
    mid_b = signal.sosfilt(sos_mid, y)
    high_b = signal.sosfilt(sos_high, y)

    # More drive on mids/highs for presence, controlled on lows.
    out = _soft_saturate(low_b, 1.8) + _soft_saturate(mid_b, 3.2) + _soft_saturate(high_b, 2.8)
    peak = np.max(np.abs(out)) or 1.0
    if peak > 0.98:
        out = out / (peak / 0.98)
    return out.astype(np.float32)


def _harmonic_exciter(y: np.ndarray, sr: int, amount: float = 0.25) -> np.ndarray:
    """Bandwidth extension / harmonic reconstruction for thin vocals or lost highs."""
    nyq = sr / 2.0
    cutoff = 4500.0 / nyq
    sos_hp = signal.butter(4, cutoff, btype="high", output="sos")
    highs = signal.sosfilt(sos_hp, y)

    excited = np.tanh(highs * 3.0) * 0.5  # nonlinear -> new harmonics
    sos_lp = signal.butter(4, cutoff * 1.6, btype="low", output="sos")
    excited = signal.sosfilt(sos_lp, excited)  # keep it musical

    out = y + amount * excited
    peak = np.max(np.abs(out)) or 1.0
    if peak > 0.98:
        out *= 0.98 / peak
    return out.astype(np.float32)


def _declick(y: np.ndarray, threshold: float = 0.08) -> np.ndarray:
    """Light spectral repair / de-click for Suno pops and gaps (crude but fast)."""
    diff = np.abs(np.diff(y, prepend=y[0]))
    mask = diff > threshold
    if not np.any(mask):
        return y
    kernel = np.ones(5, dtype=float) / 5
    smooth_mask = np.convolve(mask.astype(float), kernel, mode="same") > 0.2
    y_clean = y.copy()
    y_clean[smooth_mask] *= 0.15  # strong but localized duck (keeps energy)
    return y_clean.astype(np.float32)


def match_spectral_envelope(
    y: np.ndarray,
    reference: np.ndarray,
    sr: int,
    n_fft: int = 4096,
    max_gain_db: float = 12.0,
) -> np.ndarray:
    """Reference-guided enhancement (the POC bonus).

    Steers the long-term tonal balance of `y` toward that of `reference` (a clean
    section of the same stem). This is automatic EQ-matching: it lifts thin/lost
    bands and tames muddy/bloated ones so artifact-heavy regions inherit the tone of
    the clean part. Gains are smoothed in frequency and clamped to +/- max_gain_db so
    the match cannot introduce harsh resonances.
    """
    if reference is None or reference.size < n_fft or y.size < n_fft:
        return y

    noverlap = n_fft * 3 // 4

    def avg_mag(sig: np.ndarray) -> np.ndarray:
        _, _, spec = signal.stft(sig, fs=sr, nperseg=n_fft, noverlap=noverlap)
        return np.abs(spec).mean(axis=1) + 1e-9

    ref_mag = avg_mag(reference.astype(np.float32))
    cur_mag = avg_mag(y)

    # Per-band gain, clamped, then smoothed across frequency for a musical curve.
    gain_db = np.clip(20.0 * np.log10(ref_mag / cur_mag), -max_gain_db, max_gain_db)
    win = max(3, (len(gain_db) // 48) | 1)  # odd window ~ 1/48 of the spectrum
    kernel = np.hanning(win)
    kernel /= kernel.sum()
    gain_db = np.convolve(gain_db, kernel, mode="same")
    gain_lin = (10.0 ** (gain_db / 20.0)).astype(np.float32)

    # Apply the matching curve via STFT -> multiply -> inverse STFT.
    _, _, spec = signal.stft(y, fs=sr, nperseg=n_fft, noverlap=noverlap)
    spec *= gain_lin[:, None]
    _, out = signal.istft(spec, fs=sr, nperseg=n_fft, noverlap=noverlap)

    if len(out) >= len(y):
        out = out[: len(y)]
    else:
        out = np.pad(out, (0, len(y) - len(out)))
    return out.astype(np.float32)


def enhance_stem(
    audio: np.ndarray,
    sr: int,
    stem_name: str,
    reference: np.ndarray | None = None,
) -> np.ndarray:
    """Post-process one stem against Suno artifacts (vocals get the most help).

    1. Artifact reduction via spectral denoising (noisereduce).
    2. Spectral repair / de-click.
    3. Reference-guided spectral matching (bonus mode, when a reference is supplied).
    4. Harmonic reconstruction + bandwidth extension.
    5. Multiband saturation for density / reduced muddiness.
    """
    if audio.ndim == 2:
        # Stereo: process channels independently then stack (preserves imaging).
        # The reference is sliced per-channel so each side matches its own clean tone.
        chs = [
            enhance_stem(
                audio[c],
                sr,
                stem_name,
                reference[c] if reference is not None and reference.ndim == 2 else reference,
            )
            for c in range(audio.shape[0])
        ]
        return np.stack(chs, axis=0).astype(np.float32)

    y = audio.astype(np.float32)

    # 1. Artifact reduction / denoising (stronger on vocals/other).
    prop = 0.72 if stem_name in ("vocals", "other") else 0.45
    y = nr.reduce_noise(y=y, sr=sr, stationary=False, prop_decrease=prop)

    # 2. Light de-click / repair for clicks and gaps.
    y = _declick(y, threshold=0.07 if stem_name == "vocals" else 0.12)

    # 3. Reference-guided matching toward the clean section (bonus mode, if supplied).
    if reference is not None and getattr(reference, "size", 0) > 0:
        y = match_spectral_envelope(y, np.asarray(reference, dtype=np.float32), sr)

    # 4. Harmonic reconstruction / bandwidth extension (esp. vocals).
    if stem_name in ("vocals", "other"):
        y = _harmonic_exciter(y, sr, amount=0.32 if stem_name == "vocals" else 0.18)

    # 5. Multiband saturation (fullness, reduced sparseness/muddiness).
    y = _multiband_saturation(y, sr)

    # Safety-limit only: attenuate to avoid clipping, but never amplify. Upward make-up gain
    # would blow a near-silent stem (e.g. the vocal of an instrumental track) up into audible
    # noise, and would flatten the relative balance between stems so they no longer sum to the
    # mix. Stems are delivered at their natural separated level.
    peak = float(np.max(np.abs(y)))
    if peak > 0.99:
        y = y * (0.99 / peak)
    return y.astype(np.float32)
