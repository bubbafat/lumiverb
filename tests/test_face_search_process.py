"""Search by image's faces are found in a process of their own
(src/server/faces/server_face_provider.py), the faces producer's
(src/producers/faces/detect.py FaceDetector): the API's own process never
loads the face model, whose ONNX Runtime leaks with every picture; the child
is replaced after so many pictures, given up on when it hangs, and let go of
once idle."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.server.faces import server_face_provider as faces
from tests.test_server_images import sample

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "clients/lumiverb-app/Sources/LumiverbKit/Tests/LumiverbKitTests/Fixtures"
FACE = {"bounding_box": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}, "detection_confidence": 0.98,
        "embedding": [0.5] * 512}


@pytest.fixture
def child(monkeypatch):
    """The face process's stand-in: what it was handed, and what it answers."""
    said: dict = {"answer": {"faces": [FACE]}, "handed": []}

    def detect(image: bytes, settings: dict) -> dict:
        said["handed"].append((image, settings))
        return said["answer"]

    monkeypatch.setattr(faces, "_the_detector", lambda: SimpleNamespace(detect=detect))
    return said


@pytest.mark.fast
def test_the_faces_come_back_as_the_search_takes_them(child):
    found = faces.detect_faces_in_image(sample("JPEG"))
    assert [(f.bounding_box, f.detection_confidence, f.embedding) for f in found] == [
        (FACE["bounding_box"], 0.98, [0.5] * 512)]
    assert child["handed"] == [(sample("JPEG"), {})], "the picture as sent, the faces producer's defaults"


@pytest.mark.fast
def test_a_picture_the_child_couldnt_look_at_is_an_error_the_search_falls_back_from(child):
    child["answer"] = {"error": "out of memory"}
    with pytest.raises(RuntimeError, match="out of memory"):
        faces.detect_faces_in_image(sample("PNG"))


@pytest.mark.fast
def test_the_child_is_the_faces_producers_replaced_and_given_up_on(monkeypatch):
    from src.producers.faces.detect import FaceDetector

    monkeypatch.setattr(faces, "_detector", None)
    monkeypatch.setattr(faces, "_ensure_writable_cache", lambda: "")
    detector = faces._the_detector()
    try:
        assert isinstance(detector, FaceDetector) and faces._the_detector() is detector
        assert (detector._per_process, detector._timeout_sec) == (faces.PER_PROCESS, faces.TIMEOUT_SEC)
        assert detector._pool is None, "no process until a search asks"
    finally:
        detector.close()


@pytest.mark.fast
def test_an_idle_child_is_let_go_of_and_a_busy_or_recent_one_isnt(monkeypatch):
    closed: list[bool] = []
    monkeypatch.setattr(faces, "_detector", SimpleNamespace(close_if_idle=lambda: closed.append(True) or True))
    monkeypatch.setattr(faces, "_last_used", 1000.0)
    assert faces.let_go_if_idle(now=1000.0 + faces.IDLE_SEC - 1) is False and closed == []
    assert faces.let_go_if_idle(now=1000.0 + faces.IDLE_SEC) is True and closed == [True]
    monkeypatch.setattr(faces, "_detector", None)
    assert faces.let_go_if_idle(now=10**9) is False, "none made yet"


@pytest.mark.ai
def test_a_real_picture_s_faces_are_found_and_the_api_s_process_never_loads_the_model():
    from src.processing.workers.faces.insightface_provider import model_available

    if not model_available():
        pytest.skip("InsightFace's buffalo_l isn't downloaded")
    code = (
        "import json, sys\n"
        "from src.server.faces.server_face_provider import close, detect_faces_in_image\n"
        f"found = detect_faces_in_image(open({str(FIXTURES / 'face_single.jpg')!r}, 'rb').read())\n"
        "loaded = sorted(m for m in ('insightface', 'onnxruntime') if m in sys.modules)\n"
        "close()\n"
        "print(json.dumps({'faces': len(found), 'dims': [len(f.embedding) for f in found], 'loaded': loaded}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stderr
    said = json.loads(out.stdout.strip().splitlines()[-1])
    assert said["faces"] >= 1 and set(said["dims"]) == {512}, said
    assert said["loaded"] == [], "the model ran in the child, never in the API's process"
