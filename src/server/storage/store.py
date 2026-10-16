"""What Lumiverb keeps, by key (ADR-020 decision 2).

The previews, thumbnails, video previews, analysis proxies, scene frames
and face crops Lumiverb makes are kept in a `Store`, by key. Keys are
`{tenant}/{library}/{kind}/{bucket}/...` (the builders below); where a key
is kept, and on what, is the provider's alone: no caller learns a folder.
`get_store()` gives the one `STORAGE_PROVIDER` names; the only provider is
the filesystem's (src/server/storage/files.py), and an install naming any
other refuses to start (src/server/config.py).

Two failures, never confused:
- `NotFound`: the key has no file (the missing-file repair acts on it);
- `Unavailable`: the storage can't be reached now (a chosen folder's drive
  unplugged). Work waits; nothing is taken for missing.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol, runtime_checkable

from ulid import ULID

# The providers an install may name (STORAGE_PROVIDER). No other until one is needed (ADR-020 decision 8).
PROVIDERS = ("local",)


class UnsafeKeyError(ValueError):
    """A storage key that would reach outside where keys are kept."""


class NotFound(FileNotFoundError):
    """The key has no file."""


class Unavailable(OSError):
    """The storage can't be reached now: nothing can be said of the key. Never
    a FileNotFoundError, so a drive unplugged is never taken for files gone."""


@dataclass(frozen=True)
class Stat:
    """What a stored file is now: its size, and when it was last written
    (seconds since the epoch): a version for what's made from it."""

    size: int
    modified: float


@runtime_checkable
class Store(Protocol):
    def open(self, key: str) -> BinaryIO:
        """The file, to read; seekable (a range is a seek). NotFound, Unavailable."""

    def read(self, key: str) -> bytes:
        """All of it. NotFound, Unavailable."""

    def size(self, key: str) -> int:
        """Its size in bytes. NotFound, Unavailable."""

    def stat(self, key: str) -> Stat:
        """Its size and when it was last written. NotFound, Unavailable."""

    def exists(self, key: str) -> bool:
        """Whether the key has a file. Unavailable when that can't be known."""

    def write(self, key: str, data: bytes | BinaryIO) -> None:
        """Kept whole or not at all: a reader never sees part of it. Unavailable."""

    def delete(self, key: str) -> None:
        """Gone wherever it is; fine if it wasn't there. Unavailable when it
        may be somewhere that can't be reached (it isn't gone)."""

    def list(self, prefix: str) -> Iterator[str]:
        """Every key starting with prefix, each once, in no order. Unavailable
        when some of them can't be reached (a list that isn't whole never is)."""

    def local_file(self, key: str) -> AbstractContextManager[Path]:
        """The key as a file on this machine, for a tool that needs a path
        (ffmpeg, libvips, ONNX), for as long as the block lasts. NotFound,
        Unavailable."""

    def writing(self, key: str, suffix: str = "") -> AbstractContextManager[Path]:
        """A path for a tool to write the key's file to (suffix: what the tool
        tells the format by); kept, as write keeps it, when the block ends
        without an error, and dropped otherwise. Unavailable."""


# -- Keys ---------------------------------------------------------------------------
#
# {tenant}/{library}/{kind}/{bucket}/{file}: bucket is the id's ULID % 100,
# two digits. Fixed: every row that names a key holds one of these.


def _bucket(ulid: str) -> int:
    return int(ULID.from_str(ulid)) % 100


def _stem(path: str) -> str:
    return Path(path).stem


def proxy_key(tenant_id: str, library_id: str, asset_id: str, original_filename: str) -> str:
    return (f"{tenant_id}/{library_id}/proxies/{_bucket(asset_id.removeprefix('ast_')):02d}"
            f"/{asset_id}_{_stem(original_filename)}.webp")


def thumbnail_key(tenant_id: str, library_id: str, asset_id: str, original_filename: str) -> str:
    return (f"{tenant_id}/{library_id}/thumbnails/{_bucket(asset_id.removeprefix('ast_')):02d}"
            f"/{asset_id}_{_stem(original_filename)}.webp")


def scene_rep_key(tenant_id: str, library_id: str, asset_id: str, rep_frame_ms: int) -> str:
    return (f"{tenant_id}/{library_id}/scenes/{_bucket(asset_id.removeprefix('ast_')):02d}"
            f"/{asset_id}_{rep_frame_ms:010d}.jpg")


def video_preview_key(tenant_id: str, library_id: str, asset_id: str, rel_path: str) -> str:
    """A video's preview: an MP4, whatever the original's extension."""
    return (f"{tenant_id}/{library_id}/previews/{_bucket(asset_id.removeprefix('ast_')):02d}"
            f"/{asset_id}_{_stem(rel_path)}.mp4")


def analysis_proxy_key(tenant_id: str, library_id: str, asset_id: str, rel_path: str) -> str:
    """A video's full-length analysis proxy (MP4)."""
    return (f"{tenant_id}/{library_id}/analysis/{_bucket(asset_id.removeprefix('ast_')):02d}"
            f"/{asset_id}_{_stem(rel_path)}.mp4")


def face_crop_key(tenant_id: str, library_id: str, face_id: str) -> str:
    return f"{tenant_id}/{library_id}/face_crops/{_bucket(face_id.removeprefix('face_')):02d}/{face_id}.webp"


def face_crop_key_of(tenant_id: str, library_id: str, face_id: str) -> str | None:
    """The face's crop key, or None for an id that isn't "face_" and a ULID
    (no crop was ever written for one)."""
    try:
        return face_crop_key(tenant_id, library_id, face_id)
    except ValueError:
        return None


def check_key(key: object) -> str:
    """The key, when it's one a Store takes: relative, `/`-separated, no `..`
    part, no NUL. ValueError (UnsafeKeyError) otherwise: never a path."""
    if not isinstance(key, str) or not key or "\x00" in key:
        raise UnsafeKeyError(f"Invalid storage key: {key!r}")
    rel = PurePosixPath(key)
    if rel.is_absolute() or key.startswith("\\") or ".." in rel.parts:
        raise UnsafeKeyError(f"Storage key leaves the previews folder: {key!r}")
    return key


# -- The install's store --------------------------------------------------------------


def get_store() -> Store:
    """The install's Store: the provider STORAGE_PROVIDER names, over the
    install's storage as it is now (get_storage: one for the process)."""
    from src.server.config import get_settings

    provider = get_settings().storage_provider
    if provider == "local":
        from src.server.storage.files import FileStore
        from src.server.storage.local import get_storage

        return FileStore(get_storage())
    raise ValueError(f"STORAGE_PROVIDER={provider!r}: the providers are {', '.join(PROVIDERS)}")
