"""The faces job's machines (ADR-022 phase 3), as the scheduler uses them:
the built-in machine (the scheduler's Models, with a stand-in face process)
and a Lumiverb AI worker served over real HTTP (src/server/ai_worker/),
asked by the code that asks every AI machine. Faces from either, with the
settings sent; a worker that's down hands its work to the built-in
machine; an image neither can read is the clip's; the GPU out of memory is
the machine's; no machine left waits, uncharged."""

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
from src.processing.faces_guard import FacesGuard, built_in_faces
from src.processing.workers.faces import FaceError
from src.server.ai_worker.app import make_app
from src.server.scheduler.models import Models

pytestmark = pytest.mark.fast
KEY = "faces-guard-key"
SETTINGS = {"model": "buffalo_l", "det_size": 640, "min_confidence": 0.7}
FOUND = {"detection_model": "insightface", "detection_model_version": "buffalo_l", "embedding_model": "buffalo_l",
         "faces": [{"bounding_box": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.3}, "detection_confidence": 0.9,
                    "embedding": [0.25] * 4}]}


class FakeFaces:
    """The scheduler's face process, as far as Models goes: what it's asked, and what it answers."""

    def __init__(self, answer: dict | None = None) -> None:
        self.calls: list[tuple[int, dict]] = []
        self.answer = answer

    def detect(self, image: bytes, settings: dict) -> dict:
        try:
            Image.open(io.BytesIO(image)).verify()
        except Exception as e:  # noqa: BLE001 — as the face process says a photo it can't read
            return {"error": f"cannot identify image file: {e}"}
        self.calls.append((len(image), settings))
        return self.answer if self.answer is not None else FOUND

    def close(self) -> None:
        pass

    def close_if_idle(self) -> bool:
        return False


class Client:
    def __init__(self) -> None:
        self.reported: list[dict] = []

    def post(self, path: str, json: dict | None = None, **_kw):
        self.reported.append({"path": path, **(json or {})})


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 160, 140)).save(buf, format="PNG")
    return buf.getvalue()


def _models(faces: FakeFaces) -> Models:
    return Models(clip_factory=lambda m, p: None, face_factory=lambda: faces)


@contextmanager
def _worker(faces: FakeFaces) -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(make_app(_models(faces), KEY), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.025)
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.should_exit = True
        thread.join(10)


@contextmanager
def _machines(*machines: Machine, model: str = "buffalo_l") -> Iterator[None]:
    prior = MachinePool.load

    def load(pool) -> None:
        if pool.job != "faces":
            return prior(pool)
        pool.model = model
        pool.machines = list(machines)

    with patch("src.processing.ai_pool.MachinePool.load", load):
        yield


def _built_in() -> Machine:
    return Machine("aim_self", "Built in", "", None, 1, built_in=True)


def test_the_built_in_machine_finds_faces_as_the_scheduler_always_has() -> None:
    faces = FakeFaces()
    with _machines(_built_in()):
        guard = FacesGuard(Client())
        assert guard.check() and not guard.down
        assert guard.detector(_models(faces)).detect(_png(), SETTINGS) == FOUND
    assert [s for _, s in faces.calls] == [SETTINGS]


def test_a_workers_faces_are_its_face_processes_with_the_settings_sent() -> None:
    worker_faces = FakeFaces()
    with _worker(worker_faces) as url, _machines(Machine("aim_gpu", "GPU box", url, KEY, 2)):
        guard = FacesGuard(Client())
        assert guard.check(), guard.error
        assert guard.detector(_models(FakeFaces())).detect(_png(), SETTINGS) == FOUND
    assert [s for _, s in worker_faces.calls] == [SETTINGS]


def test_a_worker_down_hands_its_work_to_the_built_in_machine() -> None:
    faces = FakeFaces()
    gone = Machine("aim_gone", "Gone", "http://127.0.0.1:9/v1", KEY, 2)
    with _machines(gone, _built_in()):
        guard = FacesGuard(Client())
        assert guard.check() and not gone.online
        assert guard.detector(_models(faces)).detect(_png(), SETTINGS) == FOUND
    assert len(faces.calls) == 1


def test_an_image_no_machine_can_read_is_the_clips() -> None:
    with _worker(FakeFaces()) as url:
        for machine in (_built_in(), Machine("aim_gpu", "GPU box", url, KEY, 2)):
            with _machines(machine):
                guard = FacesGuard(Client())
                assert guard.check()
                with pytest.raises(FaceError) as raised:
                    guard.detector(_models(FakeFaces())).detect(b"not an image", SETTINGS)
                assert raised.value.endpoint_fault is False and guard.charges(raised.value) is True


def test_the_gpu_out_of_memory_is_the_machines() -> None:
    oom = FakeFaces({"error": "CUDA failure 2: out of memory"})
    with _machines(_built_in()):
        guard = FacesGuard(Client())
        assert guard.check()
        with pytest.raises(FaceError) as raised:
            guard.detector(_models(oom)).detect(_png(), SETTINGS)
        assert raised.value.endpoint_fault is True and guard.charges(raised.value) is False


def test_no_machine_left_waits_and_charges_no_clip() -> None:
    with _machines(Machine("aim_gone", "Gone", "http://127.0.0.1:9/v1", KEY, 2)):
        guard = FacesGuard(Client())
        assert guard.check() is False and guard.down
        with pytest.raises(FaceError) as raised:
            guard.detector(_models(FakeFaces())).detect(_png(), SETTINGS)
        assert raised.value.endpoint_fault is True and guard.charges(raised.value) is False


def test_the_built_in_machine_says_why_it_cant() -> None:
    assert built_in_faces("buffalo_l") == (["buffalo_l"], "buffalo_l", "")
    assert "doesn't know antelopev2" in built_in_faces("antelopev2")[2]
    with patch("src.processing.faces_guard.importlib.util.find_spec", return_value=None), _machines(_built_in()):
        guard = FacesGuard(Client())
        assert guard.check() is False and "insightface isn't installed" in guard.error
