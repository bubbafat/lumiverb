"""Scan phase: discover files, hash, extract EXIF, generate proxies, upload.

Scan is the only operation that touches source files. It produces three
outputs per file: a server-side asset record, a server-side 2048px proxy,
and a local proxy cache entry with SHA sidecar.

Change detection compares local SHA-256 against server-stored values to
classify files as new, changed, unchanged, or deleted.

See docs/archive/011-ingest-refactor-scan-and-enrich.md for full design.
"""

from __future__ import annotations

import io
import json
import contextlib
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from stat import S_ISDIR

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)

from src.processing.api import ApiClient, LumiverbAPIError
from src.processing.roots import local_library_root, reachable_root
from src.processing.ingest import (
    SUPPORTED_EXTENSIONS,
    _build_exif_payload,
    _detect_media_type,
    _extract_video_poster,
    _generate_proxy_bytes,
    _generate_video_preview,
    _jpeg_to_webp,
    _load_library_filters,
    _load_tenant_filters,
    _walk_library,
)
from src.processing.proxy.proxy_cache import ProxyCache
from src.processing.workers.exif_extract import compute_sha256
from src.processing.video.probe import probe_video
from src.shared.io_utils import UnsafeRelPathError, is_within, resolve_source_path, stat_if_present
from src.shared.producers import effective_settings, lineage as producer_lineage

logger = logging.getLogger(__name__)


# The macOS scanner's quarantine: a file modified this recently may still be
# copying.
SETTLE_SEC = 30
# A file stamped further ahead than this came from a camera whose clock runs
# ahead, not from a copy in progress.
FUTURE_MTIME_SEC = 300


@dataclass
class ScanStats:
    lock: threading.Lock = field(default_factory=threading.Lock)
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    moved: int = 0
    cache_populated: int = 0
    failed: int = 0
    failed_paths: list[str] = field(default_factory=list)
    scanned_asset_ids: list[str] = field(default_factory=list)
    # The library's root couldn't be read, so nothing was scanned.
    root_unreachable: bool = False
    # Files written too recently to trust: left for a later scan.
    settling: int = 0
    settling_paths: list[str] = field(default_factory=list)
    # Folders that couldn't be listed, so nothing was taken for deleted.
    unlisted: list[str] = field(default_factory=list)


@dataclass
class _ServerAsset:
    asset_id: str
    sha256: str | None
    file_size: int | None = None
    file_mtime: str | None = None  # ISO8601 string from server
    media_type: str | None = None


def _fetch_existing_assets_with_sha(
    client: ApiClient, library_id: str,
) -> dict[str, _ServerAsset]:
    """Page through all assets on the server. Returns {rel_path: _ServerAsset}."""
    existing: dict[str, _ServerAsset] = {}
    cursor: str | None = None
    while True:
        params: dict[str, str] = {
            "library_id": library_id, "limit": "500",
            "sort": "asset_id", "dir": "asc",
        }
        if cursor:
            params["after"] = cursor
        resp = client.get("/v1/assets/page", params=params)
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        for a in items:
            existing[a["rel_path"]] = _ServerAsset(
                asset_id=a["asset_id"],
                sha256=a.get("sha256"),
                file_size=a.get("file_size"),
                file_mtime=a.get("file_mtime"),
                media_type=a.get("media_type"),
            )
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return existing


def _fetch_ignored_paths(client: ApiClient, library_id: str) -> dict[str, list[_ServerAsset] | None]:
    """Files a person trashed, archived or deleted for good, by rel_path: each
    one's content, size and time, or None for whatever is at the path. Scans
    skip them: Lumiverb never deletes originals, so they may still be on
    disk. Another file at the path is a new clip."""
    ignored: dict[str, list[_ServerAsset] | None] = {}
    cursor: str | None = None
    while True:
        params: dict[str, str] = {"limit": "1000"}
        if cursor:
            params["after"] = cursor
        data = client.get(f"/v1/libraries/{library_id}/ignored-paths", params=params).json()
        for item in data.get("items", []):
            files = item.get("files")
            ignored[item["rel_path"]] = None if files is None else [
                _ServerAsset(asset_id="", sha256=f["sha256"], file_size=f.get("file_size"),
                             file_mtime=f.get("file_mtime"))
                for f in files
            ]
        cursor = data.get("next_cursor")
        if not cursor:
            return ignored


def _skipped(f: dict, ignored: dict[str, list[_ServerAsset] | None], root_path: Path) -> bool:
    """Whether this file is one a person removed. Hashed only when it isn't
    one of them untouched (same size and time), so archives kept on disk
    aren't read on every scan."""
    if f["rel_path"] not in ignored:
        return False
    removed = ignored[f["rel_path"]]
    if removed is None:
        return True
    if any(_mtime_size_match(f, r) for r in removed):
        return True
    sha = compute_sha256(resolve_source_path(root_path, f["rel_path"]))
    return sha is None or any(sha == r.sha256 for r in removed)


def _split_files(
    local_files: list[dict],
    existing: dict[str, _ServerAsset],
    *,
    thorough: bool = False,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split files into new, needs-hash, and fast-unchanged.

    New files can start scanning immediately. Needs-hash files require
    SHA comparison before we know if they're changed or unchanged.

    When thorough=False (default), files whose mtime and size match the
    server are classified as fast-unchanged without hashing. When
    thorough=True, all existing files go through SHA comparison.

    Returns (new_files, needs_hash_files, fast_unchanged_files).
    """
    new_files: list[dict] = []
    needs_hash: list[dict] = []
    fast_unchanged: list[dict] = []
    for f in local_files:
        server = existing.get(f["rel_path"])
        if server is None:
            new_files.append(f)
        elif not thorough and _mtime_size_match(f, server):
            f["asset_id"] = server.asset_id
            fast_unchanged.append(f)
        else:
            f["_server"] = server
            needs_hash.append(f)
    return new_files, needs_hash, fast_unchanged


def _mtime_size_match(local_file: dict, server: _ServerAsset) -> bool:
    """Check if local file's mtime and size match the server asset."""
    if server.file_size is None or server.file_mtime is None:
        return False
    if local_file["file_size"] != server.file_size:
        return False
    # Compare instants, not strings: the server answers in its database's
    # timezone (08:00-04:00 on a box set to New York), the scan in UTC.
    local_mtime = local_file.get("file_mtime")
    if local_mtime is None:
        return False
    try:
        server_mtime = datetime.fromisoformat(server.file_mtime.replace("Z", "+00:00"))
    except ValueError:
        return False
    if server_mtime.tzinfo is None:
        server_mtime = server_mtime.replace(tzinfo=timezone.utc)
    return local_mtime == server_mtime


def _record_file_stat(client: ApiClient, library_id: str, f: dict) -> None:
    """Tell the server a file's current size and mtime, so the next scan's
    fast check matches. A failure only costs a hash next time."""
    data = {"library_id": library_id, "rel_path": f["rel_path"], "file_size": f["file_size"],
            "file_mtime": f["file_mtime"].isoformat() if f.get("file_mtime") else None,
            "media_type": f["media_type"]}
    try:
        client.post("/v1/assets/upsert", json=data)
    except Exception as exc:  # noqa: BLE001 — never stops a scan
        logger.warning("Couldn't record the new mtime of %s: %s", f["rel_path"], exc)


def _fetch_follow_moves(client: ApiClient) -> bool | None:
    """The account's "follow moves and renames" setting: on when the server is
    too old to say, None when it can't be read."""
    try:
        settings = client.get("/v1/tenant/settings").json()
        return settings.get("follow_moves", True) is not False
    except Exception:  # noqa: BLE001 — unread: the caller looks for no moves
        return None


@dataclass
class _MoveCandidate:
    """A file that appears to have moved: same SHA, different path."""
    asset_id: str
    old_rel_path: str
    new_rel_path: str
    sha256: str


def _detect_moves(
    new_files: list[dict],
    existing: dict[str, _ServerAsset],
    root_path: Path,
    local_rel_paths: set[str],
    deleted_ids: list[str] | None = None,
    console: Console | None = None,
) -> tuple[list[_MoveCandidate], list[dict]]:
    """Detect files that moved (same SHA, different path).

    A move is: new local file whose SHA matches a server asset whose
    old rel_path is no longer on the local filesystem.

    Optimizations:
    - Skip entirely if there are no deletions (no old path gone = no moves)
    - Pre-filter by file_size before expensive SHA computation

    Returns (moves, remaining_new_files). Moves are removed from new_files.
    """
    # No deletions = no moves possible (a move requires an old path to disappear)
    if deleted_ids is not None and not deleted_ids:
        return [], new_files

    # Build set of deleted server assets for fast lookup
    deleted_asset_id_set = set(deleted_ids) if deleted_ids else None

    # Build reverse index from deleted server assets: SHA → list, file_size → set of SHAs
    # Only index server assets whose paths are gone locally (deletion candidates)
    sha_to_server: dict[str, list[tuple[str, _ServerAsset]]] = {}
    deleted_file_sizes: set[int] = set()
    for rel_path, sa in existing.items():
        if rel_path in local_rel_paths:
            continue  # still on disk — not a move source
        if deleted_asset_id_set is not None and sa.asset_id not in deleted_asset_id_set:
            continue  # not in deletion list
        if sa.sha256:
            sha_to_server.setdefault(sa.sha256, []).append((rel_path, sa))
            if sa.file_size is not None:
                deleted_file_sizes.add(sa.file_size)

    if not sha_to_server:
        return [], new_files

    # Pre-filter new files by file_size match against deleted assets
    candidates: list[dict] = []
    no_match: list[dict] = []
    for f in new_files:
        if deleted_file_sizes and f.get("file_size") not in deleted_file_sizes:
            no_match.append(f)
        else:
            candidates.append(f)

    if not candidates:
        return [], new_files

    # Hash only the candidates (file_size matched a deleted asset)
    moves: list[_MoveCandidate] = []
    remaining: list[dict] = []
    claimed_asset_ids: set[str] = set()

    progress = None
    tid = None
    if console and len(candidates) > 1:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]Checking moves"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            console=console,
            refresh_per_second=4,
        )
        progress.start()
        tid = progress.add_task("Moves", total=len(candidates))

    try:
        for f in candidates:
            source_path = resolve_source_path(root_path, f["rel_path"])
            source_sha = compute_sha256(source_path)
            f["source_sha256"] = source_sha

            if progress and tid is not None:
                progress.advance(tid)

            if not source_sha or source_sha not in sha_to_server:
                remaining.append(f)
                continue

            # Find a server asset with this SHA whose path is gone locally
            server_candidates = sha_to_server[source_sha]
            match = None
            for old_path, sa in server_candidates:
                if sa.asset_id in claimed_asset_ids:
                    continue
                if old_path not in local_rel_paths:
                    match = (old_path, sa)
                    break

            if match is None:
                remaining.append(f)
                continue

            old_path, sa = match
            moves.append(_MoveCandidate(
                asset_id=sa.asset_id,
                old_rel_path=old_path,
                new_rel_path=f["rel_path"],
                sha256=source_sha,
            ))
            claimed_asset_ids.add(sa.asset_id)
    finally:
        if progress:
            progress.stop()

    # Combine: files that didn't match by size + files that matched size but not SHA
    remaining = no_match + remaining
    return moves, remaining


def _existing_folder(root: Path, rel: str) -> str | None:
    """The deepest folder on the way to rel that's on disk, its name in either
    Unicode form. None is the library root. Raises OSError when a folder
    can't be checked: it may well be there."""
    parts = PurePosixPath(rel).parts
    while parts:
        st = stat_if_present(resolve_source_path(root, "/".join(parts)))
        if st is not None and S_ISDIR(st.st_mode):
            return "/".join(parts)
        parts = parts[:-1]
    return None


def _detect_deletions(
    local_files: list[dict],
    existing: dict[str, _ServerAsset],
    root_path: Path,
    path_prefix: str | None,
) -> list[str]:
    """Find server assets with no corresponding local file. Returns asset IDs."""
    local_rel_paths = {f["rel_path"] for f in local_files}
    scope = existing
    if path_prefix:
        prefix = path_prefix.rstrip("/") + "/"
        scope = {rp: sa for rp, sa in existing.items() if rp.startswith(prefix)}
    if not root_path.is_dir():
        return []
    return [sa.asset_id for rp, sa in scope.items() if rp not in local_rel_paths]


def _made(artifact: str, source_sha256: str | None) -> dict:
    """Lineage for what the scan makes: proxies, probes and previews come out
    of its own constants, which are the registry's settings (a test holds
    them together)."""
    return producer_lineage(artifact, effective_settings(artifact), source_sha256)


def _scan_one(
    *,
    client: ApiClient,
    library_id: str,
    root_path: Path,
    f: dict,
    proxy_cache: ProxyCache,
    stats: ScanStats,
    progress: Progress,
    task_id: object,
    counter_field: str,
) -> None:
    """Scan a single file: proxy gen → EXIF → upload → cache.

    Works for both new and changed files.
    """
    rel_path = f["rel_path"]
    source_path = resolve_source_path(root_path, rel_path).resolve()
    if not source_path.is_relative_to(root_path):
        logger.warning("Skipping %s: escapes library root", rel_path)
        with stats.lock:
            stats.failed += 1
            stats.failed_paths.append(rel_path)
        return

    try:
        # 1. Generate 2048px JPEG proxy
        jpeg_bytes, width_orig, height_orig = _generate_proxy_bytes(source_path)

        # 2. Extract EXIF (includes SHA computation, but we already have it)
        exif_payload = _build_exif_payload(source_path, f["media_type"])
        # Use the SHA we already computed during classification
        if f.get("source_sha256"):
            exif_payload["sha256"] = f["source_sha256"]

        # 3. Convert to WebP for server upload
        webp_bytes = _jpeg_to_webp(jpeg_bytes)

        # 4. POST /v1/ingest — create or update asset
        files = {"proxy": ("proxy.webp", io.BytesIO(webp_bytes), "image/webp")}
        del webp_bytes
        data: dict[str, str] = {
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": str(f["file_size"]),
            "media_type": f["media_type"],
            "width": str(width_orig),
            "height": str(height_orig),
            "exif": json.dumps(exif_payload),
        }
        if f.get("file_mtime") is not None:
            data["file_mtime"] = f["file_mtime"].isoformat()
        data["lineage"] = json.dumps({"proxy": _made("proxy", f.get("source_sha256"))})

        resp = client.post("/v1/ingest", files=files, data=data)
        result = resp.json()
        asset_id = result.get("asset_id")

        # 5. Cache the 2048px proxy + SHA sidecar
        if asset_id and f.get("source_sha256"):
            proxy_cache.put_scan(asset_id, jpeg_bytes, f["source_sha256"])
        elif asset_id:
            proxy_cache.put_scan(asset_id, jpeg_bytes, "")
        del jpeg_bytes

        with stats.lock:
            setattr(stats, counter_field, getattr(stats, counter_field) + 1)
            if asset_id:
                stats.scanned_asset_ids.append(asset_id)
        progress.advance(task_id)

    except Exception as e:
        logger.exception("Failed to scan %s: %s", rel_path, e)
        with stats.lock:
            stats.failed += 1
            stats.failed_paths.append(rel_path)
        progress.console.print(f"[red]scan \u2717[/red] {rel_path}: {e}")
        progress.advance(task_id)


def _scan_one_video(
    *,
    client: ApiClient,
    library_id: str,
    root_path: Path,
    f: dict,
    proxy_cache: ProxyCache,
    stats: ScanStats,
    progress: Progress,
    task_id: object,
    counter_field: str,
) -> None:
    """Scan a single video: poster frame + EXIF + 10-sec preview → upload → cache."""
    rel_path = f["rel_path"]
    source_path = resolve_source_path(root_path, rel_path).resolve()
    if not source_path.is_relative_to(root_path):
        logger.warning("Skipping %s: escapes library root", rel_path)
        with stats.lock:
            stats.failed += 1
            stats.failed_paths.append(rel_path)
        return

    try:
        # 1. Extract poster frame as JPEG proxy
        jpeg_bytes, width_orig, height_orig = _extract_video_poster(source_path)

        # 2. Extract EXIF
        exif_payload = _build_exif_payload(source_path, "video")
        if f.get("source_sha256"):
            exif_payload["sha256"] = f["source_sha256"]

        # 3. Generate 10-second preview
        preview_bytes = _generate_video_preview(source_path)

        # 4. Convert poster to WebP for upload
        webp_bytes = _jpeg_to_webp(jpeg_bytes)

        # 5. POST /v1/ingest — create asset with poster frame as proxy
        files = {"proxy": ("proxy.webp", io.BytesIO(webp_bytes), "image/webp")}
        del webp_bytes
        data: dict[str, str] = {
            "library_id": library_id,
            "rel_path": rel_path,
            "file_size": str(f["file_size"]),
            "media_type": "video",
            "width": str(width_orig),
            "height": str(height_orig),
            "exif": json.dumps(exif_payload),
        }
        if f.get("file_mtime") is not None:
            data["file_mtime"] = f["file_mtime"].isoformat()
        # Frame rate, timecode, audio layout for editor exports. A failed
        # probe doesn't block ingest; `lumiverb enrich --job-type probe`
        # backfills it.
        made = {"proxy": _made("proxy", f.get("source_sha256"))}
        try:
            data["video_facet"] = json.dumps(probe_video(source_path).to_dict())
            made["probe"] = _made("probe", f.get("source_sha256"))
        except Exception as exc:  # noqa: BLE001 — any ffprobe failure
            logger.warning("Probe failed for %s: %s", rel_path, exc)
        data["lineage"] = json.dumps(made)

        resp = client.post("/v1/ingest", files=files, data=data)
        asset_id = resp.json().get("asset_id")

        # 6. Upload video preview
        if asset_id and preview_bytes:
            client.post(
                f"/v1/assets/{asset_id}/artifacts/video_preview",
                files={"file": ("preview.mp4", io.BytesIO(preview_bytes), "video/mp4")},
                data={"lineage": json.dumps(_made("video_preview", f.get("source_sha256")))},
            )
        del preview_bytes

        # 7. Cache the poster proxy + SHA sidecar
        if asset_id and f.get("source_sha256"):
            proxy_cache.put_scan(asset_id, jpeg_bytes, f["source_sha256"])
        elif asset_id:
            proxy_cache.put_scan(asset_id, jpeg_bytes, "")
        del jpeg_bytes

        with stats.lock:
            setattr(stats, counter_field, getattr(stats, counter_field) + 1)
            if asset_id:
                stats.scanned_asset_ids.append(asset_id)
        progress.advance(task_id)

    except Exception as e:
        logger.exception("Failed to scan video %s: %s", rel_path, e)
        with stats.lock:
            stats.failed += 1
            stats.failed_paths.append(rel_path)
        progress.console.print(f"[red]scan \u2717[/red] {rel_path}: {e}")
        progress.advance(task_id)


def _populate_cache_for_unchanged(
    client: ApiClient,
    unchanged_files: list[dict],
    proxy_cache: ProxyCache,
    stats: ScanStats,
    console: Console,
) -> None:
    """For unchanged files missing from cache, download proxy from server."""
    missing = [f for f in unchanged_files if not proxy_cache.has(f["asset_id"])]
    if not missing:
        return

    console.print(f"Populating cache for {len(missing):,} unchanged assets...")
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]Cache"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        console=console,
        refresh_per_second=4,
    )
    with progress:
        tid = progress.add_task("Cache", total=len(missing))
        for f in missing:
            asset_id = f["asset_id"]
            try:
                resp = client._client.get(
                    client._url(f"/v1/assets/{asset_id}/proxy"),
                )
                if resp.status_code == 200:
                    sha = f.get("source_sha256") or ""
                    proxy_cache.put_scan(asset_id, resp.content, sha)
                    with stats.lock:
                        stats.cache_populated += 1
                resp.close()
            except Exception:
                logger.warning("Failed to download proxy for %s", asset_id)
            progress.advance(tid)


def _drain(inflight: set[Future]) -> tuple[set[Future], set[Future]]:
    """Wait for at least one future to finish; return (done, still_pending)."""
    from concurrent.futures import FIRST_COMPLETED, wait
    done, pending = wait(inflight, return_when=FIRST_COMPLETED)
    for fut in done:
        fut.result()
    return done, pending


def _apply_moves(
    client: ApiClient,
    moves: list[_MoveCandidate],
    stats: ScanStats,
    console: Console,
) -> None:
    """Apply detected moves by updating rel_path on the server in batches."""
    console.print(f"Applying {len(moves):,} moves...")
    for batch_start in range(0, len(moves), 500):
        batch = moves[batch_start : batch_start + 500]
        items = [{"asset_id": m.asset_id, "rel_path": m.new_rel_path} for m in batch]
        try:
            client.post("/v1/assets/batch-moves", json={"items": items})
        except Exception as e:
            logger.warning("Batch move failed: %s", e)
            # Fallback: skip these moves rather than crash
            stats.failed += len(batch)
            stats.failed_paths.extend(m.new_rel_path for m in batch)
            continue
        stats.moved += len(batch)


def _prompt_move_decision(console: Console, moves: list[_MoveCandidate]) -> str:
    """Show moved files and prompt user for action. Returns 'apply', 'skip', or 'abort'."""
    console.print(f"\n[yellow]Detected {len(moves):,} moved file(s):[/yellow]")
    show = moves[:10]
    for m in show:
        console.print(f"  {m.old_rel_path} [dim]→[/dim] {m.new_rel_path}")
    if len(moves) > 10:
        console.print(f"  [dim]... and {len(moves) - 10:,} more[/dim]")

    console.print("\nOptions:")
    console.print("  [bold]1[/bold] Perform moves (update paths on server)")
    console.print("  [bold]2[/bold] Skip moves (ignore, don't treat as new/deleted)")
    console.print("  [bold]3[/bold] Abort scan")

    while True:
        try:
            choice = input("\nChoice [1/2/3]: ").strip()
        except (EOFError, KeyboardInterrupt):
            return "abort"
        if choice == "1":
            return "apply"
        if choice == "2":
            return "skip"
        if choice == "3":
            return "abort"
        console.print("[red]Invalid choice. Enter 1, 2, or 3.[/red]")


def _archive_missing(client, deleted_ids: list[str], stats, console, *, allow_mass_delete: bool) -> None:
    """Archive clips whose files are gone ("missing", not the user's trash:
    restored if the file reappears), all in one request so the server's
    mass-missing rule sees the whole scan. When it would take most of a
    library the server asks first (409 mass_missing, usually a volume not
    fully mounted): skipped, unless --allow-mass-delete says yes with the count."""
    console.print(f"Removing {len(deleted_ids):,} assets no longer on disk...")
    body: dict = {"asset_ids": deleted_ids, "reason": "missing"}
    resp = client.raw("DELETE", "/v1/assets", json=body)
    if resp.status_code == 409:
        err = {}
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            err = (resp.json() or {}).get("error") or {}
        if err.get("code") == "mass_missing":
            count = (err.get("details") or {}).get("count")
            if not allow_mass_delete:
                console.print(
                    f"[yellow]Skipping {len(deleted_ids):,} deletions: {err.get('message', '')} "
                    "If the files really are gone, re-run with --allow-mass-delete.[/yellow]"
                )
                return
            resp = client.raw("DELETE", "/v1/assets", json={**body, "confirm_missing": count})
    if resp.status_code >= 400:
        raise LumiverbAPIError("archive_missing_failed", resp.text, resp.status_code)
    # Some may have moved to an empty copy of their file instead (copy, then delete).
    handed_over = 0
    with contextlib.suppress(AttributeError, TypeError, ValueError):
        handed_over = len(resp.json().get("handed_over") or [])
    stats.deleted = len(deleted_ids) - handed_over
    stats.moved += handed_over


def run_scan(
    client: ApiClient,
    library: dict,
    *,
    concurrency: int = 4,
    path_prefix: str | None = None,
    force: bool = False,
    media_type_filter: str = "all",
    dry_run: bool = False,
    allow_moves: bool = False,
    skip_moves: bool = False,
    thorough: bool = False,
    allow_mass_delete: bool = False,
    console: Console,
) -> ScanStats:
    """Discover files, compute SHA, extract EXIF, generate proxies, upload.

    This is Phase 1 of the scan/enrich split. Scan touches source files;
    enrich (Phase 2) operates on the proxy cache only.
    """
    library_id = library["library_id"]
    root_path = reachable_root(library)

    if root_path is None:
        here = local_library_root(library)
        console.print(f"[red]Library root not accessible: {here}[/red]")
        if here is not None and str(here) == library.get("root_path"):
            console.print("Is the volume mounted? If it's mounted elsewhere on this machine, "
                          "run `lumiverb config map-root`.")
        else:
            console.print("Is the volume mounted?")
        return ScanStats(root_unreachable=True)

    stats = ScanStats()

    # The account may not follow moves (the path is the identity): then a
    # file at a new path is new, and its old path is simply gone.
    # Unread, look for none: the old path is archived and the new one ingested,
    # and a server that follows moves restores the asset there by content.
    follow_moves = _fetch_follow_moves(client)
    if follow_moves is None:
        console.print("[dim]Couldn't read the account's settings: this scan looks for no moves.[/dim]")
    elif not follow_moves:
        console.print("[dim]This account doesn't follow moves and renames: files at new paths are new assets.[/dim]")

    # Load path filters
    tenant_filters = _load_tenant_filters(client)
    library_filters = _load_library_filters(client, library_id)
    total_filters = len(tenant_filters) + len(library_filters)
    if total_filters:
        console.print(f"Loaded {len(tenant_filters)} tenant + {len(library_filters)} library filter(s)")

    # A folder that's gone is scanned from the nearest folder still there,
    # which sees what was in it as deleted.
    if path_prefix:
        try:
            found = _existing_folder(root_path, path_prefix)
        except UnsafeRelPathError:
            console.print(f"[red]Invalid path prefix {path_prefix}: it must be a folder inside the library[/red]")
            stats.unlisted.append(path_prefix)  # nothing is removed
            return stats
        except OSError as exc:
            console.print(f"[yellow]Can't check {path_prefix} ({exc}), so this scan doesn't remove anything[/yellow]")
            stats.unlisted.append(path_prefix)
            return stats
        if found != path_prefix:
            console.print(f"{path_prefix} isn't on disk; scanning {found or 'the whole library'}")
            path_prefix = found
    if not path_prefix and not root_path.is_dir():
        console.print(f"[red]Library root went away: {root_path}[/red]")
        return ScanStats(root_unreachable=True)

    # Discover files
    console.print("[bold]Discovering files...[/bold]")
    local_files = _walk_library(root_path, path_prefix, tenant_filters=tenant_filters, library_filters=library_filters,
                                unlisted=stats.unlisted)
    if stats.unlisted:
        console.print(f"[yellow]Couldn't list {len(stats.unlisted):,} folder(s), so this scan "
                      "doesn't remove anything: {}[/yellow]".format(", ".join(stats.unlisted[:5]) or "/"))

    # Filter by media type
    if media_type_filter != "all":
        local_files = [f for f in local_files if f["media_type"] == media_type_filter]

    console.print(f"Found {len(local_files):,} media files")

    # An empty library is more likely an unmounted volume than a deleted
    # one. An empty folder within it is just empty: its files are gone.
    if not local_files and not force and not path_prefix:
        # A share gone since the scan started looks just like this: say so,
        # so the worker keeps the changes for when it's back.
        if reachable_root(library, require_entries=True) is None:
            console.print("[red]The library's storage went away during the scan; it's scanned again once it's back.[/red]")
            stats.root_unreachable = True
        return stats

    # Fetch existing assets with SHA for change detection
    console.print("Checking server for existing assets...")
    existing = _fetch_existing_assets_with_sha(client, library_id)
    console.print(f"Server has {len(existing):,} existing assets")

    # Every file on disk, ingested or not: none of them is missing.
    on_disk = local_files
    ignored = _fetch_ignored_paths(client, library_id)
    if ignored:
        before = len(local_files)
        local_files = [f for f in local_files if not _skipped(f, ignored, root_path)]
        if before > len(local_files):
            console.print(f"Skipping {before - len(local_files):,} file(s) you removed")

    # Files written in the last SETTLE_SEC may still be copying, so they wait
    # for a later scan, as on the Mac. They still count as on disk, so they
    # are never taken for deleted.
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=SETTLE_SEC)
    ahead = now + timedelta(seconds=FUTURE_MTIME_SEC)
    settled: list[dict] = []
    for f in local_files:
        if f.get("file_mtime") and cutoff < f["file_mtime"] <= ahead:
            stats.settling_paths.append(f["rel_path"])
        else:
            settled.append(f)
    stats.settling = len(stats.settling_paths)
    if stats.settling:
        console.print(f"{stats.settling:,} file(s) still being written; a later scan picks them up")

    # Split files: new (not on server by path) vs existing (need SHA check)
    # Default (fast): mtime+size match skips hashing. --thorough forces SHA on all.
    new_files, needs_hash, fast_unchanged = _split_files(
        settled, existing, thorough=thorough or force,
    )
    local_rel_paths = {f["rel_path"] for f in on_disk}

    # Deletions only consider what this scan covers: a `--media-type image`
    # scan doesn't see videos on disk, so it must not count them as gone.
    # (The mass-missing rule is the server's: DELETE /v1/assets.)
    scope = existing
    if media_type_filter != "all":
        scope = {rp: sa for rp, sa in existing.items() if sa.media_type == media_type_filter}

    # Detect deletions first (needed to scope move detection)
    deleted_ids = _detect_deletions(on_disk, scope, root_path, path_prefix)
    if stats.unlisted:
        # Files in a folder that couldn't be listed may well be there, so
        # they're neither deleted nor the old half of a move.
        unseen = {sa.asset_id for rp, sa in scope.items() if any(is_within(rp, u) for u in stats.unlisted)}
        deleted_ids = [aid for aid in deleted_ids if aid not in unseen]

    # --- Move detection ---
    # Only check for moves when: there are new files, there are deletions
    # (a move requires an old path to disappear), and --force is not set.
    # Pre-filters by file_size before expensive SHA computation.
    moves: list[_MoveCandidate] = []
    if new_files and deleted_ids and not force and not skip_moves and follow_moves is True:
        moves, new_files = _detect_moves(
            new_files, existing, root_path, local_rel_paths,
            deleted_ids=deleted_ids, console=console,
        )

    # Remove moved assets from deletion candidates (they're not deleted, just moved)
    if moves:
        moved_asset_ids = {m.asset_id for m in moves}
        deleted_ids = [aid for aid in deleted_ids if aid not in moved_asset_ids]

    fast_skip_msg = f", {len(fast_unchanged):,} unchanged (fast)" if fast_unchanged else ""
    console.print(
        f"{len(new_files):,} new, "
        f"{len(needs_hash):,} to check, "
        f"{len(deleted_ids):,} deleted, "
        f"{len(moves):,} moved"
        + fast_skip_msg
    )

    # --- Handle moves ---
    move_decision = "skip"  # default: no moves or skip
    if moves:
        if allow_moves:
            move_decision = "apply"
        elif dry_run:
            # dry-run: report and suggest flag
            console.print(f"\n[yellow]{len(moves):,} moved file(s) detected.[/yellow]")
            show = moves[:10]
            for m in show:
                console.print(f"  {m.old_rel_path} [dim]→[/dim] {m.new_rel_path}")
            if len(moves) > 10:
                console.print(f"  [dim]... and {len(moves) - 10:,} more[/dim]")
            console.print("[dim]Use --allow-moves to apply, or --skip-moves to ignore.[/dim]")
            move_decision = "skip"
        else:
            # Interactive prompt
            move_decision = _prompt_move_decision(console, moves)
            if move_decision == "abort":
                console.print("[red]Scan aborted.[/red]")
                return stats

    # --skip-moves suppresses deletions too: without move detection we can't
    # distinguish real deletes from the "old path" half of a move.
    if stats.unlisted and deleted_ids:
        console.print(f"[yellow]Skipping {len(deleted_ids):,} deletions: some folders couldn't be listed[/yellow]")
        deleted_ids = []

    if skip_moves and follow_moves is not False and deleted_ids:
        console.print(f"[dim]Skipping {len(deleted_ids):,} deletions (--skip-moves)[/dim]")
        deleted_ids = []

    # For skipped moves (via prompt choice): moved files don't participate.
    # New paths already removed from new_files by _detect_moves.
    # Old paths already removed from deleted_ids above.
    if move_decision == "skip" and moves:
        console.print(f"[dim]Skipping {len(moves):,} moves[/dim]")

    if dry_run:
        # For dry-run, hash synchronously to show full breakdown
        changed_files: list[dict] = []
        unchanged_files: list[dict] = []
        if needs_hash:
            hash_progress = Progress(
                SpinnerColumn(), TextColumn("[bold]Hashing"),
                BarColumn(bar_width=30), MofNCompleteColumn(),
                console=console, refresh_per_second=4,
            )
            with hash_progress:
                tid = hash_progress.add_task("Hashing", total=len(needs_hash))
                for f in needs_hash:
                    server = f.pop("_server")
                    source_sha = compute_sha256(resolve_source_path(root_path, f["rel_path"]))
                    if force or (source_sha and server.sha256 != source_sha):
                        changed_files.append(f)
                    else:
                        unchanged_files.append(f)
                    hash_progress.advance(tid)

        total_unchanged = len(unchanged_files) + len(fast_unchanged)
        console.print(
            f"\n[bold]Scan summary:[/bold] "
            f"{len(new_files):,} new, "
            f"{len(changed_files):,} changed, "
            f"{total_unchanged:,} unchanged, "
            f"{len(deleted_ids):,} deleted, "
            f"{len(moves):,} moved"
        )
        console.print(f"\n[bold]Root path:[/bold]  {root_path}")
        return stats

    # Files that look missing may only be the storage going away mid-scan (a
    # clean unmount leaves an empty folder): check it again before changing
    # anything, and stop if it's gone (Robert's call, Oct 8).
    if (deleted_ids or (moves and move_decision == "apply")) and reachable_root(library, require_entries=True) is None:
        console.print("[red]The library's storage went away during the scan, so nothing was archived or moved. "
                      "It's scanned again once it's back.[/red]")
        stats.root_unreachable = True
        return stats

    # --- Apply moves FIRST (before any destructive actions) ---
    if move_decision == "apply" and moves:
        _apply_moves(client, moves, stats, console)

    # Soft-delete missing assets (after moves, so moved assets are not deleted)
    if deleted_ids:
        _archive_missing(client, deleted_ids, stats, console, allow_mass_delete=allow_mass_delete)

    # Pipeline: scan new files immediately while hashing existing files
    # in the background. Changed files feed into the same scan pool as
    # hashing completes.
    # Count fast-unchanged toward stats
    stats.unchanged += len(fast_unchanged)

    total_to_scan = len(new_files) + len(needs_hash)  # upper bound (unchanged will be skipped)
    if not total_to_scan:
        proxy_cache = ProxyCache(root_path=root_path, client=client)
        _populate_cache_for_unchanged(client, fast_unchanged, proxy_cache, stats, console)
        return stats

    proxy_cache = ProxyCache(root_path=root_path, client=client)
    scan_progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold]Scanning"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TimeRemainingColumn(),
        console=console,
        refresh_per_second=4,
    )
    unchanged_files = []

    def _submit(f: dict, kind: str, pool: ThreadPoolExecutor, inflight: set, tid: object) -> set:
        handler = _scan_one_video if f["media_type"] == "video" else _scan_one
        fut = pool.submit(
            handler,
            client=client,
            library_id=library_id,
            root_path=root_path,
            f=f,
            proxy_cache=proxy_cache,
            stats=stats,
            progress=scan_progress,
            task_id=tid,
            counter_field=kind,
        )
        inflight.add(fut)
        if len(inflight) >= concurrency * 2:
            done, inflight = _drain(inflight)
        return inflight

    with scan_progress:
        tid = scan_progress.add_task("Scanning", total=total_to_scan)
        pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="scan")
        inflight: set[Future] = set()

        # Dispatch new files immediately — no hashing needed
        for f in new_files:
            inflight = _submit(f, "new", pool, inflight, tid)

        # Hash existing files and dispatch changed ones as they're identified.
        # Unchanged files skip scanning (advance progress bar only).
        for f in needs_hash:
            server = f.pop("_server")
            source_sha = compute_sha256(resolve_source_path(root_path, f["rel_path"]))
            f["source_sha256"] = source_sha

            if force or (source_sha and server.sha256 != source_sha):
                f["asset_id"] = server.asset_id
                inflight = _submit(f, "changed", pool, inflight, tid)
            else:
                f["asset_id"] = server.asset_id
                unchanged_files.append(f)
                stats.unchanged += 1
                scan_progress.advance(tid)
                # Same content, new size or mtime (touched, copied back): record
                # it, or every later scan reads the whole file again.
                if not _mtime_size_match(f, server):
                    _record_file_stat(client, library_id, f)

        while inflight:
            done, inflight = _drain(inflight)
        pool.shutdown(wait=True)

    # Populate cache for unchanged files (both hash-verified and fast-skipped)
    _populate_cache_for_unchanged(
        client, unchanged_files + fast_unchanged, proxy_cache, stats, console,
    )

    return stats
