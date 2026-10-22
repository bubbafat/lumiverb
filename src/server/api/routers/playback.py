"""Video playback: a signed link a <video> element can stream and seek.

A <video> element can't send an Authorization header, so GET
/v1/assets/{id}/playback hands out a short-lived link whose signature is
its credential. The link plays the video's analysis proxy (full length,
every audio track), or the 10-second preview scan makes until the proxy
exists. When the account caps playback at N seconds, the server serves only
the first N, cut without re-encoding and kept for the next request. Public
pages have their own cap (10 seconds unless an admin changes it), and their
transcripts stop where it does. A Live Photo's link plays its motion, the
few seconds around it (_what_plays), as the photo's.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session

from src.processing.video import ffmpeg_run
from src.producers.names import ANALYSIS_PROXY, VIDEO_PREVIEW
from src.server.api.dependencies import get_tenant_session
from src.server.api.errors import ApiError
from src.server.config import get_settings
from src.server.models.tenant import Asset
from src.server.repository.assets import AssetRepository
from src.server.repository.libraries import LibraryRepository
from src.server.storage.cache import FileCache, playback_cuts
from src.server.storage.store import NotFound, Store, Unavailable, get_store
from src.server.tenant_settings import playback_cap
from src.shared.secrets_env import without_secrets

logger = logging.getLogger(__name__)

router = APIRouter(tags=["playback"])

# A copied link stops working within the hour; the player asks for a new
# one when a link fails mid-play. A signed-in person's stops sooner, at
# their next request after they can't see the clip any more (_still_theirs).
STREAM_TTL_SECONDS = 3600
# A cut within this of the whole video isn't worth making.
_CAP_SLACK_SEC = 0.5

Source = Literal[ANALYSIS_PROXY, VIDEO_PREVIEW]


class PlaybackResponse(BaseModel):
    url: str
    expires_at: str
    source: Source
    # Seconds the link plays for this viewer; None means the whole video.
    max_seconds: int | None
    # False while the capped copy is made in the background: ask again in a
    # couple of seconds (the link answers 503 playback_preparing until then).
    ready: bool
    # What's served is the whole clip: the analysis proxy, or a preview as
    # long as a clip no longer than it (whole_clip). Players say so, rather
    # than "a short preview" until it's processed (Robert, a 1-second clip).
    whole: bool = False


def whole_clip(source: str, duration_sec: float | None) -> bool:
    """Whether `source` holds the whole clip: the analysis proxy does; a
    preview is the clip's first seconds (the scan's, video_preview's length),
    so a clip no longer than that is whole in it."""
    from src.shared.producers import PRODUCERS

    if source == ANALYSIS_PROXY:
        return True
    preview_seconds = PRODUCERS[VIDEO_PREVIEW].defaults["seconds"]
    return duration_sec is not None and duration_sec <= preview_seconds + WHOLE_SLACK_SEC


# A clip's length and its preview's differ by a frame or two (rounding).
WHOLE_SLACK_SEC = 0.1


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
    user_id: str | None = None,
    token_version: int | None = None,
    key_id: str | None = None,
) -> str:
    """A signed link: a public page's (pl, pp), or who it was made for, a
    person signed in (u, their token version tv) or a key (k), each asked
    again at every request (stream)."""
    # "n": every link is new, so a renewed one reloads the player.
    claims: dict = {"t": tenant_id, "a": asset_id, "e": int(time.time()) + ttl_seconds,
                    "n": secrets.token_urlsafe(6)}
    if public_library_id:
        claims["pl"] = public_library_id
    if public_project_id:
        claims["pp"] = public_project_id
    if user_id:
        claims["u"], claims["tv"] = user_id, token_version
    if key_id:
        claims["k"] = key_id
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
        from src.server.api.library_access import sight_of
        from src.server.repository.projects import ProjectRepository

        project = ProjectRepository(session).get_by_id(project_id)
        if project is None or project.visibility != "public":
            raise HTTPException(status_code=404, detail="Not found")
        # What its owner can still see of it (ADR-018 decision 8).
        projects = ProjectRepository(session, within=sight_of(session, project.owner_user_id))
        if not projects.has_asset(project_id, asset.asset_id):
            raise HTTPException(status_code=403, detail="Asset not in project")
    else:
        raise HTTPException(status_code=403, detail="Public access requires library or project context")


def _playable(asset: Asset, store: Store) -> tuple[Source, str, str] | None:
    """(source, key, version) of what plays: the analysis proxy, else the
    preview. Unavailable when where they're kept can't be reached."""
    for source, key in ((ANALYSIS_PROXY, asset.analysis_proxy_key), (VIDEO_PREVIEW, asset.video_preview_key)):
        if not key:
            continue
        try:
            st = store.stat(key)
        except NotFound:
            continue
        sha = (asset.analysis_proxy_sha256 or "")[:12] if source == ANALYSIS_PROXY else ""
        return source, key, sha or f"{int(st.modified)}-{st.size}"
    return None


def _duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", *ffmpeg_run.input_args(), "-show_entries", "format=duration", "-of", "json",
             str(path)],
            capture_output=True, text=True, timeout=30, check=True, env=without_secrets(os.environ),
        ).stdout
        return float(json.loads(out)["format"]["duration"])
    except (subprocess.SubprocessError, OSError, ValueError, KeyError):
        return None


@dataclass(frozen=True)
class _Ask:
    """What a request asks to play: the stored file `key`, within
    `max_seconds`, stripped or not. Its cuts, and its probed length, are
    kept in `cuts` (the account's Cache)."""
    store: Store
    key: str
    max_seconds: int | None
    cuts: FileCache
    asset_id: str
    source: Source
    version: str
    strip: bool


def _duration_key(ask: _Ask) -> str:
    """Where the served file's probed length is kept, beside its cuts: so
    every API worker knows it (a cut that isn't needed leaves no file of its
    own). Cleared with the cuts."""
    return f"{ask.asset_id}_{ask.source}_{ask.version}.duration"


def _known_duration(ask: _Ask) -> tuple[bool, float | None]:
    """(probed yet, its length; None when unreadable). Never probes: probing is the cut job's."""
    try:
        text = ask.cuts.read(_duration_key(ask)).decode().strip()
    except (OSError, UnicodeDecodeError):  # NotFound: not probed yet (or let go of)
        return False, None
    try:
        return True, float(text) if text else None
    except ValueError:
        return False, None


def _probe(ask: _Ask) -> None:
    with ask.store.local_file(ask.key) as source:
        duration = _duration(source)
    key = _duration_key(ask)
    ask.cuts.write(key, b"" if duration is None else repr(duration).encode())
    for old in list(ask.cuts.list(f"{ask.asset_id}_{ask.source}_")):
        if old.endswith(".duration") and old != key:
            ask.cuts.delete(old)


def _cut_for(ask: _Ask) -> tuple[str, int | None, str] | None:
    """(cut's key, its length, label) to serve instead of the file; None: the file itself.

    A file known to be no longer than the cap needs no cut; one not probed
    yet is planned as cut (the job probes before it cuts)."""
    strip = ask.strip and ask.source == VIDEO_PREVIEW
    max_seconds = ask.max_seconds
    if max_seconds is not None:
        probed, duration = _known_duration(ask)
        if probed and duration is not None and duration <= max_seconds + _CAP_SLACK_SEC:
            max_seconds = None
    if max_seconds is None and not strip:
        return None
    label = f"{max_seconds}s" if max_seconds is not None else "whole"
    return f"{ask.asset_id}_{ask.source}_{ask.version}_{label}.mp4", max_seconds, label


# Cuts are made off the request, at most _CUT_WORKERS at once and
# _MAX_WAITING queued or running; a request for one that isn't ready gets a
# 503 playback_preparing with Retry-After. One that failed isn't tried again
# for _FAILED_HOLD_SEC (503 playback_cut_failed meanwhile).
_CUT_WORKERS = 2
_MAX_WAITING = 16
_FAILED_HOLD_SEC = 60.0
RETRY_AFTER_SEC = 2
_CUT_TIMEOUT_SEC = 300
_executor = ThreadPoolExecutor(max_workers=_CUT_WORKERS, thread_name_prefix="playback-cut")
_running: dict[str, Future] = {}
_failed: dict[str, float] = {}
_jobs_lock = threading.Lock()
# The ffmpegs cutting now, killed when the API stops (stop_cuts): the
# executor's threads are joined at exit, and a cut can run _CUT_TIMEOUT_SEC.
_procs: set[subprocess.Popen] = set()
_stopping = threading.Event()


class _CutFailed(Exception):
    pass


def start_cuts() -> None:
    """The API's start: cuts may run (again)."""
    _stopping.clear()


def stop_cuts() -> None:
    """The API's stop: queued cuts are dropped and running ffmpegs killed,
    so the executor's threads finish at once."""
    with _jobs_lock:
        _stopping.set()
        for future in _running.values():
            future.cancel()  # only one not started yet
        procs = list(_procs)
    for proc in procs:
        with contextlib.suppress(OSError):
            proc.kill()


def _make_cut(ask: _Ask) -> None:
    """Probe the file when it hasn't been, then make the cut it still needs.

    Cuts are made with stream copy (no re-encoding, every track kept),
    without the file's metadata, and kept in the account's playback Cache
    (src/server/storage/cache.py playback_cuts)."""
    if ask.max_seconds is not None and not _known_duration(ask)[0]:
        _probe(ask)
    planned = _cut_for(ask)
    if planned is None or ask.cuts.exists(planned[0]):
        return
    cut, max_seconds, label = planned
    length = ["-t", str(max_seconds)] if max_seconds is not None else []
    try:
        with ask.store.local_file(ask.key) as source, ask.cuts.writing(cut, suffix=".mp4") as out:
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *ffmpeg_run.input_args(),
                   "-i", str(source),
                   "-map", "0:v", "-map", "0:a?", "-c", "copy", *length,
                   "-map_metadata", "-1", "-map_chapters", "-1",
                   "-movflags", "+faststart", "-f", "mp4", str(out)]
            if _stopping.is_set():
                raise _CutFailed("the API is stopping")
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    env=without_secrets(os.environ))
            with _jobs_lock:
                _procs.add(proc)
                stopping = _stopping.is_set()  # stop_cuts came between: it didn't see this one
            try:
                if stopping:
                    proc.kill()
                _, stderr = proc.communicate(timeout=_CUT_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            finally:
                with _jobs_lock:
                    _procs.discard(proc)
            if proc.returncode != 0 or not out.is_file() or out.stat().st_size == 0:
                raise _CutFailed(stderr.decode(errors="replace")[-300:])
    except (subprocess.SubprocessError, OSError) as exc:
        raise _CutFailed(str(exc)) from exc
    # Older versions of this cut.
    for old in list(ask.cuts.list(f"{ask.asset_id}_{ask.source}_")):
        if old.endswith(f"_{label}.mp4") and old != cut:
            ask.cuts.delete(old)


def _run_cut(ask: _Ask, cut: str) -> None:
    try:
        _make_cut(ask)
    except Exception as exc:  # noqa: BLE001 — remembered, and the next request says so
        logger.warning("Couldn't cut %s for %s: %s", ask.key, cut, exc)
        with _jobs_lock:
            _failed[cut] = time.monotonic()
    finally:
        with _jobs_lock:
            _running.pop(cut, None)


def capped(
    store: Store,
    key: str,
    max_seconds: int | None,
    *,
    tenant_id: str,
    asset_id: str,
    source: Source,
    version: str,
    strip: bool = False,
) -> tuple[Store, str]:
    """What to stream: (store, key) itself, or (the account's cuts, its cut:
    its first `max_seconds` when it runs longer) once the cut is ready.
    Never cuts in the request: a cut that isn't ready is started in the
    background and the request gets a 503 playback_preparing with Retry-After.

    Whether the file runs longer is the file's own say, not a stored
    duration that may be wrong. `strip`: a public page's request, which
    never gets the original's metadata (GPS, say), even whole: previews made
    before scan stripped it still have it. Analysis proxies are rendered
    without it.
    """
    ask = _Ask(store, key, max_seconds, playback_cuts(tenant_id), asset_id, source, version, strip)
    planned = _cut_for(ask)
    if planned is None:
        return store, key
    cut = planned[0]
    if ask.cuts.exists(cut):
        return ask.cuts, cut
    with _jobs_lock:
        failed_at = _failed.get(cut)
        if failed_at is not None and time.monotonic() - failed_at < _FAILED_HOLD_SEC:
            raise ApiError(503, "playback_cut_failed", "Couldn't prepare the capped video")
        _failed.pop(cut, None)
        if cut not in _running and len(_running) < _MAX_WAITING:
            _running[cut] = _executor.submit(_run_cut, ask, cut)
    raise ApiError(503, "playback_preparing", "Preparing the video; try again shortly.",
                   headers={"Retry-After": str(RETRY_AFTER_SEC)})


def is_ready(store: Store, key: str, max_seconds: int | None, **where) -> bool:
    """capped(), for a caller that only asks whether it's ready (starting it if not)."""
    try:
        capped(store, key, max_seconds, **where)
    except ApiError as exc:
        if exc.code == "playback_preparing":
            return False
        raise
    return True


def clear_cuts(tenant_id: str, asset_ids: list[str] | None = None) -> None:
    """Delete cuts: every one (a cap changed), or those of `asset_ids` (deleted for good)."""
    cuts = playback_cuts(tenant_id)
    doomed = None if asset_ids is None else set(asset_ids)
    try:
        for key in list(cuts.list("")):
            m = _CUT_NAME.match(key)
            if doomed is None or (m is not None and m.group(1) in doomed):
                cuts.delete(key)
    except Unavailable as exc:  # made again on demand; the daily sweep takes those of clips gone
        logger.warning("Couldn't clear playback cuts of %s: %s", tenant_id, exc)


_CUT_NAME = re.compile(r"^(ast_[0-9A-Z]{26})_")


def sweep_cuts(cuts: FileCache, known_asset_ids: set[str], *, dry_run: bool) -> int:
    """Daily cleanup: cuts of assets that no longer exist, and writes a dead
    process left (the Cache's own). Returns files removed."""
    try:
        removed = 0
        for key in list(cuts.list("")):
            m = _CUT_NAME.match(key)
            if m is None or m.group(1) not in known_asset_ids:
                if not dry_run:
                    cuts.delete(key)
                removed += 1
        return removed + cuts.evict(dry_run=dry_run)
    except Unavailable as exc:
        logger.warning("Playback cut sweep: %s", exc)
        return 0


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
    plays = _what_plays(session, asset)
    if plays is None:
        raise HTTPException(status_code=422, detail="Playback is only for videos and Live Photos")

    store = get_store()
    playable = _playable(plays, store)
    if playable is None:
        raise ApiError(404, "nothing_to_play", "This video has no preview or proxy yet")
    public = bool(public_library_id or public_project_id)
    max_seconds = playback_cap(session, public=public)
    source, key, version = playable
    ready = is_ready(store, key, max_seconds, tenant_id=request.state.tenant_id, asset_id=asset_id,
                     source=source, version=version, strip=public)
    token = mint_stream_token(
        request.state.tenant_id, asset_id,
        public_library_id=public_library_id,
        public_project_id=public_project_id,
        user_id=None if public else getattr(request.state, "user_id", None),
        token_version=getattr(request.state, "token_version", None),
        key_id=None if public else getattr(request.state, "key_id", None),
    )
    expires = datetime.fromtimestamp(read_stream_token(token)["e"], tz=UTC)
    return PlaybackResponse(
        url=f"/v1/stream/{token}",
        expires_at=expires.isoformat(),
        source=source,
        max_seconds=max_seconds,
        ready=ready,
        whole=whole_clip(source, plays.duration_sec),
    )


def _what_plays(session: Session, asset: Asset) -> Asset | None:
    """What a clip's playback plays: a video itself; a Live Photo its motion, the few seconds around it
    (src/server/repository/live_photos.py), out of sight as part of it, so the photo's own access is the
    link's; anything else nothing."""
    if asset.media_type.startswith("video"):
        return asset
    from src.server.repository.live_photos import LivePhotoRepository

    return LivePhotoRepository(session).motion_of(asset)


def _still_theirs(session: Session, claims: dict, tenant_id: str, asset: Asset) -> bool:
    """Whoever a signed-in link was made for can still play the clip: the
    person as they are now (signed out everywhere since, by a new password
    or role, ends it: their token version moved on), or the key (revoked
    ends it), and either can still see the clip's library (ADR-018 decision
    8: a role taken away ends it at once). A link that names nobody, made
    before links did, plays no more: the player asks for a new one."""
    from src.server.database import get_control_session
    from src.server.repository.control_plane import ApiKeyRepository, UserRepository
    from src.server.repository.library_roles import (
        LibraryRoleRepository,
        MakerGoneError,
        person_of_key,
        person_of_user,
    )

    with get_control_session() as ctrl:
        users = UserRepository(ctrl)
        if isinstance(claims.get("k"), str):
            key = ApiKeyRepository(ctrl).get(claims["k"])
            if key is None or key.revoked_at is not None or key.tenant_id != tenant_id:
                return False
            try:
                person = person_of_key(key, users)
            except MakerGoneError:
                return False
        elif isinstance(claims.get("u"), str):
            user = users.get_by_id(claims["u"])
            if user is None or user.tenant_id != tenant_id or user.token_version != claims.get("tv"):
                return False
            person = person_of_user(user)
        else:
            return False
    if person is None or person.account_admin:
        return True
    return LibraryRoleRepository(session).role_on(person, asset.library_id) not in (None, "none")


@router.get("/v1/stream/{token}")
def stream(token: str, request: Request) -> StreamingResponse:
    """Stream what a playback link points at. The signature is the credential.

    Skips the tenant middleware: it resolves the tenant from the token, and
    refuses a deleted account's link as the middleware refuses its every
    request (401 account_deleted; a public page's, 404 as one that isn't
    there).
    """
    from src.server.api.streaming import stream_stored
    from src.server.database import get_control_session, get_engine_for_url
    from src.server.repository.control_plane import TenantDbRoutingRepository, TenantRepository

    claims = read_stream_token(token)
    if claims is None:
        raise ApiError(403, "invalid_link", "This link is invalid or expired")
    tenant_id, asset_id = claims.get("t"), claims.get("a")
    if not isinstance(tenant_id, str) or not isinstance(asset_id, str):
        raise ApiError(403, "invalid_link", "This link is invalid or expired")
    with get_control_session() as ctrl:
        gone = TenantRepository(ctrl).gone(tenant_id)
        routing = TenantDbRoutingRepository(ctrl).get_by_tenant_id(tenant_id)
    if routing is None or gone and (claims.get("pl") or claims.get("pp")):
        raise HTTPException(status_code=404, detail="Not found")
    if gone:
        raise ApiError(401, "account_deleted", "This account was deleted")

    with Session(get_engine_for_url(routing.connection_string)) as session:
        asset = AssetRepository(session).get_by_id(asset_id)
        if asset is None or asset.deleted_at is not None:
            raise HTTPException(status_code=404, detail="Asset not found")
        if claims.get("pl") or claims.get("pp"):
            try:
                _check_public(session, asset, claims.get("pl"), claims.get("pp"))
            except HTTPException as exc:
                raise HTTPException(status_code=404, detail="Not found") from exc
        elif not _still_theirs(session, claims, tenant_id, asset):
            raise ApiError(403, "invalid_link", "This link is invalid or expired")
        store = get_store()
        plays = _what_plays(session, asset)
        playable = _playable(plays, store) if plays is not None else None
        if playable is None:
            raise HTTPException(status_code=404, detail="Nothing to play")
        source, key, version = playable
        max_seconds = playback_cap(session, public=bool(claims.get("pl") or claims.get("pp")))

    where, key = capped(store, key, max_seconds, tenant_id=tenant_id, asset_id=asset_id,
                        source=source, version=version, strip=bool(claims.get("pl") or claims.get("pp")))
    # Names the bytes: a cap change or a new proxy mid-play mustn't splice two files.
    etag = f'"{source}-{version}-{max_seconds or "whole"}"'
    try:
        response = stream_stored(where, key, request, media_type="video/mp4", etag=etag)
    except NotFound:  # gone between looking and opening (a cut let go of, a re-render)
        raise ApiError(503, "playback_preparing", "Preparing the video; try again shortly.",
                       headers={"Retry-After": str(RETRY_AFTER_SEC)}) from None
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response
