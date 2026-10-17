"""A model whose weights are in the models folder loads from there alone
(src/processing/models_dir.py from_folder; the Whisper child's _whisper):
Hugging Face's library would otherwise ask huggingface.co about it on every
load, a request off this machine each time CLIP or Whisper starts (seen in
the service log, "unauthenticated requests to the HF Hub", at each CLIP
load). One that isn't there yet, or won't load from what's there, is
downloaded as before.

The network is blocked in these tests and every attempt to reach it
recorded, so "no request" is what's seen, not assumed."""

from __future__ import annotations

import socket
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.processing import models_dir

pytestmark = pytest.mark.fast

REPO = "lumiverb-test/model"
COMMIT = "0" * 40


@pytest.fixture
def attempts(monkeypatch) -> list:
    """Every reach for the network, refused."""
    tried: list = []

    def refuse(*args, **kwargs):
        tried.append(args[1:2] or args)
        raise OSError("the network is blocked in this test")

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: refuse(None, a[0]))
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: refuse(None, a))
    return tried


@pytest.fixture
def hub(tmp_path, monkeypatch) -> Path:
    """A Hugging Face folder holding REPO's weights, as a download leaves it."""
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    model = tmp_path / "hf" / "hub" / ("models--" + REPO.replace("/", "--"))
    (model / "blobs").mkdir(parents=True)
    (model / "blobs" / "abc").write_bytes(b"weights")
    (model / "snapshots" / COMMIT).mkdir(parents=True)
    (model / "snapshots" / COMMIT / "w.bin").symlink_to(model / "blobs" / "abc")
    (model / "refs").mkdir()
    (model / "refs" / "main").write_text(COMMIT)
    return tmp_path / "hf" / "hub"


def _download(hub: Path) -> str:
    from huggingface_hub import hf_hub_download

    return hf_hub_download(REPO, "w.bin", cache_dir=hub)


def test_weights_that_are_here_load_without_a_request(hub, attempts):
    path = models_dir.from_folder(lambda: _download(hub), REPO)
    assert Path(path).read_bytes() == b"weights"
    assert attempts == []


def test_the_check_would_see_one(hub, attempts):
    """The same load as Hugging Face's library makes it: it asks first (and,
    refused, uses what's here)."""
    assert Path(_download(hub)).read_bytes() == b"weights"
    assert attempts, "the library reached for the network"


def test_a_model_that_isnt_here_loads_as_before(tmp_path, monkeypatch):
    from huggingface_hub import constants

    monkeypatch.setenv("HF_HOME", str(tmp_path / "empty"))
    seen = []
    assert models_dir.from_folder(lambda: seen.append(constants.is_offline_mode()) or "loaded", REPO) == "loaded"
    assert seen == [False], "downloaded as before"


def test_one_that_wont_load_from_whats_here_loads_as_before(hub):
    from huggingface_hub import constants

    seen = []

    def load():
        seen.append(constants.is_offline_mode())
        if len(seen) == 1:
            raise FileNotFoundError("half a download")
        return "loaded"

    assert models_dir.from_folder(load, REPO) == "loaded"
    assert seen == [True, False]
    assert constants.is_offline_mode() is False, "the switch is put back"


def test_the_switch_is_put_back_whatever_the_load_does(hub):
    from huggingface_hub import constants

    with pytest.raises(RuntimeError):
        models_dir.from_folder(lambda: (_ for _ in ()).throw(RuntimeError("out of memory")), REPO)
    assert constants.is_offline_mode() is False


def test_downloaded_means_a_snapshot_with_a_file(hub):
    assert models_dir.downloaded(REPO)
    assert not models_dir.downloaded("lumiverb-test/other")
    assert not models_dir.downloaded(None)


@pytest.mark.parametrize("provider", ["src.server.embeddings.clip_provider",
                                      "src.processing.workers.embeddings.clip_provider"])
def test_clip_loads_from_the_folder_alone_when_its_weights_are_here(provider, monkeypatch):
    import importlib

    import open_clip
    from huggingface_hub import constants

    seen = []

    def create(name, pretrained, device):
        seen.append((name, pretrained, constants.is_offline_mode()))
        return MagicMock(), None, "preprocess"

    monkeypatch.setattr(models_dir, "downloaded", lambda repo: repo == models_dir.clip_repo("ViT-B-32", "openai"))
    monkeypatch.setattr(open_clip, "create_model_and_transforms", create)
    importlib.import_module(provider).CLIPEmbeddingProvider()._load()
    assert seen == [("ViT-B-32", "openai", True)]


def test_whisper_loads_from_whats_downloaded_first(monkeypatch):
    import faster_whisper

    from src.processing.workers.transcripts import child

    calls = []

    def model(name, **kwargs):
        calls.append(kwargs.get("local_files_only", False))
        if kwargs.get("local_files_only") and name == "missing":
            raise FileNotFoundError("not downloaded")
        return f"whisper {name}"

    monkeypatch.setattr(faster_whisper, "WhisperModel", model)
    assert child._whisper("small", "cpu", "int8") == "whisper small"
    assert calls == [True], "from what's here alone"
    calls.clear()
    assert child._whisper("missing", "cpu", "int8") == "whisper missing"
    assert calls == [True, False], "downloaded as before"
