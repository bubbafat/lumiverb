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
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
)

from src.client.cli.client import LumiverbClient
from src.client.cli.ingest import (
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
from src.client.proxy.proxy_cache import ProxyCache
from src.client.workers.exif_extract import compute_sha256

logger = logging.getLogger(__name__)


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
    scanned_asset_ids: list[str] = field(default_factory=list)


@dataclass
class _ServerAsset:
    asset_id: str
    sha256: str | None
    file_size: int | None = None
    file_mtime: str | None = None  # ISO8601 string from server


def _fetch_existing_assets_with_sha(
    client: LumiverbClient, library_id: str,
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
            )
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return existing


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
    # Compare mtime: local is a datetime, server is ISO8601 string
    local_mtime = local_file.get("file_mtime")
    if local_mtime is None:
        return False
    return local_mtime.isoformat() == server.file_mtime


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
            source_path = root_path / f["rel_path"]
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


def _scan_one(
    *,
    client: LumiverbClient,
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
    source_path = (root_path / rel_path).resolve()
    if not source_path.is_relative_to(root_path):
        logger.warning("Skipping %s: escapes library root", rel_path)
        with stats.lock:
            stats.failed += 1
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
        progress.console.print(f"[red]scan \u2717[/red] {rel_path}: {e}")
        progress.advance(task_id)


def _scan_one_video(
    *,
    client: LumiverbClient,
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
    source_path = (root_path / rel_path).resolve()
    if not source_path.is_relative_to(root_path):
        logger.warning("Skipping %s: escapes library root", rel_path)
        with stats.lock:
            stats.failed += 1
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

        resp = client.post("/v1/ingest", files=files, data=data)
        asset_id = resp.json().get("asset_id")

        # 6. Upload video preview
        if asset_id and preview_bytes:
            client.post(
                f"/v1/assets/{asset_id}/artifacts/video_preview",
                files={"file": ("preview.mp4", io.BytesIO(preview_bytes), "video/mp4")},
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
        progress.console.print(f"[red]scan \u2717[/red] {rel_path}: {e}")
        progress.advance(task_id)


def _populate_cache_for_unchanged(
    client: LumiverbClient,
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
    client: LumiverbClient,
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


def run_scan(
    client: LumiverbClient,
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
    console: Console,
) -> ScanStats:
    """Discover files, compute SHA, extract EXIF, generate proxies, upload.

    This is Phase 1 of the scan/enrich split. Scan touches source files;
    enrich (Phase 2) operates on the proxy cache only.
    """
    library_id = library["library_id"]
    root_path = Path(library["root_path"]).resolve()

    if not root_path.is_dir():
        console.print(f"[red]Library root not accessible: {root_path}[/red]")
        console.print("Is the volume mounted?")
        return ScanStats()

    stats = ScanStats()

    # Load path filters
    tenant_filters = _load_tenant_filters(client)
    library_filters = _load_library_filters(client, library_id)
    total_filters = len(tenant_filters) + len(library_filters)
    if total_filters:
        console.print(f"Loaded {len(tenant_filters)} tenant + {len(library_filters)} library filter(s)")

    # Discover files
    console.print("[bold]Discovering files...[/bold]")
    local_files = _walk_library(root_path, path_prefix, tenant_filters=tenant_filters, library_filters=library_filters)

    # Filter by media type
    if media_type_filter != "all":
        local_files = [f for f in local_files if f["media_type"] == media_type_filter]

    console.print(f"Found {len(local_files):,} media files")

    if not local_files and not force:
        return stats

    # Fetch existing assets with SHA for change detection
    console.print("Checking server for existing assets...")
    existing = _fetch_existing_assets_with_sha(client, library_id)
    console.print(f"Server has {len(existing):,} existing assets")

    # Split files: new (not on server by path) vs existing (need SHA check)
    # Default (fast): mtime+size match skips hashing. --thorough forces SHA on all.
    new_files, needs_hash, fast_unchanged = _split_files(
        local_files, existing, thorough=thorough or force,
    )
    local_rel_paths = {f["rel_path"] for f in local_files}

    # Detect deletions first (needed to scope move detection)
    deleted_ids = _detect_deletions(local_files, existing, root_path, path_prefix)

    # --- Move detection ---
    # Only check for moves when: there are new files, there are deletions
    # (a move requires an old path to disappear), and --force is not set.
    # Pre-filters by file_size before expensive SHA computation.
    moves: list[_MoveCandidate] = []
    if new_files and deleted_ids and not force and not skip_moves:
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
    if skip_moves and deleted_ids:
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
                    source_sha = compute_sha256(root_path / f["rel_path"])
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

    # --- Apply moves FIRST (before any destructive actions) ---
    if move_decision == "apply" and moves:
        _apply_moves(client, moves, stats, console)

    # Soft-delete missing assets (after moves, so moved assets are not deleted)
    if deleted_ids:
        console.print(f"Removing {len(deleted_ids):,} assets no longer on disk...")
        for batch_start in range(0, len(deleted_ids), 500):
            batch = deleted_ids[batch_start : batch_start + 500]
            client.delete("/v1/assets", json={"asset_ids": batch})
        stats.deleted = len(deleted_ids)

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
            source_sha = compute_sha256(root_path / f["rel_path"])
            f["source_sha256"] = source_sha

            if force or (source_sha and server.sha256 != source_sha):
                f["asset_id"] = server.asset_id
                inflight = _submit(f, "changed", pool, inflight, tid)
            else:
                f["asset_id"] = server.asset_id
                unchanged_files.append(f)
                stats.unchanged += 1
                scan_progress.advance(tid)

        while inflight:
            done, inflight = _drain(inflight)
        pool.shutdown(wait=True)

    # Populate cache for unchanged files (both hash-verified and fast-skipped)
    _populate_cache_for_unchanged(
        client, unchanged_files + fast_unchanged, proxy_cache, stats, console,
    )

    return stats
