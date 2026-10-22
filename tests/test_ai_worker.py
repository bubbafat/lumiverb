"""The AI worker (ADR-022) through its own API, with stand-ins for CLIP and
the face process (the scheduler's Models, given factories): the key on
every request, the models it lists as every AI machine does, a vector per
image and a found per image, an image it doesn't read that image's error
alone, the machine's troubles a 503, its turns, and its key kept for good,
owner-only, printed by --show-key. Run as `lumiverb-server ai-worker` runs
it: HTTPS with its own certificate, reached by that certificate's
fingerprint alone; plain HTTP when it's chosen, or for a worker set up
before it served HTTPS until it's started with --https, said loudly."""

from __future__ import annotations

import io
import logging
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.server.ai_worker import (
    certificate_files,
    ensure_certificate,
    ensure_key,
    fingerprint,
    key_file,
)
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
                self._hold.wait(60)
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
        deadline = time.monotonic() + 60
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.025)
        assert list_models(f"http://127.0.0.1:{port}/v1", KEY) == sorted([*CLIP_MODELS, *FACE_MODELS])
        with pytest.raises(VisionEndpointError):
            list_models(f"http://127.0.0.1:{port}/v1", "not its key")
    finally:
        server.should_exit = True
        thread.join(60)


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
        t.join(60)
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
        t.join(60)
    assert codes == [200, 200] and clips[0].most == 1


def test_its_key_is_made_once_kept_owner_only_and_shown(tmp_path, capsys, monkeypatch) -> None:
    key = ensure_key(tmp_path)
    assert len(key) >= 40 and ensure_key(tmp_path) == key
    path = key_file(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    env = tmp_path / "env"
    env.write_text(f"DATA_DIR={tmp_path}\nCONTROL_PLANE_DATABASE_URL=postgresql://x@127.0.0.1/none\n")
    monkeypatch.delenv("DATA_DIR")  # the test process's own (tests/conftest.py) would win over the file's
    from src.server.lifecycle.cli import main

    assert main(["ai-worker", "--env", str(env), "--show-key"]) == 0
    assert capsys.readouterr().out.strip() == key


def test_a_new_worker_makes_its_certificate_with_its_key_kept_for_good_and_shown(tmp_path, capsys, monkeypatch) -> None:
    """Its own, signed by nobody, its key owner-only; never made again on its own (the fingerprint it was added with
    would no longer be its own)."""
    from cryptography import x509

    from src.server.lifecycle.cli import main
    from src.shared.ai_requests import FINGERPRINT

    ensure_key(tmp_path)
    made = fingerprint(tmp_path)
    assert made is not None and FINGERPRINT.fullmatch(made)
    cert, key = certificate_files(tmp_path)
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert ensure_certificate(tmp_path) == made and ensure_key(tmp_path) and fingerprint(tmp_path) == made
    issued = x509.load_pem_x509_certificate(cert.read_bytes())
    assert issued.issuer == issued.subject, "its own: no authority signed it"
    assert issued.not_valid_after_utc - issued.not_valid_before_utc > __import__("datetime").timedelta(days=3000)
    env = tmp_path / "env"
    env.write_text(f"DATA_DIR={tmp_path}\nCONTROL_PLANE_DATABASE_URL=postgresql://x@127.0.0.1/none\n")
    monkeypatch.delenv("DATA_DIR")  # the test process's own (tests/conftest.py) would win over the file's
    assert main(["ai-worker", "--env", str(env), "--show-fingerprint"]) == 0
    assert capsys.readouterr().out.strip() == made
    # One set up before it served HTTPS has none to show.
    old = tmp_path / "old"
    key_file(old).parent.mkdir(parents=True)
    key_file(old).write_text("a-key-from-before\n")
    env.write_text(f"DATA_DIR={old}\nCONTROL_PLANE_DATABASE_URL=postgresql://x@127.0.0.1/none\n")
    assert main(["ai-worker", "--env", str(env), "--show-fingerprint"]) == 1
    assert "no certificate" in capsys.readouterr().err and fingerprint(old) is None


@contextmanager
def _running(tmp_path, monkeypatch, *flags: str) -> Iterator[int]:
    """`lumiverb-server ai-worker` itself on this computer, as it's run (its certificate, its uvicorn), with stand-ins
    for its models, until the block ends. Its port."""
    import socket

    import uvicorn

    from src.server.lifecycle import cli

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = tmp_path / "env"
    env.write_text(f"DATA_DIR={tmp_path}\nCONTROL_PLANE_DATABASE_URL=postgresql://x@127.0.0.1/none\n")
    # The worker puts its install's settings in the environment: these, restored after.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", "postgresql://x@127.0.0.1/none")
    leave = threading.Event()
    servers: list[uvicorn.Server] = []

    class Server(uvicorn.Server):
        def __init__(self, config) -> None:
            super().__init__(config)
            servers.append(self)

    def until_left(command, stop, upgraded, every=0) -> None:
        leave.wait(60)
        stop.set()

    stand_ins = Models(clip_factory=lambda m, p: FakeClip(m, p), face_factory=FakeFaces)
    with patch.object(cli, "watch_for_upgrade", until_left), patch.object(cli, "_launched_as", lambda: tmp_path / "x"), \
            patch("uvicorn.Server", Server), patch("src.server.scheduler.models.Models", lambda: stand_ins), \
            patch("src.shared.logging_config.configure_api_logging", lambda: None):
        ran: list[int] = []
        thread = threading.Thread(target=lambda: ran.append(cli.main(
            ["ai-worker", "--env", str(env), "--host", "127.0.0.1", "--port", str(port), *flags])), daemon=True)
        thread.start()
        deadline = time.monotonic() + 60
        while not (servers and servers[0].started) and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.025)
        assert servers and servers[0].started, "the worker started"
        try:
            yield port
        finally:
            leave.set()
            thread.join(60)
    assert ran == [0]


def test_a_worker_serves_https_with_its_own_certificate_and_is_reached_by_it_alone(tmp_path, monkeypatch, caplog):
    """A new worker, as it runs: HTTPS from its first start, saying what to add (its https:// address, its
    certificate's fingerprint, where its key is); the scheduler, given that fingerprint, checks it and sends it work,
    by the code every AI machine is asked with; another certificate's fingerprint, or none, and nothing is."""
    from src.processing.ai_worker_client import AiWorker
    from src.processing.workers.embeddings.base import EmbeddingError
    from src.shared.vision_endpoint import (
        NOT_ITS_CERTIFICATE,
        UNTRUSTED,
        VisionEndpointError,
        list_models,
    )
    from tests.ai_machine_server import certificate

    _, another = certificate(tmp_path / "another")
    with caplog.at_level(logging.INFO), _running(tmp_path, monkeypatch) as port:
        key, pinned = key_file(tmp_path).read_text().strip(), fingerprint(tmp_path)
        url = f"https://127.0.0.1:{port}/v1"
        said = caplog.text
        assert f"add a machine at {url}" in said and f"certificate fingerprint {pinned}" in said
        assert "--show-key" in said and "over HTTPS" in said and "PLAIN HTTP" not in said
        assert list_models(url, key, fingerprint=pinned) == sorted([*CLIP_MODELS, *FACE_MODELS])
        vector = AiWorker(url, key, fingerprint=pinned).clip(_png((255, 0, 0)), "ViT-B-32")
        assert vector["vector"] == [1.0, 0.0, 0.0]
        for wrong, says in ((another, NOT_ITS_CERTIFICATE), ("", UNTRUSTED)):
            with pytest.raises(VisionEndpointError) as raised:
                list_models(url, key, fingerprint=wrong)
            assert str(raised.value) == says
        with pytest.raises(EmbeddingError, match="isn't the one it was added with"):
            AiWorker(url, key, fingerprint=another).clip(_png(), "ViT-B-32")
        with pytest.raises(VisionEndpointError):
            list_models(f"http://127.0.0.1:{port}/v1", key)  # plain HTTP isn't served beside it


def test_plain_http_when_its_chosen_said_loudly(tmp_path, monkeypatch, caplog):
    from src.shared.vision_endpoint import list_models

    with caplog.at_level(logging.INFO), _running(tmp_path, monkeypatch, "--plain-http") as port:
        key = key_file(tmp_path).read_text().strip()
        assert list_models(f"http://127.0.0.1:{port}/v1", key) == sorted([*CLIP_MODELS, *FACE_MODELS])
    loud = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("PLAIN HTTP" in line for line in loud) and any("--plain-http" in line for line in loud)
    assert f"machine at http://127.0.0.1:{port}/v1" in caplog.text


def test_a_worker_from_before_keeps_plain_http_until_its_started_with_https(tmp_path, monkeypatch, caplog):
    """Its Lumiverb has its http:// address: an upgrade alone changes nothing there, and says loudly how to switch.
    Started with --https it makes its certificate and serves HTTPS from then on, its key the same."""
    from src.shared.vision_endpoint import list_models

    key_file(tmp_path).parent.mkdir(parents=True)
    key_file(tmp_path).write_text("a-key-from-before\n")
    with caplog.at_level(logging.INFO), _running(tmp_path, monkeypatch) as port:
        assert list_models(f"http://127.0.0.1:{port}/v1", "a-key-from-before")
    loud = " ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "PLAIN HTTP" in loud and "set up before the AI worker served HTTPS" in loud and "--https" in loud
    assert fingerprint(tmp_path) is None
    caplog.clear()
    for flags in (("--https",), ()):  # and after, as its certificate is there
        with caplog.at_level(logging.INFO), _running(tmp_path, monkeypatch, *flags) as port:
            pinned = fingerprint(tmp_path)
            assert pinned and f"certificate fingerprint {pinned}" in caplog.text
            assert list_models(f"https://127.0.0.1:{port}/v1", "a-key-from-before", fingerprint=pinned)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING and "PLAIN" in r.getMessage()]
        caplog.clear()
