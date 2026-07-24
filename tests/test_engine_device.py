"""Device resolution for the separation engine (app.engine.dsp._pick_device).

Guarded with importorskip so the lean API test environment (which installs only
requirements.txt, no torch/demucs) skips this module instead of erroring; it runs
wherever the worker deps are present.
"""

import pytest

pytest.importorskip("torch")
pytest.importorskip("demucs")

import app.engine.dsp as dsp  # noqa: E402


def _force(monkeypatch, device, *, cuda=False, mps=False):
    monkeypatch.setattr(dsp, "get_settings", lambda: type("S", (), {"worker_device": device})())
    monkeypatch.setattr(dsp.torch.cuda, "is_available", lambda: cuda)
    monkeypatch.setattr(dsp, "_mps_available", lambda: mps)


def test_auto_prefers_cuda_when_present(monkeypatch):
    _force(monkeypatch, "auto", cuda=True)
    assert dsp._pick_device() == "cuda"


def test_auto_prefers_mps_over_cpu(monkeypatch):
    _force(monkeypatch, "auto", cuda=False, mps=True)
    assert dsp._pick_device() == "mps"


def test_auto_falls_back_to_cpu(monkeypatch):
    _force(monkeypatch, "auto", cuda=False, mps=False)
    assert dsp._pick_device() == "cpu"


def test_explicit_cuda_requires_gpu(monkeypatch):
    # The whole point on a rented GPU box: fail fast, don't silently run on CPU.
    _force(monkeypatch, "cuda", cuda=False)
    with pytest.raises(RuntimeError, match="requires CUDA"):
        dsp._pick_device()


def test_explicit_cuda_index_is_honored(monkeypatch):
    _force(monkeypatch, "cuda:0", cuda=True)
    assert dsp._pick_device() == "cuda:0"


def test_cpu_forces_cpu_even_with_gpu_present(monkeypatch):
    _force(monkeypatch, "cpu", cuda=True)
    assert dsp._pick_device() == "cpu"


def test_mps_required_when_requested(monkeypatch):
    _force(monkeypatch, "mps", mps=False)
    with pytest.raises(RuntimeError, match="MPS"):
        dsp._pick_device()


def test_unknown_device_raises(monkeypatch):
    _force(monkeypatch, "bogus")
    with pytest.raises(RuntimeError, match="Unrecognized"):
        dsp._pick_device()
