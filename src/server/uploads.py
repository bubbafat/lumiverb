"""Uploads that survive a dropped connection (ADR-018 decision 9, phase 5).

A big file (an analysis proxy runs to hundreds of MB an hour) goes in
three steps: start, saying what it is (the clip, the artifact, its size,
the whole file's SHA-256 and how it was made); its bytes, in chunks, each
sent at the offset it begins at; then finish, where the server checks the
size and the hash, then checks and rewrites the video
(src/server/uploaded_video.py) and keeps it as a single upload would. A
dropped connection or a laptop asleep at 90% costs a chunk, never the file.

- The server counts bytes received, not chunks: a chunk sent again is
  taken for what's new in it, so a client resumes from `received` with
  whatever chunk size it uses now.
- Starting the same upload again (the same clip, artifact, size and hash,
  not finished) is that upload, with what it has: a client that restarted
  picks it up where it was, keeping nothing of its own (decision 13).
- The answer to a start says where the chunks go (chunk_url): this API
  here, object storage for a hosted server someday, with no client changing.
- An upload expires a day after its last chunk; upkeep deletes it and its
  bytes (delete_expired).

Its bytes wait in the server's own folder, DATA_DIR/uploads/<account>/,
until the finish keeps the file in the Store.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlmodel import Session
from ulid import ULID

from src.producers.names import ANALYSIS_PROXY, VIDEO_PREVIEW

# An upload expires this long after its last chunk.
TTL = timedelta(hours=24)
# The chunk size the server suggests, and the most it takes (Lumiverb's own nginx takes 100 MB a request).
CHUNK_SIZE = 32 * 1024 * 1024
MAX_CHUNK = 64 * 1024 * 1024
# What's sent this way: a clip's videos. Pictures are small enough for one request.
KINDS = (VIDEO_PREVIEW, ANALYSIS_PROXY)


def folder(tenant_id: str) -> Path:
    """Where the account's uploads wait, on the server's own disk."""
    from src.server.config import get_settings

    if not tenant_id or "/" in tenant_id or tenant_id.startswith("."):
        raise ValueError(f"not an account: {tenant_id!r}")
    return Path(get_settings().data_dir) / "uploads" / tenant_id


def part(tenant_id: str, upload_id: str) -> Path:
    """The upload's bytes so far."""
    if not upload_id.startswith("upl_") or "/" in upload_id:
        raise ValueError(f"not an upload: {upload_id!r}")
    return folder(tenant_id) / f"{upload_id}.part"


def chunk_file(tenant_id: str, upload_id: str) -> Path:
    """A path for a chunk while it arrives, before it's added (_add): nothing else writes there."""
    path = folder(tenant_id) / f"{upload_id}.{secrets.token_hex(8)}.chunk"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


_COLUMNS = "upload_id, asset_id, artifact, size, sha256, lineage, received, created_at, expires_at, finished_at, answer"


def get(session: Session, upload_id: str, *, lock: bool = False) -> dict | None:
    """The upload, locked until the transaction ends when lock."""
    row = session.execute(text(f"SELECT {_COLUMNS} FROM uploads WHERE upload_id = :u"  # noqa: S608 — fixed names
                               + (" FOR UPDATE" if lock else "")), {"u": upload_id}).mappings().first()
    return dict(row) if row is not None else None


def start(session: Session, tenant_id: str, *, asset_id: str, artifact: str, size: int, sha256: str,
          lineage: dict, now: datetime) -> dict:
    """The open upload of this file for this clip and artifact, or a new
    one (no commit). One picked up again counts only the bytes its folder
    still has (a disk replaced meanwhile starts it over)."""
    session.execute(text("""
        INSERT INTO uploads (upload_id, asset_id, artifact, size, sha256, lineage, received, created_at, expires_at)
        VALUES (:u, :a, :k, :size, :sha, CAST(:lineage AS jsonb), 0, :now, :expires)
        ON CONFLICT (asset_id, artifact, size, sha256) WHERE finished_at IS NULL DO NOTHING
    """), {"u": "upl_" + str(ULID()), "a": asset_id, "k": artifact, "size": size, "sha": sha256,
           "lineage": json.dumps(lineage), "now": now, "expires": now + TTL})
    row = session.execute(text(f"SELECT {_COLUMNS} FROM uploads WHERE asset_id = :a AND artifact = :k"  # noqa: S608
                               " AND size = :size AND sha256 = :sha AND finished_at IS NULL FOR UPDATE"),
                          {"a": asset_id, "k": artifact, "size": size, "sha": sha256}).mappings().one()
    upload = dict(row)
    path = part(tenant_id, upload["upload_id"])
    have = path.stat().st_size if path.exists() else 0
    received = min(upload["received"], have)
    session.execute(text("UPDATE uploads SET received = :r, lineage = CAST(:lineage AS jsonb), expires_at = :e"
                         " WHERE upload_id = :u"),
                    {"r": received, "lineage": json.dumps(lineage), "e": now + TTL, "u": upload["upload_id"]})
    return {**upload, "received": received, "lineage": lineage, "expires_at": now + TTL}


class GapError(Exception):
    """A chunk that begins past what the server has: send from received."""

    def __init__(self, received: int) -> None:
        super().__init__(f"the upload has {received} bytes: send from there")
        self.received = received


class TooBigError(Exception):
    """More bytes than the upload said it has."""


def add(session: Session, tenant_id: str, upload: dict, offset: int, chunk: Path, now: datetime) -> int:
    """Add what's new in a chunk that begins at offset to the locked
    upload's bytes (no commit): bytes it has already are skipped (a chunk
    sent again). Its received, as it is now. GapError for a chunk past what it
    has; TooBigError past its size."""
    received, size = upload["received"], upload["size"]
    length = chunk.stat().st_size
    if offset > received:
        raise GapError(received)
    if offset + length > size:
        raise TooBigError(f"the upload is {size} bytes: this chunk ends at {offset + length}")
    new = offset + length - received
    if new > 0:
        path = part(tenant_id, upload["upload_id"])
        with open(chunk, "rb") as src, open(path, "ab") as dest:
            dest.truncate(received)  # anything past received was never counted
            dest.seek(received)
            src.seek(received - offset)
            while data := src.read(1024 * 1024):
                dest.write(data)
            dest.flush()
            os.fsync(dest.fileno())
        received += new
    session.execute(text("UPDATE uploads SET received = :r, expires_at = :e WHERE upload_id = :u"),
                    {"r": received, "e": now + TTL, "u": upload["upload_id"]})
    return received


def whole_sha256(tenant_id: str, upload_id: str) -> str:
    hasher = hashlib.sha256()
    with open(part(tenant_id, upload_id), "rb") as f:
        while data := f.read(1024 * 1024):
            hasher.update(data)
    return hasher.hexdigest()


def finished(session: Session, upload_id: str, answer: dict[str, Any], now: datetime) -> None:
    """The upload is kept: its answer, for a finish sent again (no commit).
    It goes when it expires, a day on."""
    session.execute(text("UPDATE uploads SET finished_at = :now, expires_at = :e, answer = CAST(:a AS jsonb)"
                         " WHERE upload_id = :u"), {"now": now, "e": now + TTL, "a": json.dumps(answer), "u": upload_id})


def drop(session: Session, tenant_id: str, upload_id: str) -> bool:
    """Delete an upload and its bytes (no commit for the row). False when there's none."""
    gone = bool(session.execute(text("DELETE FROM uploads WHERE upload_id = :u"), {"u": upload_id}).rowcount)
    part(tenant_id, upload_id).unlink(missing_ok=True)
    return gone


def delete_expired(session: Session, tenant_id: str, now: datetime) -> int:
    """Delete the uploads expired by now, with their bytes (no commit), and
    bytes no upload claims that are older than a day (a crash's). How many uploads."""
    ids = list(session.execute(text("DELETE FROM uploads WHERE expires_at < :now RETURNING upload_id"),
                               {"now": now}).scalars())
    for upload_id in ids:
        part(tenant_id, upload_id).unlink(missing_ok=True)
    here = folder(tenant_id)
    if here.is_dir():
        live = set(session.execute(text("SELECT upload_id FROM uploads")).scalars())
        old = time.time() - TTL.total_seconds()
        for path in here.iterdir():
            if path.name.split(".", 1)[0] not in live and path.stat().st_mtime < old:
                path.unlink(missing_ok=True)
    return len(ids)
