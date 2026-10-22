"""Where the faces model (InsightFace buffalo_l) is kept
(src/processing/workers/faces/insightface_provider.py model_root): the
models folder, with Lumiverb's other models (src/processing/models_dir.py),
for the scheduler's faces process and the API's search by image alike. InsightFace reads no
setting (INSIGHTFACE_HOME was set and ignored): it's told, root=. A model
already in ~/.insightface is copied in, not downloaded again, and left there."""

from __future__ import annotations

import os
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
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    return h


def _downloaded(home: Path) -> Path:
    model = home / ".insightface" / "models" / "buffalo_l"
    model.mkdir(parents=True)
    (model / "det_10g.onnx").write_bytes(b"detector")
    (model / "w600k_r50.onnx").write_bytes(b"embedder")
    return model


def test_the_install_keeps_it_in_its_models_folder(home, tmp_path, monkeypatch):
    assert faces.model_root() == home / ".cache" / "lumiverb" / "models" / "insightface", \
        "a development checkout: this machine's cache folder's"
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    assert faces.model_root() == tmp_path / "data" / "models" / "insightface"
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
    _no_matplotlib_setting(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "data" / "cache"))
    faces.InsightFaceProvider().ensure_loaded()
    root = tmp_path / "data" / "models" / "insightface"
    assert told == {"name": "buffalo_l", "root": str(root)}
    assert (root / "models" / "buffalo_l" / "det_10g.onnx").is_file(), "copied in before InsightFace looks"
    assert os.environ["MPLCONFIGDIR"] == str(tmp_path / "data" / "cache" / "lumiverb" / "matplotlib")


def _no_matplotlib_setting(monkeypatch) -> None:
    """MPLCONFIGDIR unset, and unset again after the test whatever the code sets."""
    monkeypatch.setenv("MPLCONFIGDIR", "")
    monkeypatch.delenv("MPLCONFIGDIR")


def test_matplotlibs_settings_folder_is_beside_the_caches_never_the_home_folder(home, tmp_path, monkeypatch):
    # InsightFace imports matplotlib, which makes itself a settings folder as it's imported: a Linux install's
    # home folder is read-only to the scheduler and the API.
    _no_matplotlib_setting(monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "data" / "cache"))
    faces.matplotlib_settings_beside_the_caches()
    assert os.environ["MPLCONFIGDIR"] == str(tmp_path / "data" / "cache" / "lumiverb" / "matplotlib")
    monkeypatch.setenv("MPLCONFIGDIR", str(tmp_path / "chosen"))
    faces.matplotlib_settings_beside_the_caches()
    assert os.environ["MPLCONFIGDIR"] == str(tmp_path / "chosen"), "an operator's stays"
