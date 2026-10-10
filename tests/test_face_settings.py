"""Face detection reads the account's settings (Settings → Processing → Faces):
how sure, how big and how sharp a face must be, and the image size looked at.
The model and the detector's input size stay fixed."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from src.processing.workers.faces.insightface_provider import FaceSettings, InsightFaceProvider
from src.producers.faces import PRODUCER as FACES
from tests.test_face_detection import _make_mock_face, _make_sharp_image

pytestmark = pytest.mark.fast


def _found(settings: FaceSettings | None, *faces, image=None) -> list:
    app = MagicMock()
    app.get.return_value = list(faces)
    provider = InsightFaceProvider(settings)
    provider._app = app
    return provider.detect_faces(image or _make_sharp_image(640, 480))


def test_the_defaults_are_the_producers_declared_defaults():
    s = FaceSettings()
    for key, value in FACES.defaults.items():
        if key != "model":
            assert getattr(s, key) == value, key


def test_settings_come_from_the_producers_settings_and_ignore_the_rest():
    s = FaceSettings.for_producer({**FACES.defaults, "min_confidence": 0.8, "something_new": 1})
    assert s.min_confidence == 0.8 and s.min_sharpness == FACES.defaults["min_sharpness"]


def test_how_sure_a_face_must_be():
    face = _make_mock_face([64, 48, 320, 336], det_score=0.6)
    assert len(_found(None, face)) == 1  # 0.6 ≥ 0.5
    assert _found(FaceSettings(min_confidence=0.7), face) == []


def test_how_many_pixels_wide_a_face_must_be():
    face = _make_mock_face([100, 100, 160, 180])  # 60 px wide
    assert len(_found(None, face)) == 1
    assert _found(FaceSettings(min_face_pixels=80), face) == []


def test_how_big_beside_the_largest_a_face_must_be():
    big, small = _make_mock_face([0, 0, 320, 320]), _make_mock_face([400, 300, 480, 380])  # 1/16 the area
    assert len(_found(None, big, small)) == 1  # under 0.15
    assert len(_found(FaceSettings(min_relative_size=0.05), big, small)) == 2


def test_how_sharp_a_face_must_be():
    face = _make_mock_face([64, 48, 320, 336])
    assert len(_found(None, face)) == 1
    assert _found(FaceSettings(min_sharpness=1e9), face) == []


def test_the_image_looked_at_is_at_most_the_size_set():
    app = MagicMock()
    app.get.return_value = []
    provider = InsightFaceProvider(FaceSettings(max_detect_edge=800))
    provider._app = app
    provider.detect_faces(Image.new("RGB", (1200, 900)))
    assert app.get.call_args.args[0].shape[:2] == (600, 800)


def test_the_detector_is_prepared_at_its_input_size():
    provider = InsightFaceProvider(FaceSettings(det_size=320))
    app = MagicMock()
    app.det_model.session.get_providers.return_value = ["CPUExecutionProvider"]
    with patch.dict("sys.modules", {"insightface": MagicMock(), "insightface.app": MagicMock(FaceAnalysis=lambda **kw: app)}):
        provider._load()
    assert app.prepare.call_args.kwargs["det_size"] == (320, 320)


# ── The face process and the scheduler use them ──────────────────────────


def _jpeg() -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buf, format="JPEG")
    return buf.getvalue()


def _detect(settings: dict, provider=None):
    from src.producers.faces import detect

    provider = provider or MagicMock(model_id="insightface", model_version="buffalo_l",
                                     detect_faces=MagicMock(return_value=[]))
    made: list[FaceSettings] = []

    def build(settings=None):
        made.append(settings)
        provider.settings = settings
        return provider

    detect._PROVIDER = None  # a fresh process
    with patch("src.processing.workers.faces.insightface_provider.InsightFaceProvider", build):
        out = detect.detect_in_child(_jpeg(), settings)
    return out, made


def test_the_face_process_finds_faces_with_the_settings_its_given_and_says_which_models(tmp_path):
    used = {**FACES.defaults, "min_confidence": 0.8}
    out, made = _detect(used)
    assert made[-1] == FaceSettings.for_producer(used)
    # InsightFace's model pack embeds the faces it finds: it says so.
    assert out == {"detection_model": "insightface", "detection_model_version": "buffalo_l",
                   "embedding_model": "buffalo_l", "faces": []}


def test_one_model_per_process_while_the_detector_size_stays(tmp_path):
    """Loading the model for every photo cost seconds each; the gates change without it."""
    from src.producers.faces import detect

    built: list[FaceSettings] = []

    class Provider:
        model_id, model_version = "insightface", "buffalo_l"

        def __init__(self, settings=None):
            built.append(settings)
            self.settings = settings

        def ensure_loaded(self):
            pass

        def detect_faces(self, img):
            return []

    detect._PROVIDER = None
    with patch("src.processing.workers.faces.insightface_provider.InsightFaceProvider", Provider):
        a = detect._provider({})
        b = detect._provider({"min_confidence": 0.9})
        c = detect._provider({"det_size": 320})
    assert a is b and b.settings.min_confidence == 0.9  # the same model, the new gates
    assert c is not a and len(built) == 2


def test_the_scheduler_finds_faces_with_the_settings_its_lineage_names(tmp_path, monkeypatch):
    from src.server.scheduler import runners
    from src.shared.producers import lineage
    from tests.test_scheduler_runners import FakeAccount, _job

    acct = FakeAccount(tmp_path)
    monkeypatch.setattr("src.producers.faces.work.face_proxy", lambda item, root, cache: True)
    used = {**FACES.defaults, "min_face_pixels": 60}
    acct.producers = MagicMock()
    acct.producers.settings.return_value = used
    acct.producers.lineage.side_effect = lambda artifact, sha, used=None: lineage(artifact, used, sha)
    acct.cache.get.return_value = b"jpg"
    detector = MagicMock()
    detector.detect.return_value = {"detection_model": "insightface", "detection_model_version": "buffalo_l",
                                    "embedding_model": "buffalo_l", "faces": []}
    acct.models.faces.return_value = detector
    runners.runners()["faces"](acct, _job("faces", "ast_1"))
    assert detector.detect.call_args.args[1] == used
    assert acct.client.post.call_args.kwargs["json"]["lineage"] == lineage("faces", used, None)
    acct.producers.settings.assert_called_once_with("faces")


def test_faces_settings_that_can_change_and_those_that_cant():
    from src.producers.clip import PROXY_CACHE_EDGE

    editable = {s.key for s in FACES.settings if not s.fixed and s.remakes}
    assert editable == {"max_detect_edge", "min_confidence", "min_area_fraction", "min_face_pixels",
                        "min_relative_size", "min_sharpness"}
    # How faces are grouped, not found: outside lineage.
    assert {s.key for s in FACES.settings if not s.remakes} == {"merge_close_clusters", "merge_distance"}
    assert "merge_close_clusters" not in FACES.defaults
    assert FACES.setting("model").fixed and FACES.setting("det_size").fixed
    assert FACES.setting("max_detect_edge").maximum <= PROXY_CACHE_EDGE  # faces are found in the proxies


def test_the_detector_keeps_faces_down_to_the_least_confidence_set():
    """SCRFD drops faces under its own det_thresh (0.5) before our gate sees
    them: a lower setting would change nothing yet redo every photo."""
    app = MagicMock()
    app.get.return_value = [_make_mock_face([64, 48, 320, 336], det_score=0.35)]
    provider = InsightFaceProvider(FaceSettings(min_confidence=0.3))
    provider._app = app
    assert len(provider.detect_faces(_make_sharp_image(640, 480))) == 1
    assert app.det_model.det_thresh == 0.3
    provider.settings = FaceSettings()  # the next batch, at the default
    provider.detect_faces(_make_sharp_image(640, 480))
    assert app.det_model.det_thresh == 0.5


@pytest.fixture(autouse=True)
def _no_provider_left_behind():
    """The face process's model is per process: a test's mock mustn't reach the next test."""
    from src.producers.faces import detect

    detect._PROVIDER = None
    yield
    detect._PROVIDER = None
