"""ArtifactStore: unified interface for reading and writing media artifacts.

Provides two implementations:
  LocalArtifactStore  — wraps LocalStorage; reads/writes directly to DATA_DIR (server-side).
  RemoteArtifactStore — calls the upload/download artifact API endpoints (CLI workers).

CLI workers always use RemoteArtifactStore. LocalArtifactStore is used server-side
by the API artifact endpoints.
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from src.cli.client import LumiverbClient
    from src.storage.local import LocalStorage


@dataclass
class ArtifactRef:
    """Return value from write_artifact: the computed storage key and sha256 of the bytes."""

    key: str
    sha256: str


class ArtifactStore(Protocol):
    """Protocol satisfied by both LocalArtifactStore and RemoteArtifactStore."""

    def write_artifact(
        self,
        artifact_type: str,
        asset_id: str,
        data: bytes,
        *,
        library_id: str,
        rel_path: str,
        rep_frame_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> ArtifactRef:
        """Write artifact bytes and return the storage key and sha256.

        artifact_type must be one of: proxy, thumbnail, video_preview, scene_rep.
        For scene_rep, rep_frame_ms is required.
        width/height are persisted only for proxy artifacts.
        """
        ...

    def read_artifact(
        self,
        key: str,
        *,
        asset_id: str,
        artifact_type: str,
        rep_frame_ms: int | None = None,
    ) -> bytes:
        """Read artifact bytes.

        key is the opaque storage key (e.g. "t1/lib1/proxies/07/ast_01JX_photo.jpg").
        asset_id and artifact_type are used by RemoteArtifactStore to call the correct
        API endpoint; LocalArtifactStore uses key directly and ignores the hints.
        For scene_rep in remote mode, rep_frame_ms is required.
        """
        ...


class LocalArtifactStore:
    """Reads and writes artifacts directly to the local DATA_DIR via LocalStorage."""

    def __init__(self, storage: "LocalStorage", tenant_id: str) -> None:
        self._storage = storage
        self._tenant_id = tenant_id

    def write_artifact(
        self,
        artifact_type: str,
        asset_id: str,
        data: bytes,
        *,
        library_id: str,
        rel_path: str,
        rep_frame_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> ArtifactRef:
        if artifact_type == "proxy":
            key = self._storage.proxy_key(self._tenant_id, library_id, asset_id, rel_path)
        elif artifact_type == "thumbnail":
            key = self._storage.thumbnail_key(self._tenant_id, library_id, asset_id, rel_path)
        elif artifact_type == "video_preview":
            key = self._storage.video_preview_key(self._tenant_id, library_id, asset_id, rel_path)
        elif artifact_type == "scene_rep":
            if rep_frame_ms is None:
                raise ValueError("rep_frame_ms is required for scene_rep artifacts")
            key = self._storage.scene_rep_key(
                self._tenant_id, library_id, asset_id, rep_frame_ms
            )
        else:
            raise ValueError(f"Unknown artifact_type: {artifact_type!r}")

        sha256 = hashlib.sha256(data).hexdigest()
        self._storage.write(key, data)
        return ArtifactRef(key=key, sha256=sha256)

    def read_artifact(
        self,
        key: str,
        *,
        asset_id: str,
        artifact_type: str,
        rep_frame_ms: int | None = None,
    ) -> bytes:
        # asset_id / artifact_type / rep_frame_ms are unused in local mode.
        return self._storage.abs_path(key).read_bytes()


class RemoteArtifactStore:
    """Reads and writes artifacts via the Lumiverb artifact API endpoints."""

    def __init__(self, client: "LumiverbClient") -> None:
        self._client = client

    def write_artifact(
        self,
        artifact_type: str,
        asset_id: str,
        data: bytes,
        *,
        library_id: str,
        rel_path: str,
        rep_frame_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> ArtifactRef:
        # library_id and rel_path are unused in remote mode; the server computes the key.
        files = {"file": ("artifact", io.BytesIO(data), "application/octet-stream")}
        form: dict[str, str] = {}
        if width is not None:
            form["width"] = str(width)
        if height is not None:
            form["height"] = str(height)
        if rep_frame_ms is not None:
            form["rep_frame_ms"] = str(rep_frame_ms)

        resp = self._client.post(
            f"/v1/assets/{asset_id}/artifacts/{artifact_type}",
            files=files,
            data=form,
        )
        body = resp.json()
        return ArtifactRef(key=body["key"], sha256=body["sha256"])

    def read_artifact(
        self,
        key: str,
        *,
        asset_id: str,
        artifact_type: str,
        rep_frame_ms: int | None = None,
    ) -> bytes:
        # key is unused in remote mode; the download endpoint is addressed by asset_id + type.
        params: dict[str, int] | None = None
        if artifact_type == "scene_rep":
            if rep_frame_ms is None:
                raise ValueError("rep_frame_ms is required for scene_rep artifacts")
            params = {"rep_frame_ms": rep_frame_ms}
        resp = self._client.get(f"/v1/assets/{asset_id}/artifacts/{artifact_type}", params=params)
        return resp.content


def get_artifact_store(
    mode: str,
    *,
    storage: "LocalStorage | None" = None,
    client: "LumiverbClient | None" = None,
    tenant_id: str | None = None,
) -> LocalArtifactStore | RemoteArtifactStore:
    """Factory: return the appropriate ArtifactStore for the given mode.

    mode="local"  → LocalArtifactStore (requires storage and tenant_id)
    mode="remote" → RemoteArtifactStore (requires client)
    """
    if mode == "remote":
        if client is None:
            raise ValueError("client is required for remote artifact store")
        return RemoteArtifactStore(client=client)
    else:
        if storage is None or tenant_id is None:
            raise ValueError("storage and tenant_id are required for local artifact store")
        return LocalArtifactStore(storage=storage, tenant_id=tenant_id)
