"""The AI worker (ADR-022) through its own API, with stand-ins for CLIP and
the face process (the scheduler's Models, given factories): the key on
every request, the models it lists as every AI machine does, a vector per
image and a found per image, an image it doesn't read that image's error
alone, the machine's troubles a 503, its turns, and its key kept for good,
owner-only, printed by --show-key."""

from __future__ import annotations

import io
import stat
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.server.ai_worker import ensure_key, key_file
from src.server.ai_worker.app import CLIP_MODELS, FACE_MODELS, MAX_IMAGES, make_app
from src.server.scheduler.models import Models

pytestmark = pytest.mark.fast
KEY = "worker-test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def _png(color=(200, 30, 30), size=(32, 24)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _gif() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="GIF")
    return buf.getvalue()


class FakeClip:
    model_id = "clip"

    def __init__(self, model: str, pretrained: str, *, hold: threading.Event | None = None) -> None:
        self.model_version = f"{model}-{pretrained}"
        self.seen: list[tuple[int, int]] = []
        self.running = 0
        self.most = 0
        self._hold = hold
        self._lock = threading.Lock()

    def embed_image(self, img) -> list[float]:
        with self._lock:
            self.running += 1
            self.most = max(self.most, self.running)
        try:
            if self._hold is not None:
                self._hold.wait(5)
            self.seen.append(img.size)
            r, g, b = img.getpixel((0, 0))
            return [r / 255, g / 255, b / 255]
        finally:
            with self._lock:
                self.running -= 1


class FakeFaces:
    def __init__(self, answer=None) -> None:
        self.calls: list[tuple[int, dict]] = []
        self.answer = answer

    def detect(self, image: bytes, settings: dict) -> dict:
        self.calls.append((len(image), settings))
        if self.answer is not None:
            return self.answer
        return {"detection_model": "insightface", "detection_model_version": "buffalo_l",
                "embedding_model": "buffalo_l", "faces": [{"bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                                                            "detection_confidence": 0.9, "embedding": [0.5] * 4}]}

    def close(self) -> None:
        pass


def _worker(*, clip=None, faces=None, at_once=2) -> tuple[TestClient, dict]:
    made: dict = {}

    def clip_factory(model, pretrained):
        made["clip"] = clip(model, pretrained) if clip else FakeClip(model, pretrained)
        return made["clip"]

    made["faces"] = faces or FakeFaces()
    models = Models(clip_factory=clip_factory, face_factory=lambda: made["faces"])
    return TestClient(make_app(models, KEY, at_once=at_once)), made


def _files(*images: bytes) -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("images", (f"{i}.png", data, "application/octet-stream")) for i, data in enumerate(images)]


def test_every_request_needs_its_key() -> None:
    client, _ = _worker()
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": KEY}):
        r = client.get("/v1/models", headers=headers)
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized", headers
        assert client.post("/v1/lumiverb/clip", headers=headers, data={"model": "ViT-B-32"},
                           files=_files(_png())).status_code == 401
    assert client.get("/v1/models", headers=AUTH).status_code == 200


def test_it_lists_its_models_as_every_ai_machine_does() -> None:
    client, _ = _worker()
    listed = client.get("/v1/models", headers=AUTH).json()
    assert listed["object"] == "list"
    assert [m["id"] for m in listed["data"]] == [*CLIP_MODELS, *sorted(FACE_MODELS)]


def test_the_scheduler_checks_it_as_it_checks_any_ai_machine() -> None:
    """Over real HTTP, by the code that checks every AI machine (src/shared/vision_endpoint.py)."""
    import socket

    import uvicorn

    from src.shared.vision_endpoint import VisionEndpointError, list_models

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    models = Models(clip_factory=lambda m, p: FakeClip(m, p), face_factory=FakeFaces)
    server = uvicorn.Server(uvicorn.Config(make_app(models, KEY), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.025)
        assert list_models(f"http://127.0.0.1:{port}/v1", KEY) == sorted([*CLIP_MODELS, *FACE_MODELS])
        with pytest.raises(VisionEndpointError):
            list_models(f"http://127.0.0.1:{port}/v1", "not its key")
    finally:
        server.should_exit = True
        thread.join(10)


def test_a_vector_for_each_image_and_its_models_version() -> None:
    client, made = _worker()
    r = client.post("/v1/lumiverb/clip", headers=AUTH, data={"model": "ViT-B-32"},
                    files=_files(_png((255, 0, 0)), b"not an image", _gif(), _png((0, 0, 255))))
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["model_id"], body["model_version"]) == ("clip", "ViT-B-32-openai")
    results = body["results"]
    assert results[0] == {"vector": [1.0, 0.0, 0.0]} and results[3] == {"vector": [0.0, 0.0, 1.0]}
    # What it doesn't read is that image's error alone, never shown to the model.
    assert "error" in results[1] and "JPEG, PNG or WebP" in results[1]["error"]
    assert "error" in results[2]
    assert made["clip"].seen == [(32, 24), (32, 24)]


def test_a_model_it_doesnt_serve_is_said() -> None:
    client, _ = _worker()
    r = client.post("/v1/lumiverb/clip", headers=AUTH, data={"model": "ViT-L-14/laion"}, files=_files(_png()))
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    r = client.post("/v1/lumiverb/faces", headers=AUTH, data={"settings": '{"model": "antelopev2"}'},
                    files=_files(_png()))
    assert r.status_code == 404 and r.json()["error"]["code"] == "model_not_found"
    for data in ({"model": "ViT-B-32"}, {"settings": '{"model": "buffalo_l"}'}):
        path = "/v1/lumiverb/clip" if "model" in data else "/v1/lumiverb/faces"
        r = client.post(path, headers=AUTH, data=data)  # no images
        assert r.status_code == 400, path
    r = client.post("/v1/lumiverb/clip", headers=AUTH, data={"model": "ViT-B-32"},
                    files=_files(*[_png()] * (MAX_IMAGES + 1)))
    assert r.status_code == 400


def test_faces_for_each_image_with_the_settings_sent() -> None:
    faces = FakeFaces()
    client, _ = _worker(faces=faces)
    settings = {"model": "buffalo_l", "det_size": 640, "min_confidence": 0.7}
    r = client.post("/v1/lumiverb/faces", headers=AUTH, data={"settings": __import__("json").dumps(settings)},
                    files=_files(_png(), _gif(), _png()))
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[0]["faces"][0]["detection_confidence"] == 0.9 and results[2]["detection_model_version"] == "buffalo_l"
    assert "error" in results[1]
    assert [s for _, s in faces.calls] == [settings, settings]  # the GIF never reached the face process
    r = client.post("/v1/lumiverb/faces", headers=AUTH, data={"settings": "not json"}, files=_files(_png()))
    assert r.status_code == 400


def test_the_machines_troubles_are_a_503_so_the_work_goes_elsewhere() -> None:
    client, _ = _worker(faces=FakeFaces({"error": "CUDA out of memory. Tried to allocate 2 GiB"}))
    r = client.post("/v1/lumiverb/faces", headers=AUTH, data={"settings": '{"model": "buffalo_l"}'},
                    files=_files(_png()))
    assert r.status_code == 503 and r.json()["error"]["code"] == "out_of_memory", r.text

    class Broken(FakeClip):
        def embed_image(self, img):
            raise RuntimeError("the model wouldn't load")

    client, _ = _worker(clip=Broken)
    r = client.post("/v1/lumiverb/clip", headers=AUTH, data={"model": "ViT-B-32"}, files=_files(_png()))
    assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"
    # A photo's own trouble stays that photo's.
    client, _ = _worker(faces=FakeFaces({"error": "cannot identify image"}))
    r = client.post("/v1/lumiverb/faces", headers=AUTH, data={"settings": '{"model": "buffalo_l"}'},
                    files=_files(_png()))
    assert r.status_code == 200 and r.json()["results"] == [{"error": "cannot identify image"}]


def test_requests_take_turns_at_once_at_a_time() -> None:
    hold = threading.Event()
    clips: list[FakeClip] = []

    def held(model, pretrained):
        clips.append(FakeClip(model, pretrained, hold=hold))
        return clips[-1]

    client, _ = _worker(clip=held, at_once=1)
    codes: list[int] = []

    def send() -> None:
        codes.append(client.post("/v1/lumiverb/clip", headers=AUTH, data={"model": "ViT-B-32"},
                                 files=_files(_png())).status_code)

    threads = [threading.Thread(target=send) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    hold.set()
    for t in threads:
        t.join(10)
    assert codes == [200, 200, 200] and clips[0].most == 1


def test_the_model_takes_turns_however_many_requests_are_taken() -> None:
    """Two requests at once (at_once 2) still give the model one image at a time: two threads on
    Apple's GPU at once hang it (found running a worker for real)."""
    hold = threading.Event()
    clips: list[FakeClip] = []

    def held(model, pretrained):
        clips.append(FakeClip(model, pretrained, hold=hold))
        return clips[-1]

    client, _ = _worker(clip=held, at_once=2)
    codes: list[int] = []

    def send() -> None:
        codes.append(client.post("/v1/lumiverb/clip", headers=AUTH, data={"model": "ViT-B-32"},
                                 files=_files(_png())).status_code)

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    hold.set()
    for t in threads:
        t.join(10)
    assert codes == [200, 200] and clips[0].most == 1


def test_its_key_is_made_once_kept_owner_only_and_shown(tmp_path, capsys) -> None:
    key = ensure_key(tmp_path)
    assert len(key) >= 40 and ensure_key(tmp_path) == key
    path = key_file(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    env = tmp_path / "env"
    env.write_text(f"DATA_DIR={tmp_path}\nCONTROL_PLANE_DATABASE_URL=postgresql://x@127.0.0.1/none\n")
    from src.server.lifecycle.cli import main

    assert main(["ai-worker", "--env", str(env), "--show-key"]) == 0
    assert capsys.readouterr().out.strip() == key
