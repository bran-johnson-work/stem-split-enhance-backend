"""v2 vocal separation: Mel-Band RoFormer inference.

Why this model and not the one at the top of the leaderboard: **"BS Roformer 124 bands
(2026.07, MVSep)" has no public weights.** It is a hosted service configuration, so it
cannot be put in a container at any price — only called over MVSep's API. This is the best
*downloadable* vocal model: Mel-Band RoFormer, MIT weights, ~10.98 dB vocals SDR against
the ~8.2-8.9 dB of the HT-Demucs v1 stack. See docs/stem-quality-v2.md.

The checkpoint has ``num_stems: 1`` and ``target_instrument: vocals`` — it predicts
*vocals only*. The instrumental is the residual, ``mix - vocals``, which is exactly how
the upstream inference does it and is why the instrumental SDR is so much higher than the
vocal SDR on the leaderboards: the residual inherits the vocal model's accuracy.

Hyperparameters are **read from the checkpoint's own config YAML**, never hardcoded here.
Guessing ``dim``/``depth``/``num_bands`` to match a community checkpoint is how you get a
model that loads with subtly wrong shapes, and the config is published alongside the
weights precisely so nobody has to.

Pure compute: no job state, no HTTP, no DB.
"""

import logging
import os
import threading

# Match dsp.py: let MPS fall back to CPU for unimplemented ops rather than crashing.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import torch  # noqa: E402
import yaml  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.engine import weights  # noqa: E402
from app.engine.dsp import _pick_device  # noqa: E402

logger = logging.getLogger(__name__)

CKPT_FILENAME = "melband_roformer_vocals.ckpt"
CONFIG_FILENAME = "melband_roformer_vocals.yaml"

# One resident model per process, loaded once. Mirrors dsp._models: the startup prewarm
# and the first job must never both download or build the same model.
_model: tuple[object, str, dict] | None = None
_load_lock = threading.Lock()


def _load_config(path: Path) -> dict:
    """Parse the model config YAML into plain dicts.

    Upstream uses omegaconf; the file is ordinary YAML and we only read it, so plain
    ``yaml`` avoids adding a dependency for attribute access we don't need.
    """
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    for section in ("audio", "model"):
        if section not in cfg:
            raise RuntimeError(
                f"Model config {path.name} has no '{section}' section — it is not a "
                "Music-Source-Separation-Training config. Check V2_VOCALS_CONFIG_URL."
            )
    return cfg


def _strip_state_dict(raw: dict) -> dict:
    """Normalize the many shapes a published checkpoint can arrive in.

    Community checkpoints are variously a bare ``state_dict``, a Lightning checkpoint
    wrapping one under ``state_dict``, and either form with a ``model.`` or ``module.``
    prefix from DataParallel/Lightning. Handling this here keeps the failure mode
    "checkpoint is not this architecture" instead of "unexpected key".
    """
    state = raw
    for key in ("state_dict", "model_state_dict", "model"):
        if isinstance(state, dict) and key in state and isinstance(state[key], dict):
            state = state[key]
            break

    for prefix in ("module.", "model."):
        if state and all(k.startswith(prefix) for k in state):
            state = {k[len(prefix) :]: v for k, v in state.items()}
            break
    return state


def get_vocals_model() -> tuple[object, str, dict]:
    """Load (and cache) the Mel-Band RoFormer vocal model. Returns (model, device, cfg)."""
    global _model
    if _model is not None:
        return _model

    with _load_lock:
        if _model is not None:  # another thread finished while we waited
            return _model

        settings = get_settings()
        try:
            from bs_roformer import MelBandRoformer
        except ImportError as exc:  # pragma: no cover - depends on the image build
            raise RuntimeError(
                "MelBandRoformer is unavailable: the v2 engine needs the 'bs-roformer' "
                "package, which is only installed in the v2 worker image "
                "(requirements-worker-v2.txt). This container was built for v1 — check "
                "WORKER_ENGINE_VERSIONS, it should not be claiming v2 jobs."
            ) from exc

        cache = weights.weights_dir(settings.v2_weights_dir)
        config_path = weights.ensure_file(
            settings.v2_vocals_config_url, CONFIG_FILENAME, cache_dir=cache
        )
        ckpt_path = weights.ensure_file(
            settings.v2_vocals_ckpt_url,
            CKPT_FILENAME,
            cache_dir=cache,
            sha256=settings.v2_vocals_ckpt_sha256,
        )

        cfg = _load_config(config_path)
        device = _pick_device()
        if device.startswith("cuda"):
            # Fixed-size chunks => the cuDNN autotuner pays off after the first batch.
            torch.backends.cudnn.benchmark = True

        logger.info("roformer.loading device=%s ckpt=%s", device, ckpt_path.name)
        model = MelBandRoformer(**cfg["model"])

        # weights_only=True: a checkpoint is untrusted data pulled over the network, and
        # the default pickle path would execute whatever it contains.
        raw = torch.load(str(ckpt_path), map_location="cpu", weights_only=True)
        state = _strip_state_dict(raw)
        missing, unexpected = model.load_state_dict(state, strict=False)
        if missing or unexpected:
            # Not fatal on its own (buffers legitimately differ between versions), but a
            # large count means the config and checkpoint disagree and the output will be
            # silently wrong, so it has to be visible.
            logger.warning(
                "roformer.state_dict_partial missing=%d unexpected=%d first_missing=%s",
                len(missing), len(unexpected), list(missing)[:3],
            )
        if len(missing) > len(state) // 10:
            raise RuntimeError(
                f"Checkpoint does not match the model config: {len(missing)} missing keys "
                f"out of {len(state)}. The config at V2_VOCALS_CONFIG_URL and the "
                "checkpoint at V2_VOCALS_CKPT_URL are not a matching pair."
            )

        model.to(device)
        model.eval()
        _model = (model, device, cfg)
        logger.info("roformer.ready device=%s", device)
        return _model


def _fade_window(length: int, fade: int, *, fade_in: bool, fade_out: bool) -> torch.Tensor:
    """Linear cross-fade window for one chunk.

    Chunk contributions are normalized by the summed window at the end, so the exact shape
    only affects smoothness at the seams, not correctness. The edges matter though: the
    first chunk must not fade *in* and the last must not fade *out*, or the track's own
    start and end get windowed down toward silence.
    """
    window = torch.ones(length)
    fade = max(0, min(fade, length // 2))
    if fade:
        if fade_in:
            window[:fade] = torch.linspace(0.0, 1.0, fade)
        if fade_out:
            window[-fade:] = torch.linspace(1.0, 0.0, fade)
    return window


def separate_vocals(
    model,
    device: str,
    cfg: dict,
    wav: torch.Tensor,
    on_progress=None,
) -> np.ndarray:
    """Run the vocal model over a (channels, samples) tensor; return (channels, samples).

    Overlap-add chunked inference: the model sees fixed ``chunk_size`` windows (it was
    trained on 8 s at 44.1 kHz and degrades on other lengths), stepping by
    ``chunk_size // overlap``. Each output is multiplied by a cross-fade window and
    accumulated, then divided by the summed windows — so the normalization is exact for
    any window shape and any overlap factor.

    ``on_progress(done, total)`` is called per batch so the worker can report progress.
    """
    settings = get_settings()
    chunk = int(cfg["audio"]["chunk_size"])
    overlap = max(1, int(settings.v2_overlap))
    batch_size = max(1, int(settings.v2_batch_size))

    step = max(1, chunk // overlap)
    # Cross-fade only where chunks actually overlap. At overlap=1 the hop equals the chunk,
    # so consecutive chunks share no samples and there is nothing to fade *with*: fading
    # anyway drives the window to ~0 at every chunk boundary, and since contributions are
    # normalized by the summed window, those boundary samples come out silent — a click
    # every chunk. Zero fade there gives a rectangular window, which is exactly right.
    fade = step if step < chunk else 0
    total_samples = wav.shape[-1]

    # Reflect-pad the edges by one hop so the first and last samples are covered by a
    # full-length chunk rather than a zero-padded one (zero padding makes the model
    # hallucinate a fade at the track boundaries).
    border = min(step, total_samples)
    padded = wav
    if total_samples > 2 * border and border > 0:
        padded = torch.nn.functional.pad(wav, (border, border), mode="reflect")
    else:
        border = 0
    length = padded.shape[-1]

    accum = torch.zeros_like(padded, dtype=torch.float32)
    counter = torch.zeros_like(padded, dtype=torch.float32)

    starts = list(range(0, length, step))
    total_batches = (len(starts) + batch_size - 1) // batch_size
    done_batches = 0

    with torch.no_grad():
        for batch_start in range(0, len(starts), batch_size):
            group = starts[batch_start : batch_start + batch_size]
            parts, spans = [], []
            for start in group:
                part = padded[:, start : start + chunk]
                seg = part.shape[-1]
                if seg < chunk:
                    # A short tail. Reflect-pad when there's enough signal to reflect,
                    # otherwise zero-pad — reflect requires pad < dim.
                    mode = "reflect" if seg > chunk // 2 else "constant"
                    part = torch.nn.functional.pad(part, (0, chunk - seg), mode=mode)
                parts.append(part)
                spans.append((start, seg))

            out = model(torch.stack(parts, dim=0).to(device))
            if out.ndim == 4:  # (batch, stems, channels, samples) — take the single stem
                out = out[:, 0]
            out = out.to("cpu", dtype=torch.float32)

            for idx, (start, seg) in enumerate(spans):
                window = _fade_window(
                    chunk,
                    fade,
                    fade_in=start > 0,
                    fade_out=(start + chunk) < length,
                )[:seg]
                accum[:, start : start + seg] += out[idx, :, :seg] * window
                counter[:, start : start + seg] += window

            done_batches += 1
            if on_progress:
                on_progress(done_batches, total_batches)

    # Every sample is covered by at least one un-faded chunk edge, but clamp anyway so a
    # pathological chunk/overlap combination yields silence rather than NaN.
    vocals = accum / counter.clamp(min=1e-8)
    vocals = torch.nan_to_num(vocals, nan=0.0, posinf=0.0, neginf=0.0)
    if border:
        vocals = vocals[..., border:-border]
    return vocals[..., :total_samples].numpy()


def weights_present(settings=None) -> bool:
    """True if the v2 checkpoint and config are already cached (never downloads)."""
    settings = settings or get_settings()
    cache = weights.weights_dir(settings.v2_weights_dir)
    return (cache / CKPT_FILENAME).exists() and (cache / CONFIG_FILENAME).exists()
