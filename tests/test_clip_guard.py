"""The CLIP job's machines (ADR-022 phase 2), as the scheduler uses them:
the built-in machine (the scheduler's Models, given stand-in factories) and
a Lumiverb AI worker served over real HTTP (src/server/ai_worker/), asked
by the code that asks every AI machine. Vectors from either, the same model;
a worker that's down hands its work to the built-in machine; an image
neither can read is the clip's; no machine left waits, uncharged."""

from __future__ import annotations

import io
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
import uvicorn
from PIL import Image

from src.processing.ai_pool import Machine, MachinePool
from src.processing.clip_guard import ClipGuard, built_in_clip
from src.processing.workers.embeddings.base import EmbeddingError
from src.server.ai_worker.app import make_app
from src.server.scheduler.models import Models

pytestmark = pytest.mark.fast
KEY = "clip-guard-key"


class FakeClip:
    model_id = "clip"

    def __init__(self, model: str, pretrained: str, *, scale: float = 1.0) -> None:
        self.model_version = f"{model}-{pretrained}"
        self.calls = 0
        self._scale = scale

    def embed_image(self, img) -> list[float]:
        self.calls += 1
        r, g, b = img.getpixel((0, 0))
        return [self._scale * r / 255, self._scale * g / 255, self._scale * b / 255]


class Client:
    """The scheduler's API client, as far as the machines' checks go: what each said is reported."""

    def __init__(self) -> None:
        self.reported: list[dict] = []

    def post(self, path: str, json: dict | None = None, **_kw):
        self.reported.append({"path": path, **(json or {})})


def _png(color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buf, format="PNG")
    return buf.getvalue()


def _models(made: list) -> Models:
    def clip_factory(model, pretrained):
        made.append(FakeClip(model, pretrained))
        return made[-1]

    return Models(clip_factory=clip_factory, face_factory=lambda: None)


@contextmanager
def _worker(made: list) -> Iterator[str]:
    """A Lumiverb AI worker on a free local port: its URL as Settings › AI keeps it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make_app(_models(made), KEY), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.025)
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.should_exit = True
        thread.join(60)


@contextmanager
def _machines(*machines: Machine, model: str = "ViT-B-32") -> Iterator[None]:
    """The account's CLIP job: its model, done by these machines."""
    prior = MachinePool.load

    def load(pool) -> None:
        if pool.job != "clip":
            return prior(pool)
        pool.model = model
        pool.machines = list(machines)

    with patch("src.processing.ai_pool.MachinePool.load", load):
        yield


def _built_in(at_once: int = 1) -> Machine:
    return Machine("aim_self", "Built in", "", None, at_once, built_in=True)


def test_the_built_in_machine_makes_vectors_as_the_scheduler_always_has() -> None:
    made: list = []
    with _machines(_built_in()):
        guard = ClipGuard(Client())
        assert guard.check() and not guard.down
        embedder = guard.embedder(_models(made))
        got = embedder.embed(_png((0, 255, 0)))
    assert got == {"vector": [0.0, 1.0, 0.0], "model_id": "clip", "model_version": "ViT-B-32-openai"}
    assert made[0].calls == 1


def test_a_workers_vectors_are_the_same_models() -> None:
    worker_made: list = []
    with _worker(worker_made) as url, _machines(Machine("aim_gpu", "GPU box", url, KEY, 2)):
        guard = ClipGuard(Client())
        assert guard.check(), guard.error
        got = guard.embedder(_models([])).embed(_png((0, 0, 255)))
    assert got == {"vector": [0.0, 0.0, 1.0], "model_id": "clip", "model_version": "ViT-B-32-openai"}
    assert worker_made[0].calls == 1


def test_a_worker_with_the_wrong_key_or_down_hands_its_work_to_the_built_in_machine() -> None:
    worker_made: list = []
    with _worker(worker_made) as url:
        for worker in (Machine("aim_gpu", "GPU box", url, "not its key", 2),
                       Machine("aim_gone", "Gone", "http://127.0.0.1:9/v1", KEY, 2)):
            made: list = []
            client = Client()
            with _machines(worker, _built_in()):
                guard = ClipGuard(client)
                assert guard.check()
                assert not worker.online and worker.error  # said in Settings › AI
                assert guard.embedder(_models(made)).embed(_png())["vector"] == [1.0, 0.0, 0.0]
            assert made[0].calls == 1
            assert any(r["path"] == f"/v1/ai/machines/{worker.machine_id}/status" and r["online"] is False
                       for r in client.reported)
    assert worker_made == []


def test_an_image_no_machine_can_read_is_the_clips() -> None:
    worker_made: list = []
    with _worker(worker_made) as url:
        for machine in (_built_in(), Machine("aim_gpu", "GPU box", url, KEY, 2)):
            with _machines(machine):
                guard = ClipGuard(Client())
                assert guard.check()
                with pytest.raises(EmbeddingError) as raised:
                    guard.embedder(_models([])).embed(b"not an image")
                assert raised.value.endpoint_fault is False
                assert guard.charges(raised.value) is True  # the clip's, the machine checked out
                assert machine.online


def test_no_machine_left_waits_and_charges_no_clip() -> None:
    gone = Machine("aim_gone", "Gone", "http://127.0.0.1:9/v1", KEY, 2)
    with _machines(gone):
        guard = ClipGuard(Client())
        assert guard.check() is False and guard.down
        with pytest.raises(EmbeddingError) as raised:
            guard.embedder(_models([])).embed(_png())
        assert raised.value.endpoint_fault is True
        assert guard.charges(raised.value) is False


def test_the_built_in_machine_says_why_it_cant() -> None:
    assert built_in_clip("ViT-B-32") == (["ViT-B-32"], "ViT-B-32", "")
    models, serves, why = built_in_clip("ViT-L-14")
    assert serves == "" and "doesn't know ViT-L-14" in why
    with patch("src.processing.clip_guard.importlib.util.find_spec", return_value=None):
        assert built_in_clip("ViT-B-32")[2].startswith("This computer can't make CLIP vectors")
    with patch("src.processing.clip_guard.importlib.util.find_spec", return_value=None), _machines(_built_in()):
        guard = ClipGuard(Client())
        assert guard.check() is False and "open-clip-torch isn't installed" in guard.error


def test_the_built_in_machine_takes_its_turn_beside_video_decodes() -> None:
    """As CLIP always has on the server's GPU (AiJob.built_in_yields): video decoding doesn't hold it back."""
    built_in = _built_in(at_once=1)
    with _machines(built_in):
        guard = ClipGuard(Client())
        guard.check()
        guard.pool.set_gpu_hold(1)
        assert guard.capacity() == 1 and not built_in.shares_gpu
