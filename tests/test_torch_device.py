"""Where torch models run: CUDA, then Apple's GPU (MPS), then the CPU, for
every torch model: CLIP in the workers and in the API (src/shared/torch_device.py).
On Apple's GPU torch's CPU work gets one thread, in every thread that does
it: libomp's thread team segfaulted the Mac's scheduler while CLIP was built."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
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
    """And give torch one CPU thread on Apple's GPU: where it's built, and in
    each thread that embeds with it."""
    import importlib

    provider_module = importlib.import_module(module)
    loaded: list[str] = []
    threads_for: list[str] = []
    fake_tensor = SimpleNamespace(unsqueeze=lambda _dim: fake_tensor, to=lambda _device: fake_tensor)
    fake_model = SimpleNamespace(eval=lambda: None, encode_image=lambda _tensor: torch.ones(1, 4))

    def create_model_and_transforms(name, pretrained, device):
        loaded.append(device)
        return fake_model, None, lambda _image: fake_tensor

    monkeypatch.setitem(sys.modules, "open_clip", SimpleNamespace(create_model_and_transforms=create_model_and_transforms))
    monkeypatch.setattr("src.shared.torch_device.torch_device", lambda: "mps")
    monkeypatch.setattr("src.shared.torch_device.cpu_threads_for", threads_for.append)
    from src.server.config import get_settings

    if module.startswith("src.server"):
        monkeypatch.setenv("DATA_DIR", str(tmp_path))  # its model cache goes under the data dir
        get_settings.cache_clear()
    try:
        provider = provider_module.CLIPEmbeddingProvider()
        _, _, device = provider._load()
        assert threads_for == ["mps"], "before it's built"
        vector = provider.embed_image(object())
    finally:
        get_settings.cache_clear()  # no other test sees this one's data dir
    assert device == "mps"
    assert loaded == ["mps"]
    assert threads_for == ["mps", "mps"], "and before each embed"
    assert vector == pytest.approx([0.5] * 4)


_THREADS = textwrap.dedent("""
    import sys, threading
    sys.path.insert(0, sys.argv[1])
    import torch
    from src.shared.torch_device import cpu_threads_for

    def threads():
        seen = []
        thread = threading.Thread(target=lambda: seen.append(torch.get_num_threads()))
        thread.start()
        thread.join()
        return seen[0]

    many = torch.get_num_threads()
    used = {}
    def a_thread_that_used_torch():
        torch.ones(64, 64) * 2
        cpu_threads_for(sys.argv[2])
        used["it"] = torch.get_num_threads()
    worker = threading.Thread(target=a_thread_that_used_torch)
    worker.start()
    worker.join()
    print(many, used["it"], threads())
""")


@pytest.mark.parametrize(("device", "one"), [("mps", True), ("cuda", False), ("cpu", False)])
def test_on_apples_gpu_torch_s_cpu_work_is_one_thread(device, one):
    """In a process of its own: torch's thread count is the whole process's."""
    out = subprocess.run([sys.executable, "-c", _THREADS, str(Path(__file__).resolve().parents[1]), device],
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    many, in_the_thread, in_a_new_thread = map(int, out.stdout.split())
    if many == 1:
        pytest.skip("this machine gives torch one thread anyway")
    if one:
        assert (in_the_thread, in_a_new_thread) == (1, 1), "in the thread that called it, and every new one"
    else:
        assert (in_the_thread, in_a_new_thread) == (many, many), "torch's own choice elsewhere"
