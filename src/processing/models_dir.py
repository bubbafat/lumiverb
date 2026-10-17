"""Where Lumiverb keeps AI models' weights (ADR-020 decision 3): CLIP's and
Whisper's (Hugging Face's hub, HF_HOME) and InsightFace's, in one folder on
this machine, shared by the API (search) and the scheduler (the producers):
DATA_DIR/models on an install, else this machine's cache folder's
(src/processing/cache_dir.py). No cap: a model is downloaded once and kept
while a setting names it (prune).

Before, the API kept CLIP in DATA_DIR/hf-cache, the scheduler kept CLIP and
Whisper in its cache folder's Hugging Face cache (XDG_CACHE_HOME), and
InsightFace was in DATA_DIR/insightface-cache: a model could be downloaded
twice. What's there is renamed in at each start (adopt_old_models), on the
same disk: nothing is downloaded again.

An operator's HF_HOME or INSIGHTFACE_HOME is theirs: used as it is, and
nothing in it is moved or pruned.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Iterable
from pathlib import Path

from src.shared.whisper_models import REPOS

logger = logging.getLogger(__name__)

HF = "huggingface"
FACES = "insightface"
# The one face model there is (src/processing/workers/faces/insightface_provider.py).
FACE_MODEL = "buffalo_l"
# Nothing changed this recently is pruned: a download or a copy may be under way.
QUIET_SEC = 3600


def models_dir() -> Path:
    """DATA_DIR/models (an install's), else this machine's cache folder's."""
    if data := os.environ.get("DATA_DIR"):
        return Path(data) / "models"
    from src.processing.cache_dir import cache_dir

    return cache_dir("models")


def hf_home() -> Path:
    return models_dir() / HF


def insightface_root() -> Path:
    return models_dir() / FACES


def use_models_dir() -> None:
    """Hugging Face's downloads (CLIP's, Whisper's) go to the models folder:
    HF_HOME, set before anything reads it (each process's start; what it
    starts inherits it). An operator's HF_HOME stays."""
    os.environ.setdefault("HF_HOME", str(hf_home()))


def _ours(env: str, folder: Path) -> bool:
    """Whether the folder an env var names is the models folder's (not an operator's)."""
    named = os.environ.get(env)
    return named is None or Path(named).absolute() == folder.absolute()


def _hub_name(repo: str) -> str:
    return "models--" + repo.strip("/").replace("/", "--")


# -- Moving in what's there ----------------------------------------------------------


def _snapshots(model: Path) -> set[str]:
    try:
        return {p.name for p in (model / "snapshots").iterdir()}
    except OSError:
        return set()


def _move_in(old: Path, new: Path, moved: list[str]) -> None:
    """old renamed to new when new isn't there; when it is, old goes if it's
    the same (every snapshot old has, new has: Hugging Face's layout; or,
    without one, the same files), else it stays. Same disk only."""
    if not old.is_dir() or old.is_symlink():
        return
    if not new.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(old, new)
        except FileNotFoundError:
            return  # another process moved it first
        except OSError as exc:
            logger.warning("models: couldn't move %s into %s (left where it is): %s", old, new, exc)
            return
        moved.append(f"{old} -> {new}")
        return
    old_snaps = _snapshots(old)
    same = old_snaps <= _snapshots(new) if old_snaps else _files(old) <= _files(new)
    if same:
        shutil.rmtree(old, ignore_errors=True)
        moved.append(f"{old} (a copy of {new}) removed")


def _files(folder: Path) -> set[tuple[str, int]]:
    out = set()
    for root, _dirs, names in os.walk(folder):
        for name in names:
            path = Path(root, name)
            try:
                out.add((str(path.relative_to(folder)), path.stat().st_size))
            except OSError:
                continue
    return out


def _remove_if_empty(*folders: Path) -> None:
    """Each folder that's empty but for empty folders goes, innermost first."""
    for folder in folders:
        if not folder.is_dir() or folder.is_symlink():
            continue
        for root, dirs, files in os.walk(folder, topdown=False):
            if files:
                return
            for name in dirs:
                try:
                    Path(root, name).rmdir()
                except OSError:
                    return
        try:
            folder.rmdir()
        except OSError:
            return


def adopt_old_models() -> list[str]:
    """The models kept where they were before this folder, renamed into it:
    the scheduler's Hugging Face cache (DATA_DIR/cache/huggingface), the
    API's (DATA_DIR/hf-cache) and InsightFace's (DATA_DIR/insightface-cache).
    At each process's start, before a model is loaded; another process doing
    it at once is fine. What moved."""
    data = os.environ.get("DATA_DIR")
    if not data:
        return []
    data_dir, moved = Path(data), []
    hub = hf_home() / "hub"
    if _ours("HF_HOME", hf_home()):
        scheduler_hf = data_dir / "cache" / HF
        if not hf_home().exists() and scheduler_hf.is_dir() and not scheduler_hf.is_symlink():
            _move_in(scheduler_hf, hf_home(), moved)  # the whole of it: hub, xet, locks
        for old in (scheduler_hf / "hub", data_dir / "hf-cache", data_dir / "hf-cache" / "hub"):
            try:
                found = sorted(p for p in old.iterdir() if p.name.startswith("models--"))
            except OSError:
                continue
            for model in found:
                _move_in(model, hub / model.name, moved)
        _remove_if_empty(scheduler_hf / "hub", data_dir / "hf-cache")
    if _ours("INSIGHTFACE_HOME", insightface_root()):
        old = data_dir / "insightface-cache"
        _move_in(old / "models" / FACE_MODEL, insightface_root() / "models" / FACE_MODEL, moved)
        _remove_if_empty(old)
    for line in moved:
        logger.info("models: %s", line)
    return moved


# -- Pruning ----------------------------------------------------------------------------


def clip_repo(model: str, pretrained: str) -> str | None:
    """The Hugging Face repo open_clip loads a CLIP model's weights from, or None."""
    import open_clip

    try:
        hub = open_clip.get_pretrained_cfg(model, pretrained).get("hf_hub") or ""
    except Exception:  # noqa: BLE001 — a model open_clip doesn't know: no repo
        return None
    return hub.strip("/") or None


def clip_repos() -> set[str]:
    """Every repo open_clip loads pretrained CLIP weights from."""
    import open_clip

    return {repo for model, tag in open_clip.list_pretrained() if (repo := clip_repo(model, tag))}


def whisper_repo(model: str) -> str | None:
    """The repo the built-in Whisper loads a model from (faster-whisper's table)."""
    return REPOS.get(model)


def _quiet(path: Path) -> bool:
    """Nothing in it changed for QUIET_SEC (it, or what's right in it)."""
    try:
        newest = max([path.lstat().st_mtime, *(e.stat(follow_symlinks=False).st_mtime for e in os.scandir(path))]
                     if path.is_dir() and not path.is_symlink() else [path.lstat().st_mtime])
    except OSError:
        return False
    return time.time() - newest >= QUIET_SEC


def prune(wanted: Iterable[str], known: Iterable[str]) -> list[str]:
    """The models in the folder that a repo in `known` names (Whisper's,
    CLIP's: never anything else there) and none in `wanted` does, removed;
    and anything in the face models' but the one there is (a download's
    zip, another model, a copy a killed process left). Never what changed
    in the last QUIET_SEC, nor in an operator's folder. What went."""
    wanted, gone = set(wanted), []
    hub = hf_home() / "hub"
    if _ours("HF_HOME", hf_home()):
        for repo in sorted(set(known) - wanted):
            model = hub / _hub_name(repo)
            if model.is_dir() and not model.is_symlink() and _quiet(model):
                shutil.rmtree(model, ignore_errors=True)
                shutil.rmtree(hub / ".locks" / _hub_name(repo), ignore_errors=True)
                gone.append(repo)
    faces = insightface_root() / "models"
    if _ours("INSIGHTFACE_HOME", insightface_root()) and faces.is_dir():
        for path in sorted(faces.iterdir()):
            if path.name == FACE_MODEL or not _quiet(path):
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
            gone.append(f"{FACES}/{path.name}")
    for name in gone:
        logger.info("models: %s removed: no setting names it", name)
    return gone
