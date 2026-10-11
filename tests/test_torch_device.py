"""Where torch models run: CUDA, then Apple's GPU (MPS), then the CPU, for
every torch model: CLIP in the workers and in the API (src/shared/torch_device.py)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.fast

torch = pytest.importorskip("torch")

from src.shared.torch_device import empty_torch_cache, torch_device  # noqa: E402


@pytest.fixture
def gpus(monkeypatch):
    """Set which GPUs torch says it has, and record cache emptying."""
    emptied: list[str] = []

    def have(cuda: bool, mps: bool) -> None:
        monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)
        monkeypatch.setattr(torch.backends.mps, "is_available", lambda: mps)
        monkeypatch.setattr(torch.cuda, "empty_cache", lambda: emptied.append("cuda"))
        monkeypatch.setattr(torch.mps, "empty_cache", lambda: emptied.append("mps"))

    have.emptied = emptied
    return have


@pytest.mark.parametrize(
    ("cuda", "mps", "want"),
    [(True, False, "cuda"), (True, True, "cuda"), (False, True, "mps"), (False, False, "cpu")],
    ids=["linux-gpu", "cuda-wins", "mac", "cpu-only"],
)
def test_cuda_then_apples_gpu_then_the_cpu(gpus, cuda, mps, want):
    gpus(cuda, mps)
    assert torch_device() == want


def test_a_torch_without_mps_is_the_cpu(gpus, monkeypatch):
    gpus(False, False)
    monkeypatch.delattr(torch.backends, "mps")
    assert torch_device() == "cpu"


@pytest.mark.parametrize(("cuda", "mps", "want"), [(True, True, ["cuda"]), (False, True, ["mps"]), (False, False, [])])
def test_the_cache_given_back_is_the_device_in_use(gpus, cuda, mps, want):
    gpus(cuda, mps)
    empty_torch_cache()
    assert gpus.emptied == want


@pytest.mark.parametrize(
    "module",
    ["src.processing.workers.embeddings.clip_provider", "src.server.embeddings.clip_provider"],
)
def test_both_clip_providers_load_on_the_chosen_device(module, monkeypatch, tmp_path):
    import importlib
    import sys

    provider_module = importlib.import_module(module)
    loaded: list[str] = []
    fake_model = SimpleNamespace(eval=lambda: None)

    def create_model_and_transforms(name, pretrained, device):
        loaded.append(device)
        return fake_model, None, object()

    monkeypatch.setitem(sys.modules, "open_clip", SimpleNamespace(create_model_and_transforms=create_model_and_transforms))
    monkeypatch.setattr("src.shared.torch_device.torch_device", lambda: "mps")
    from src.server.config import get_settings

    if module.startswith("src.server"):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))  # its model cache goes under the data dir
        get_settings.cache_clear()
    try:
        provider = provider_module.CLIPEmbeddingProvider()
        _, _, device = provider._load()
    finally:
        get_settings.cache_clear()  # no other test sees this one's data dir
    assert device == "mps"
    assert loaded == ["mps"]
