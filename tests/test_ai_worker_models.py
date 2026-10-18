"""The AI worker with the real models (ADR-022): what it gives another
machine's scheduler is what the scheduler's own code makes, so a library's
vectors and faces are uniform whichever machine made them. The repo's face
fixture, at a proxy's size. Opt-in: pytest -m ai (CLIP's and InsightFace's
weights downloaded, once)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.server.ai_worker.app import make_app
from src.server.scheduler.models import Models

pytestmark = pytest.mark.ai

FIXTURES = Path(__file__).resolve().parents[1] / "clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/Fixtures"
AUTH = {"Authorization": "Bearer k"}


def _proxy(name: str) -> bytes:
    with Image.open(FIXTURES / f"{name}.jpg") as img:
        img = img.convert("RGB")
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()


@pytest.fixture(scope="module")
def worker():
    from src.processing.workers.faces.insightface_provider import model_available

    if not model_available():
        pytest.skip("InsightFace's buffalo_l isn't downloaded")
    models = Models()
    yield TestClient(make_app(models, "k")), models
    models.close()


def test_its_clip_vector_is_the_schedulers(worker):
    client, models = worker
    image = _proxy("face_single")
    r = client.post("/v1/lumiverb/clip", headers=AUTH, data={"model": "ViT-B-32/openai"},
                    files=[("images", ("p.jpg", image, "image/jpeg"))])
    assert r.status_code == 200, r.text
    vector = r.json()["results"][0]["vector"]
    with Image.open(io.BytesIO(image)) as img:
        own = models.clip("ViT-B-32", "openai").embed_image(img.convert("RGB"))
    assert len(vector) == len(own) == 512
    assert max(abs(a - b) for a, b in zip(vector, own, strict=True)) < 1e-5
    assert r.json()["model_version"] == "ViT-B-32-openai"


def test_its_faces_are_the_schedulers(worker):
    from src.producers.faces.levels import gates

    client, models = worker
    settings = {"model": "buffalo_l", "det_size": 640, **gates("normal")}
    image = _proxy("face_single")
    r = client.post("/v1/lumiverb/faces", headers=AUTH, data={"settings": json.dumps(settings)},
                    files=[("images", ("p.jpg", image, "image/jpeg"))])
    assert r.status_code == 200, r.text
    found = r.json()["results"][0]
    own = models.faces().detect(image, settings)
    assert len(found["faces"]) == len(own["faces"]) == 1
    assert found["faces"][0]["bounding_box"] == pytest.approx(own["faces"][0]["bounding_box"], abs=1e-4)
    assert found["embedding_model"] == own["embedding_model"]
