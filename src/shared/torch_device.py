"""Where torch models run on this machine: an NVIDIA GPU, else Apple's GPU,
else the CPU.

The one choice for every torch model (CLIP in the workers and in the API).
On Linux that's CUDA when there's one and the CPU otherwise, as before: MPS
exists only on a Mac. On a Mac, CLIP's vectors on MPS match the CPU's
(cosine 1.000000, ViT-B-32) and it's a little faster, which also leaves the
CPU to Whisper and ffmpeg. Faces choose through ONNX Runtime
(insightface_provider: CUDA, then CoreML, then the CPU) and Whisper through
CTranslate2, which has no Apple GPU.
"""

from __future__ import annotations


def _mps_available(torch) -> bool:
    mps = getattr(torch.backends, "mps", None)
    return bool(mps is not None and mps.is_available())


def torch_device() -> str:
    """"cuda", "mps" or "cpu", in that order of preference."""
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if _mps_available(torch):
        return "mps"
    return "cpu"


def empty_torch_cache() -> None:
    """Give back the GPU memory torch keeps cached, once a model is dropped."""
    import torch

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    elif _mps_available(torch):
        torch.mps.empty_cache()
