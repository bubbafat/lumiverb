"""AI models' weights in one folder (src/processing/models_dir.py, ADR-020
decision 3): CLIP's, Whisper's and InsightFace's, shared by the API and the
scheduler. What was kept elsewhere before is renamed in (nothing downloaded
again); the models no account's settings name are pruned, only Whisper's
and CLIP's, never what changed in the last hour or an operator's folder.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.processing import models_dir
from src.processing.models_dir import FACE_MODEL, adopt_old_models, prune

pytestmark = pytest.mark.fast

ROOT = Path(__file__).resolve().parents[1]
SMALL, MEDIUM = "Systran/faster-whisper-small", "Systran/faster-whisper-medium"
CLIP = "timm/vit_base_patch32_clip_224.openai"


@pytest.fixture
def data(tmp_path, monkeypatch) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.delenv("HF_HOME", raising=False)
    monkeypatch.delenv("INSIGHTFACE_HOME", raising=False)
    return data


def _hub_model(hub: Path, repo: str, revision: str = "abc123", age: float = 7200) -> Path:
    """A model as Hugging Face's hub keeps it: a blob, and its snapshot's file linking to it."""
    model = hub / ("models--" + repo.replace("/", "--"))
    (model / "blobs").mkdir(parents=True)
    (model / "blobs" / "f00d").write_bytes(b"weights")
    (model / "snapshots" / revision).mkdir(parents=True)
    (model / "snapshots" / revision / "model.bin").symlink_to("../../blobs/f00d")
    (model / "refs").mkdir()
    (model / "refs" / "main").write_text(revision)
    _age(model, age)
    return model


def _face_model(root: Path, age: float = 7200) -> Path:
    model = root / "models" / FACE_MODEL
    model.mkdir(parents=True)
    (model / "det_10g.onnx").write_bytes(b"onnx")
    _age(model, age)
    return model


def _age(path: Path, seconds: float) -> None:
    when = time.time() - seconds
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files + dirs:
            os.utime(Path(root, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when))


# -- Where -------------------------------------------------------------------------------


def test_an_installs_models_are_in_its_data_folder(data):
    assert models_dir.models_dir() == data / "models"
    assert models_dir.hf_home() == data / "models" / "huggingface"
    assert models_dir.insightface_root() == data / "models" / "insightface"


def test_without_a_data_folder_they_re_this_machines_caches(monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert models_dir.models_dir() == tmp_path / "xdg" / "lumiverb" / "models"


def test_hugging_face_is_told_and_an_operators_folder_stays(data, monkeypatch):
    models_dir.use_models_dir()
    assert os.environ["HF_HOME"] == str(data / "models" / "huggingface")
    monkeypatch.setenv("HF_HOME", "/srv/operators-hf")
    models_dir.use_models_dir()
    assert os.environ["HF_HOME"] == "/srv/operators-hf"


def test_insightface_is_told_the_models_folder_unless_an_operator_says(data, monkeypatch):
    from src.processing.workers.faces.insightface_provider import model_root

    assert model_root() == data / "models" / "insightface"
    monkeypatch.setenv("INSIGHTFACE_HOME", "/srv/faces")
    assert model_root() == Path("/srv/faces")


@pytest.mark.parametrize("module", ["src.server.scheduler.service", "src.server.api.main"])
def test_nothing_reads_hf_home_before_a_process_sets_it(module):
    """Hugging Face reads HF_HOME as it's imported: the API and the scheduler
    set it as they start, so nothing may import it before then."""
    code = f"import sys, {module}; print('huggingface_hub' in sys.modules, 'open_clip' in sys.modules)"
    env = {**os.environ, "JWT_SECRET": "x" * 32}
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert out.stdout.strip().splitlines()[-1] == "False False", out.stderr[-2000:]


# -- What was kept elsewhere, moved in ---------------------------------------------------


def test_what_was_kept_elsewhere_is_renamed_in_and_still_reads(data):
    whisper = _hub_model(data / "cache" / "huggingface" / "hub", SMALL)  # the scheduler's (XDG_CACHE_HOME)
    (data / "cache" / "huggingface" / "xet").mkdir()
    clip = _hub_model(data / "hf-cache", CLIP)  # the API's (HF_HUB_CACHE was hf-cache itself)
    face = _face_model(data / "insightface-cache")
    inode = (whisper / "blobs" / "f00d").stat().st_ino

    moved = adopt_old_models()

    hub = data / "models" / "huggingface" / "hub"
    assert len(moved) == 3
    snapshot = hub / whisper.name / "snapshots" / "abc123" / "model.bin"
    assert snapshot.read_bytes() == b"weights", "its links still lead to its blobs"
    assert (hub / whisper.name / "blobs" / "f00d").stat().st_ino == inode, "renamed, not copied"
    assert (data / "models" / "huggingface" / "xet").is_dir(), "the whole of the scheduler's came"
    assert (hub / clip.name / "snapshots" / "abc123" / "model.bin").read_bytes() == b"weights"
    assert (data / "models" / "insightface" / "models" / FACE_MODEL / "det_10g.onnx").read_bytes() == b"onnx"
    assert not whisper.exists() and not clip.exists() and not face.exists()
    assert not (data / "hf-cache").exists() and not (data / "insightface-cache").exists()
    assert adopt_old_models() == [], "once"


def test_a_copy_of_one_already_there_goes_and_another_stays(data):
    hub = data / "models" / "huggingface" / "hub"
    _hub_model(hub, SMALL)
    _hub_model(hub, CLIP, revision="new")
    same = _hub_model(data / "cache" / "huggingface" / "hub", SMALL)
    other = _hub_model(data / "hf-cache", CLIP, revision="old")
    adopt_old_models()
    assert not same.exists(), "the same snapshot: a copy, and it goes"
    assert other.is_dir(), "another revision: kept until a person decides"
    assert (hub / other.name / "snapshots" / "new").is_dir()


def test_an_operators_folders_are_theirs(data, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(data / "operators"))
    monkeypatch.setenv("INSIGHTFACE_HOME", str(data / "faces"))
    whisper = _hub_model(data / "cache" / "huggingface" / "hub", SMALL)
    face = _face_model(data / "insightface-cache")
    assert adopt_old_models() == []
    assert whisper.is_dir() and face.is_dir()


def test_without_a_data_folder_nothing_moves(monkeypatch):
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert adopt_old_models() == []


# -- Pruning ---------------------------------------------------------------------------


def test_the_models_no_setting_names_go_and_nothing_else(data):
    hub = data / "models" / "huggingface" / "hub"
    small, medium, clip = _hub_model(hub, SMALL), _hub_model(hub, MEDIUM), _hub_model(hub, CLIP)
    (hub / ".locks" / medium.name).mkdir(parents=True)
    someone = _hub_model(hub, "someone/else")  # not Whisper's or CLIP's: never ours to judge
    fresh = _hub_model(hub, "Systran/faster-whisper-tiny", age=60)  # being downloaded, perhaps
    faces = data / "models" / "insightface"
    face = _face_model(faces)
    zip_ = faces / "models" / f"{FACE_MODEL}.zip"
    zip_.write_bytes(b"zip")
    _age(zip_, 7200)
    left = faces / "models" / f".{FACE_MODEL}.copying-123-456"  # a killed copy's
    left.mkdir()
    _age(left, 7200)
    copying = faces / "models" / f".{FACE_MODEL}.copying-789-1"  # one under way
    copying.mkdir()

    gone = prune({SMALL, CLIP}, {SMALL, MEDIUM, CLIP, "Systran/faster-whisper-tiny"})

    assert sorted(gone) == sorted([MEDIUM, f"insightface/{FACE_MODEL}.zip", f"insightface/{left.name}"])
    assert not medium.exists() and not (hub / ".locks" / medium.name).exists() and not zip_.exists()
    assert not left.exists()
    assert all(p.is_dir() for p in (small, clip, someone, fresh, face, copying))


def test_an_operators_folders_are_never_pruned(data, monkeypatch):
    hub = data / "models" / "huggingface" / "hub"
    medium = _hub_model(hub, MEDIUM)
    zip_ = data / "models" / "insightface" / "models" / f"{FACE_MODEL}.zip"
    zip_.parent.mkdir(parents=True)
    zip_.write_bytes(b"zip")
    _age(zip_, 7200)
    monkeypatch.setenv("HF_HOME", "/srv/operators-hf")
    monkeypatch.setenv("INSIGHTFACE_HOME", "/srv/faces")
    assert prune(set(), {MEDIUM}) == []
    assert medium.is_dir() and zip_.exists()


def test_a_models_repo_is_what_its_library_loads_it_from():
    assert models_dir.whisper_repo("small") == SMALL
    assert models_dir.whisper_repo("an-own-name") is None
    assert models_dir.clip_repo("ViT-B-32", "openai") == CLIP
    assert models_dir.clip_repo("ViT-B-32", "no-such-weights") is None
    assert CLIP in models_dir.clip_repos()


# -- What the accounts name (the scheduler) -------------------------------------------------


def _account(*, ready: bool = True, read: bool = True, whisper: str = "small",
             clip: tuple[str, str] = ("ViT-B-32", "openai")) -> SimpleNamespace:
    settings = {"clip": {"model": clip[0], "pretrained": clip[1]}}
    return SimpleNamespace(settings_ready=ready, guards={"transcripts": SimpleNamespace(model_read=read, model=whisper)},
                           producers=SimpleNamespace(settings=settings.__getitem__))


@pytest.fixture
def repos(monkeypatch):
    """CLIP's repos without loading open_clip: "<model>/<pretrained>"."""
    monkeypatch.setattr(models_dir, "clip_repo", lambda model, pretrained: f"{model}/{pretrained}")


def test_the_models_wanted_are_every_accounts_and_searchs(repos):
    from src.server.scheduler.service import models_wanted

    wanted = models_wanted([_account(), _account(whisper="medium", clip=("ViT-L-14", "laion")), _account(whisper="")])
    assert wanted == {"ViT-B-32/openai", "ViT-L-14/laion", SMALL, MEDIUM}


@pytest.mark.parametrize("unread", [{"ready": False}, {"read": False}])
def test_none_are_wanted_until_every_accounts_are_read(repos, unread):
    """One not read yet might name any: nothing is pruned meanwhile."""
    from src.server.scheduler.service import models_wanted, prune_unwanted_models

    accounts = [_account(), _account(**unread)]
    assert models_wanted(accounts) is None
    assert prune_unwanted_models(accounts) == []
    assert models_wanted([]) is None, "no account: nothing to go by"


def test_the_scheduler_prunes_once_a_day_after_its_first_ten_minutes_once_all_are_read():
    from src.server.scheduler import service

    now = [0.0]
    pruned: list[int] = []
    accounts = {"t1": _account(ready=False)}
    s = service.Scheduler(lambda: accounts, capacity={"scan": 0}, clock=lambda: now[0], runners={},
                          inline_refill=True, prune_models=lambda accts: pruned.append(len(accts)))

    def tick_at(t: float) -> None:
        now[0] = t
        s._prune_models_soon(accounts)
        if s._pruning is not None:
            s._pruning.result(timeout=5)

    try:
        tick_at(service.PRUNE_MODELS_FIRST_SEC - 1)
        assert pruned == [], "not in its first ten minutes"
        tick_at(service.PRUNE_MODELS_FIRST_SEC + 1)
        assert pruned == [], "an account's settings aren't read yet"
        accounts["t1"] = _account()
        tick_at(service.PRUNE_MODELS_FIRST_SEC + 2)
        assert pruned == [1]
        tick_at(service.PRUNE_MODELS_FIRST_SEC + 3600)
        assert pruned == [1], "once a day"
        tick_at(service.PRUNE_MODELS_FIRST_SEC + 2 + service.PRUNE_MODELS_EVERY_SEC)
        assert pruned == [1, 1]
    finally:
        s.stop()


def test_a_scheduler_not_told_to_prune_never_does():
    from src.server.scheduler import service

    accounts = {"t1": _account()}
    s = service.Scheduler(lambda: accounts, capacity={"scan": 0}, clock=lambda: 10**9, runners={}, inline_refill=True)
    try:
        s._prune_models_soon(accounts)
        assert s._pruning is None
    finally:
        s.stop()


def test_a_guard_says_its_model_is_read_only_once_a_check_reads_it():
    from unittest.mock import MagicMock

    from src.processing.job_guard import JobGuard

    client = MagicMock()
    client.get.side_effect = ConnectionError("the server isn't answering")
    guard = JobGuard(client, "transcripts")
    assert guard.check() is False and guard.model_read is False
    client.get.side_effect = None
    client.get.return_value.json.return_value = {"model": "", "machines": []}
    guard.check()
    assert guard.model_read is True and guard.model == "", "read, and off"
