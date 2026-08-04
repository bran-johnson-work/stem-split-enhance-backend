"""Engine version dispatch and the v2 RoFormer chunking math.

The chunked overlap-add in ``roformer.separate_vocals`` is the part most likely to be
subtly wrong (off-by-one seams, edge windows fading the track's own start to silence,
NaN from a zero window sum) and the part hardest to notice by ear. It is exercised here
against a stub model, so the arithmetic is verified without a 600 MB checkpoint or a GPU.

Guarded with importorskip: torch is worker-only, and ``bs_roformer`` is v2-image-only.
Nothing here needs ``bs_roformer`` — the stub replaces it.
"""

import pytest

pytest.importorskip("torch")
pytest.importorskip("numpy")

import numpy as np  # noqa: E402
import torch  # noqa: E402

SR = 44100


class _IdentityModel:
    """Stub separator that returns its input unchanged.

    Makes the overlap-add math testable: if the windowing and normalization are correct,
    feeding a signal through must return that same signal. Any seam error, edge-window bug
    or normalization mistake shows up as a deviation from the input.
    """

    def __init__(self, ndim: int = 3) -> None:
        self.ndim = ndim
        self.calls = 0
        self.batch_shapes: list[tuple] = []

    def __call__(self, batch: torch.Tensor) -> torch.Tensor:
        self.calls += 1
        self.batch_shapes.append(tuple(batch.shape))
        if self.ndim == 4:  # (b, stems, ch, samples) — the num_stems > 1 output shape
            return batch.unsqueeze(1)
        return batch


def _cfg(chunk: int = 4096) -> dict:
    return {"audio": {"chunk_size": chunk, "sample_rate": SR, "num_channels": 2}}


def _signal(samples: int, channels: int = 2) -> torch.Tensor:
    """A deterministic non-trivial signal — chirp-ish, so seams are visible as error."""
    t = torch.arange(samples, dtype=torch.float32) / SR
    mono = torch.sin(2 * torch.pi * (200 + 400 * t) * t) * 0.6
    return torch.stack([mono, mono * 0.8][:channels], dim=0)


# ---- window shape ----


def test_fade_window_edges():
    from app.engine import roformer

    w = roformer._fade_window(100, 10, fade_in=True, fade_out=True)
    assert w[0] == pytest.approx(0.0)
    assert w[-1] == pytest.approx(0.0)
    assert w[50] == pytest.approx(1.0)


def test_fade_window_can_skip_each_edge():
    from app.engine import roformer

    first = roformer._fade_window(100, 10, fade_in=False, fade_out=True)
    assert first[0] == pytest.approx(1.0)  # track start must not be faded down
    last = roformer._fade_window(100, 10, fade_in=True, fade_out=False)
    assert last[-1] == pytest.approx(1.0)  # nor the track end


def test_fade_window_clamps_oversized_fade():
    from app.engine import roformer

    w = roformer._fade_window(10, 999, fade_in=True, fade_out=True)
    assert len(w) == 10
    assert torch.all(w >= 0)


# ---- overlap-add reconstruction ----


@pytest.mark.parametrize("overlap", [1, 2, 4])
def test_reconstruction_is_near_identity(monkeypatch, overlap):
    """An identity model must round-trip the signal for every overlap factor."""
    from app.engine import roformer

    monkeypatch.setattr(
        roformer,
        "get_settings",
        lambda: type("S", (), {"v2_overlap": overlap, "v2_batch_size": 2})(),
    )
    wav = _signal(20000)
    out = roformer.separate_vocals(_IdentityModel(), "cpu", _cfg(), wav)

    assert out.shape == tuple(wav.shape)
    np.testing.assert_allclose(out, wav.numpy(), atol=1e-4)


def test_track_edges_are_not_faded(monkeypatch):
    """The regression the edge-window handling exists to prevent.

    If the first chunk faded in and the last faded out, the track's own start and end
    would be attenuated toward silence — audible, and invisible in an average-error check.
    """
    from app.engine import roformer

    monkeypatch.setattr(
        roformer, "get_settings", lambda: type("S", (), {"v2_overlap": 2, "v2_batch_size": 4})()
    )
    wav = torch.ones((2, 12000), dtype=torch.float32)
    out = roformer.separate_vocals(_IdentityModel(), "cpu", _cfg(), wav)

    assert out[:, :64] == pytest.approx(np.ones((2, 64)), abs=1e-4)
    assert out[:, -64:] == pytest.approx(np.ones((2, 64)), abs=1e-4)


def test_output_has_no_nan(monkeypatch):
    """A zero window sum anywhere would surface as NaN in the delivered WAV."""
    from app.engine import roformer

    monkeypatch.setattr(
        roformer, "get_settings", lambda: type("S", (), {"v2_overlap": 2, "v2_batch_size": 1})()
    )
    out = roformer.separate_vocals(_IdentityModel(), "cpu", _cfg(), _signal(9000))
    assert np.isfinite(out).all()


def test_signal_shorter_than_one_chunk(monkeypatch):
    """A clip shorter than the model's 8 s window still has to come back whole."""
    from app.engine import roformer

    monkeypatch.setattr(
        roformer, "get_settings", lambda: type("S", (), {"v2_overlap": 2, "v2_batch_size": 4})()
    )
    wav = _signal(1000)
    out = roformer.separate_vocals(_IdentityModel(), "cpu", _cfg(chunk=4096), wav)
    assert out.shape == tuple(wav.shape)
    assert np.isfinite(out).all()


def test_four_dim_model_output_is_handled(monkeypatch):
    """num_stems>1 checkpoints emit (b, stems, ch, samples); take the single stem."""
    from app.engine import roformer

    monkeypatch.setattr(
        roformer, "get_settings", lambda: type("S", (), {"v2_overlap": 2, "v2_batch_size": 2})()
    )
    wav = _signal(9000)
    out = roformer.separate_vocals(_IdentityModel(ndim=4), "cpu", _cfg(), wav)
    np.testing.assert_allclose(out, wav.numpy(), atol=1e-4)


def test_batch_size_is_respected(monkeypatch):
    from app.engine import roformer

    monkeypatch.setattr(
        roformer, "get_settings", lambda: type("S", (), {"v2_overlap": 1, "v2_batch_size": 3})()
    )
    model = _IdentityModel()
    roformer.separate_vocals(model, "cpu", _cfg(chunk=4096), _signal(40960))
    assert max(shape[0] for shape in model.batch_shapes) <= 3


def test_progress_is_reported(monkeypatch):
    from app.engine import roformer

    monkeypatch.setattr(
        roformer, "get_settings", lambda: type("S", (), {"v2_overlap": 2, "v2_batch_size": 2})()
    )
    seen: list[tuple[int, int]] = []
    roformer.separate_vocals(
        _IdentityModel(),
        "cpu",
        _cfg(),
        _signal(30000),
        on_progress=lambda d, t: seen.append((d, t)),
    )
    assert seen
    assert seen[-1][0] == seen[-1][1]  # ends at 100%


# ---- checkpoint loading robustness ----


@pytest.mark.parametrize(
    "raw",
    [
        {"layer.weight": 1},
        {"state_dict": {"layer.weight": 1}},
        {"state_dict": {"model.layer.weight": 1}},
        {"module.layer.weight": 1},
    ],
)
def test_state_dict_shapes_are_normalized(raw):
    """Community checkpoints ship in several wrappings; all must reduce to bare keys."""
    from app.engine.roformer import _strip_state_dict

    assert _strip_state_dict(raw) == {"layer.weight": 1}


def test_config_missing_section_raises():
    import tempfile
    from pathlib import Path

    from app.engine.roformer import _load_config

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bad.yaml"
        path.write_text("audio:\n  chunk_size: 10\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="no 'model' section"):
            _load_config(path)


def test_missing_bs_roformer_gives_an_actionable_error(monkeypatch):
    """The v1-image-claimed-a-v2-job failure mode.

    A v1 container has no ``bs-roformer``, so the error has to name the real cause
    (WORKER_ENGINE_VERSIONS is letting it claim work it cannot run) rather than surfacing a
    bare ImportError from deep in the loader.
    """
    import builtins

    from app.engine import roformer

    monkeypatch.setattr(roformer, "_model", None)
    real_import = builtins.__import__

    def _no_bs_roformer(name, *args, **kwargs):
        if name == "bs_roformer":
            raise ImportError("No module named 'bs_roformer'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_bs_roformer)
    with pytest.raises(RuntimeError, match="WORKER_ENGINE_VERSIONS"):
        roformer.get_vocals_model()


# ---- version dispatch ----


def test_unknown_engine_version_raises(tmp_path):
    from app.engine import pipeline

    with pytest.raises(RuntimeError, match="Unknown engine_version"):
        pipeline.separate_all(tmp_path / "x.wav", engine_version="v99")


def test_both_versions_are_registered():
    from app.engine import pipeline

    assert set(pipeline.SEPARATORS) == {"v1", "v2"}


def test_engine_config_snapshot_records_version_specific_dials():
    from app.engine.pipeline import engine_config_snapshot

    v1 = engine_config_snapshot("v1")
    assert v1["engine_version"] == "v1"
    assert "shifts" in v1  # the v1 quality dial
    assert "vocals_model" not in v1

    v2 = engine_config_snapshot("v2")
    assert v2["vocals_model"] == "mel_band_roformer"
    assert "recompute_other" in v2
    # Without the thresholds recorded, a stem set from an old run is uninterpretable.
    assert "stem_filter" in v2
