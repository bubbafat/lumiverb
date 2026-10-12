"""Where the faces model (InsightFace buffalo_l) is kept
(src/processing/workers/faces/insightface_provider.py model_root): the
install's data folder, with Lumiverb's other models, for the scheduler's
faces process and the API's search by image alike. InsightFace reads no
setting (INSIGHTFACE_HOME was set and ignored): it's told, root=. A model
already in ~/.insightface is copied in, not downloaded again, and left there."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.processing.workers.faces import insightface_provider as faces

pytestmark = pytest.mark.fast


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(Path, "home", lambda: h)
    monkeypatch.delenv("INSIGHTFACE_HOME", raising=False)
    monkeypatch.delenv("DATA_DIR", raising=False)
    return h


def _downloaded(home: Path) -> Path:
    model = home / ".insightface" / "models" / "buffalo_l"
    model.mkdir(parents=True)
    (model / "det_10g.onnx").write_bytes(b"detector")
    (model / "w600k_r50.onnx").write_bytes(b"embedder")
    return model


def test_the_install_keeps_it_in_its_data_folder(home, tmp_path, monkeypatch):
    assert faces.model_root() == home / ".insightface", "a development checkout: InsightFace's own"
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    assert faces.model_root() == tmp_path / "data" / "insightface-cache"
    monkeypatch.setenv("INSIGHTFACE_HOME", str(tmp_path / "chosen"))
    assert faces.model_root() == tmp_path / "chosen", "an operator's choice wins"


def test_a_model_already_downloaded_is_copied_in_and_left_where_it_was(home, tmp_path):
    old = _downloaded(home)
    root = tmp_path / "data" / "insightface-cache"
    assert faces.adopt_home_copy(root) is True
    new = root / "models" / "buffalo_l"
    assert sorted(p.name for p in new.iterdir()) == ["det_10g.onnx", "w600k_r50.onnx"]
    assert (new / "det_10g.onnx").read_bytes() == b"detector"
    assert (old / "det_10g.onnx").read_bytes() == b"detector", "the home folder's copy is left alone"
    assert faces.adopt_home_copy(root) is False, "once"
    assert not list((root / "models").glob(".buffalo_l.copying-*"))


def test_one_there_already_is_never_overwritten(home, tmp_path):
    _downloaded(home)
    target = tmp_path / "root" / "models" / "buffalo_l"
    target.mkdir(parents=True)
    (target / "det_10g.onnx").write_bytes(b"newer")
    assert faces.adopt_home_copy(tmp_path / "root") is False
    assert (target / "det_10g.onnx").read_bytes() == b"newer"


def test_available_counts_one_that_can_be_copied_in(home, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    assert faces.model_available() is False
    _downloaded(home)
    assert faces.model_available() is True


def test_the_provider_tells_insightface_where(home, tmp_path, monkeypatch):
    import insightface.app

    _downloaded(home)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    told: dict = {}

    class FakeAnalysis:
        def __init__(self, name, root, providers, **kwargs):
            told.update(name=name, root=root)

            class Session:
                def get_providers(self):
                    return ["CPUExecutionProvider"]

            self.det_model = type("Det", (), {"session": Session()})()

        def prepare(self, ctx_id, det_size):
            pass

    monkeypatch.setattr(insightface.app, "FaceAnalysis", FakeAnalysis)
    faces.InsightFaceProvider().ensure_loaded()
    root = tmp_path / "data" / "insightface-cache"
    assert told == {"name": "buffalo_l", "root": str(root)}
    assert (root / "models" / "buffalo_l" / "det_10g.onnx").is_file(), "copied in before InsightFace looks"
