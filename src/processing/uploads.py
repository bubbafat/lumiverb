"""Sending a clip's big video in chunks (ADR-018 decision 9; the server's
side is src/server/uploads.py).

Start (the file's size and SHA-256, and how it was made), then each chunk
at its offset from where the server says it is, then finish. Every call is
safe to send again, so the client (src/processing/api.py) tries each again
through a dropped connection or the API restarting, and one that went
wrong in the middle starts again where the server has got to: the same
file started again is the same upload. The client keeps nothing of its own
about it (decision 13).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from src.processing.api import LumiverbAPIError


def sha256_of(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while data := f.read(1024 * 1024):
            hasher.update(data)
    return hasher.hexdigest()


def send(client: Any, asset_id: str, artifact: str, path: Path, lineage: dict, *,
         chunk_size: int | None = None) -> dict:
    """Upload the file at path as the clip's video preview or analysis
    proxy, in chunks of the server's suggested size (or chunk_size, up to
    the server's most). The finish's answer: {key, sha256, duration_sec}.
    The file is sent by its name (ADR-024: lumiverb.video.analysis_proxy)."""

    size = path.stat().st_size
    upload = client.post(f"/v1/assets/{asset_id}/uploads", retry=True, json={
        "artifact": artifact, "size": size, "sha256": sha256_of(path), "lineage": lineage}).json()
    url, received = upload["chunk_url"], upload["received"]
    step = min(chunk_size or upload["chunk_size"], upload["max_chunk"])
    with open(path, "rb") as f:
        while received < size:
            f.seek(received)
            data = f.read(step)
            try:
                received = client.put(url, params={"offset": received}, content=data, retry=True,
                                      headers={"Content-Type": "application/octet-stream"}).json()["received"]
            except LumiverbAPIError as exc:
                if exc.code != "upload_gap":
                    raise
                received = client.get(url).json()["received"]  # the server has less: on from there
    return client.post(f"{url}/finish", retry=True).json()
