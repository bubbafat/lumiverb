"""Video playback: a signed link a <video> element can stream and seek.

A <video> element can't send an Authorization header, so GET
/v1/assets/{id}/playback hands out a short-lived link whose signature is
its credential. The link plays the video's analysis proxy (full length,
every audio track), or the 10-second preview scan makes until the proxy
exists. When the account caps playback at N seconds, the server serves only
the first N, cut without re-encoding and kept for the next request. Public
pages have their own cap (10 seconds unless an admin changes it), and their
transcripts stop where it does.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from src.server.api.dependencies import get_tenant_session
from src.server.config import get_settings
from src.server.models.tenant import Asset
from src.server.repository.tenant import AssetRepository, LibraryRepository
from src.server.storage.local import LocalStorage, get_storage
from src.server.tenant_settings import playback_cap

logger = logging.getLogger(__name__)

router = APIRouter(tags=["playback"])

# A copied link stops working within the hour; the player asks for a new
# one when a link fails mid-play.
STREAM_TTL_SECONDS = 3600
# Half-written cuts a killed API left behind.
_STALE_TEMP_SEC = 600
# A cut within this of the whole video isn't worth making.
_CAP_SLACK_SEC = 0.5

Source = Literal["analysis_proxy", "preview"]


class PlaybackResponse(BaseModel):
    url: str
    expires_at: str
    source: Source
    # Seconds the link plays for this viewer; None means the whole video.
    max_seconds: int | None


# ---------------------------------------------------------------------------
# Signed links
# ---------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(payload: str) -> str:
    secret = get_settings().jwt_secret
    if not secret:
        raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
    key = b"lumiverb-stream\0" + secret.encode()
    return _b64(hmac.new(key, payload.encode(), hashlib.sha256).digest())


def mint_stream_token(
    tenant_id: str,
    asset_id: str,
    *,
    ttl_seconds: int = STREAM_TTL_SECONDS,
    public_library_id: str | None = None,
    public_project_id: str | None = None,
) -> str:
    # "n": every link is new, so a renewed one reloads the player.
    claims: dict = {"t": tenant_id, "a": asset_id, "e": int(time.time()) + ttl_seconds,
                    "n": secrets.token_urlsafe(6)}
    if public_library_id:
        claims["pl"] = public_library_id
    if public_project_id:
        claims["pp"] = public_project_id
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{payload}.{_signature(payload)}"


def read_stream_token(token: str) -> dict | None:
    """The token's claims, or None if it's forged, malformed or expired."""
    payload, _, sig = token.partition(".")
    if not payload or not sig:
        return None
    try:
        if not hmac.compare_digest(sig, _signature(payload)):
            return None
        claims = json.loads(_unb64(payload))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict) or not isinstance(claims.get("e"), int) or claims["e"] < time.time():
        return None
    return claims


# ---------------------------------------------------------------------------
# What plays
# ---------------------------------------------------------------------------


def _check_public(session: Session, asset: Asset, library_id: str | None, project_id: str | None) -> None:
    """Raise unless a public page may show this asset."""
    if library_id:
        if asset.library_id != library_id:
            raise HTTPException(status_code=403, detail="Asset does not belong to the requested public library")
        lib = LibraryRepository(session).get_by_id(library_id)
        if lib is None or not lib.is_public:
            raise HTTPException(status_code=404, detail="Not found")
    elif project_id:
        from src.server.models.tenant import ProjectAsset
        from src.server.repository.tenant import ProjectRepository

        project = ProjectRepository(session).get_by_id(project_id)
        if project is None or project.visibility != "public":
            raise HTTPException(status_code=404, detail="Not found")
        member = session.exec(
            select(ProjectAsset).where(ProjectAsset.project_id == project_id, ProjectAsset.asset_id == asset.asset_id)
        ).first()
        if member is None:
            raise HTTPException(status_code=403, detail="Asset not in project")
    else:
        raise HTTPException(status_code=403, detail="Public access requires library or project context")


def _playable(asset: Asset, storage: LocalStorage) -> tuple[Source, Path, str] | None:
    """(source, file, version) of what plays: the analysis proxy, else the preview."""
    if asset.analysis_proxy_key:
        path = storage.abs_path(asset.analysis_proxy_key)
        if path.is_file():
            return "analysis_proxy", path, (asset.analysis_proxy_sha256 or "")[:12] or _file_version(path)
    if asset.video_preview_key:
        path = storage.abs_path(asset.video_preview_key)
        if path.is_file():
            return "preview", path, _file_version(path)
    return None


def _file_version(path: Path) -> str:
    st = path.stat()
    return f"{int(st.st_mtime)}-{st.st_size}"


def _duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        return float(json.loads(out)["format"]["duration"])
    except (subprocess.SubprocessError, OSError, ValueError, KeyError):
        return None


@functools.lru_cache(maxsize=8192)
def _served_duration(path: str, version: str) -> float | None:
    """The served file's own length, probed once per version of it."""
    return _duration(Path(path))


def _playback_dir(storage: LocalStorage, tenant_id: str) -> Path:
    return storage.abs_path(f"{tenant_id}/playback")


def capped(
    path: Path,
    max_seconds: int | None,
    *,
    storage: LocalStorage,
    tenant_id: str,
    asset_id: str,
    source: Source,
    version: str,
    strip: bool = False,
) -> Path:
    """`path`, or a copy of its first `max_seconds` when it runs longer.

    Copies are made with stream copy (no re-encoding, every track kept),
    without the file's metadata, and stored under {tenant}/playback,
    outside the library folders cleanup walks. Whether the file runs longer
    is the file's own say, not a stored duration that may be wrong; unknown
    means cut. `strip`: a public page's request, which never gets the
    original's metadata (GPS, say), even whole: previews made before scan
    stripped it still have it. Analysis proxies are rendered without it.
    """
    strip = strip and source == "preview"
    if max_seconds is not None:
        duration = _served_duration(str(path), version)
        if duration is not None and duration <= max_seconds + _CAP_SLACK_SEC:
            max_seconds = None
    if max_seconds is None and not strip:
        return path
    label = f"{max_seconds}s" if max_seconds is not None else "whole"
    cut = _playback_dir(storage, tenant_id) / f"{asset_id}_{source}_{version}_{label}.mp4"
    if cut.is_file():
        return cut
    cut.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=cut.parent, prefix=f".{asset_id}_", suffix=".mp4")
    os.close(fd)
    tmp = Path(tmp_name)
    failed = HTTPException(status_code=503, detail={"code": "playback_cut_failed",
                                                    "message": "Couldn't prepare the capped video"})
    length = ["-t", str(max_seconds)] if max_seconds is not None else []
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(path),
             "-map", "0:v", "-map", "0:a?", "-c", "copy", *length,
             "-map_metadata", "-1", "-map_chapters", "-1",
             "-movflags", "+faststart", "-f", "mp4", str(tmp)],
            capture_output=True, timeout=300, check=False,
        )
        if result.returncode != 0 or tmp.stat().st_size == 0:
            logger.warning("Couldn't cut %s to %s: %s", path, label,
                           result.stderr.decode(errors="replace")[-300:])
            raise failed
        tmp.replace(cut)
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Couldn't cut %s to %s: %s", path, label, exc)
        raise failed from exc
    finally:
        tmp.unlink(missing_ok=True)
    # Older versions of this cut, and temp files a killed API left.
    for old in cut.parent.glob(f"{asset_id}_{source}_*_{label}.mp4"):
        if old != cut:
            old.unlink(missing_ok=True)
    for tmp_old in cut.parent.glob(f".{asset_id}_*.mp4"):
        with contextlib.suppress(OSError):
            if time.time() - tmp_old.stat().st_mtime > _STALE_TEMP_SEC:
                tmp_old.unlink(missing_ok=True)
    return cut


def clear_cuts(tenant_id: str, asset_ids: list[str] | None = None) -> None:
    """Delete cuts: every one (a cap changed), or those of `asset_ids` (deleted for good)."""
    folder = _playback_dir(get_storage(), tenant_id)
    if not folder.is_dir():
        return
    doomed = None if asset_ids is None else set(asset_ids)
    for f in folder.iterdir():
        m = _CUT_NAME.match(f.name.lstrip("."))
        if doomed is None or (m is not None and m.group(1) in doomed):
            with contextlib.suppress(OSError):
                f.unlink(missing_ok=True)


_CUT_NAME = re.compile(r"^(ast_[0-9A-Z]{26})_")


def sweep_cuts(folder: Path, known_asset_ids: set[str], *, dry_run: bool) -> int:
    """Daily cleanup: cuts of assets that no longer exist, and stale temp files. Returns files removed."""
    if not folder.is_dir():
        return 0
    removed = 0
    for f in folder.iterdir():
        try:
            if f.name.startswith("."):
                gone = time.time() - f.stat().st_mtime > _STALE_TEMP_SEC
            else:
                m = _CUT_NAME.match(f.name)
                gone = m is None or m.group(1) not in known_asset_ids
            if gone:
                if not dry_run:
                    f.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            logger.warning("Playback cut sweep: %s: %s", f, exc)
    return removed


_SRT_START = re.compile(r"^\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->")


def srt_before(srt: str, seconds: int) -> str:
    """The SRT's entries that start before `seconds`."""
    kept = []
    for block in re.split(r"\n\s*\n", srt.strip()):
        for line in block.splitlines():
            m = _SRT_START.match(line)
            if m:
                h, mi, se, ms = (int(g) for g in m.groups())
                if h * 3600 + mi * 60 + se + ms / 1000 < seconds:
                    kept.append(block)
                break
    return "\n\n".join(kept) + ("\n" if kept else "")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/v1/assets/{asset_id}/playback", response_model=PlaybackResponse)
def get_playback(
    asset_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_tenant_session)],
) -> PlaybackResponse:
    """A signed link that streams this video, as far as the account's cap allows."""
    asset = AssetRepository(session).get_by_id(asset_id)
    if asset is None or asset.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Asset not found")
    public_library_id = public_project_id = None
    if getattr(request.state, "is_public_request", False):
        public_library_id = request.query_params.get("public_library_id")
        public_project_id = request.query_params.get("public_project_id")
        if public_library_id and public_project_id:
            raise HTTPException(status_code=400, detail="Give a public library or a public project, not both")
        _check_public(session, asset, public_library_id, public_project_id)
    if not asset.media_type.startswith("video"):
        raise HTTPException(status_code=422, detail="Playback is only for videos")

    playable = _playable(asset, get_storage())
    if playable is None:
        raise HTTPException(status_code=404, detail={"code": "nothing_to_play",
                                                     "message": "This video has no preview or proxy yet"})
    token = mint_stream_token(
        request.state.tenant_id, asset_id,
        public_library_id=public_library_id,
        public_project_id=public_project_id,
    )
    expires = datetime.fromtimestamp(read_stream_token(token)["e"], tz=UTC)
    return PlaybackResponse(
        url=f"/v1/stream/{token}",
        expires_at=expires.isoformat(),
        source=playable[0],
        max_seconds=playback_cap(session, public=bool(public_library_id or public_project_id)),
    )


@router.get("/v1/stream/{token}")
def stream(token: str, request: Request) -> StreamingResponse:
    """Stream what a playback link points at. The signature is the credential.

    Skips the tenant middleware: it resolves the tenant from the token.
    """
    from src.server.api.routers.assets import _stream_file_with_range
    from src.server.database import get_control_session, get_engine_for_url
    from src.server.repository.control_plane import TenantDbRoutingRepository

    claims = read_stream_token(token)
    if claims is None:
        raise HTTPException(status_code=403, detail={"code": "invalid_link", "message": "This link is invalid or expired"})
    tenant_id, asset_id = claims.get("t"), claims.get("a")
    if not isinstance(tenant_id, str) or not isinstance(asset_id, str):
        raise HTTPException(status_code=403, detail={"code": "invalid_link", "message": "This link is invalid or expired"})
    with get_control_session() as ctrl:
        routing = TenantDbRoutingRepository(ctrl).get_by_tenant_id(tenant_id)
    if routing is None:
        raise HTTPException(status_code=404, detail="Not found")

    with Session(get_engine_for_url(routing.connection_string)) as session:
        asset = AssetRepository(session).get_by_id(asset_id)
        if asset is None or asset.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Asset not found")
        if claims.get("pl") or claims.get("pp"):
            try:
                _check_public(session, asset, claims.get("pl"), claims.get("pp"))
            except HTTPException as exc:
                raise HTTPException(status_code=404, detail="Not found") from exc
        storage = get_storage()
        playable = _playable(asset, storage)
        if playable is None:
            raise HTTPException(status_code=404, detail="Nothing to play")
        source, path, version = playable
        max_seconds = playback_cap(session, public=bool(claims.get("pl") or claims.get("pp")))

    path = capped(path, max_seconds, storage=storage, tenant_id=tenant_id, asset_id=asset_id,
                  source=source, version=version, strip=bool(claims.get("pl") or claims.get("pp")))
    # Names the bytes: a cap change or a new proxy mid-play mustn't splice two files.
    etag = f'"{source}-{version}-{max_seconds or "whole"}"'
    response = _stream_file_with_range(path, request, media_type="video/mp4", etag=etag)
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response
