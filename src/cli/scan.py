"""Scan phase: discover files, hash, extract EXIF, generate proxies, upload.

Scan is the only operation that touches source files. It produces three
outputs per file: a server-side asset record, a server-side 2048px proxy,
and a local proxy cache entry with SHA sidecar.

Change detection compares local SHA-256 against server-stored values to
classify files as new, changed, unchanged, or deleted.

See docs/adr/011-ingest-refactor-scan-and-enrich.md for full design.
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

from src.cli.client import LumiverbClient
from src.cli.ingest import (
    SUPPORTED_EXTENSIONS,
    _build_exif_payload,
    _detect_media_type,
    _generate_proxy_bytes,
    _jpeg_to_webp,
    _load_library_filters,
    _load_tenant_filters,
    _walk_library,
)
from src.cli.proxy_cache import ProxyCache
from src.workers.exif_extract import compute_sha256

logger = logging.getLogger(__name__)


@dataclass
class ScanStats:
    lock: threading.Lock = field(default_factory=threading.Lock)
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0
    cache_populated: int = 0
    failed: int = 0


@dataclass
class _ServerAsset:
    asset_id: str
    sha256: str | None


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
            )
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return existing


def _classify_files(
    local_files: list[dict],
    existing: dict[str, _ServerAsset],
    root_path: Path,
    path_prefix: str | None,
    force: bool,
    console: Console,
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Classify local files against server state.

    Returns (new_files, changed_files, unchanged_files, deleted_asset_ids).
    Each file dict in new/changed/unchanged has the original walk fields
    plus 'source_sha256' (computed here) and optionally 'asset_id'.
    """
    new_files: list[dict] = []
    changed_files: list[dict] = []
    unchanged_files: list[dict] = []

    # Split into new (not on server) vs needs-comparison (on server).
    # Only files that exist on the server need SHA computation.
    needs_hash: list[dict] = []
    for f in local_files:
        server = existing.get(f["rel_path"])
        if server is None:
            new_files.append(f)
        else:
            f["_server"] = server
            needs_hash.append(f)

    if needs_hash:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold]Hashing"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            console=console,
            refresh_per_second=4,
        )
        with progress:
            tid = progress.add_task("Hashing", total=len(needs_hash))
            for f in needs_hash:
                server = f.pop("_server")
                source_path = root_path / f["rel_path"]
                source_sha = compute_sha256(source_path)
                f["source_sha256"] = source_sha

                if force or (source_sha and server.sha256 != source_sha):
                    f["asset_id"] = server.asset_id
                    changed_files.append(f)
                else:
                    f["asset_id"] = server.asset_id
                    unchanged_files.append(f)
                progress.advance(tid)

    # Detect deletions — server assets not found on disk
    local_rel_paths = {f["rel_path"] for f in local_files}
    scope = existing
    if path_prefix:
        prefix = path_prefix.rstrip("/") + "/"
        scope = {rp: sa for rp, sa in existing.items() if rp.startswith(prefix)}

    deleted_ids = [
        sa.asset_id for rp, sa in scope.items() if rp not in local_rel_paths
    ] if root_path.is_dir() else []

    return new_files, changed_files, unchanged_files, deleted_ids


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
        progress.advance(task_id)

    except Exception as e:
        logger.exception("Failed to scan %s: %s", rel_path, e)
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


def run_scan(
    client: LumiverbClient,
    library: dict,
    *,
    concurrency: int = 4,
    path_prefix: str | None = None,
    force: bool = False,
    media_type_filter: str = "all",
    dry_run: bool = False,
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

    # Separate images and videos. Videos need poster frame extraction (ffmpeg),
    # not the image proxy pipeline. Video scan support is deferred to Phase 2.
    images = [f for f in local_files if f["media_type"] == "image"]
    videos = [f for f in local_files if f["media_type"] == "video"]
    console.print(f"Found {len(local_files):,} media files ({len(images):,} images, {len(videos):,} videos)")
    if videos:
        console.print("[dim]Videos are skipped by scan — use `lumiverb ingest` for video processing[/dim]")
    local_files = images

    if not local_files and not force:
        return stats

    # Fetch existing assets with SHA for change detection
    console.print("Checking server for existing assets...")
    existing = _fetch_existing_assets_with_sha(client, library_id)
    console.print(f"Server has {len(existing):,} existing assets")

    # Classify files
    new_files, changed_files, unchanged_files, deleted_ids = _classify_files(
        local_files, existing, root_path, path_prefix, force, console,
    )

    console.print(
        f"\n[bold]Scan summary:[/bold] "
        f"{len(new_files):,} new, "
        f"{len(changed_files):,} changed, "
        f"{len(unchanged_files):,} unchanged, "
        f"{len(deleted_ids):,} deleted"
    )

    if dry_run:
        console.print()
        console.print(f"[bold]Root path:[/bold]  {root_path}")
        if new_files and len(new_files) <= 20:
            console.print("\n[bold]New files:[/bold]")
            for f in sorted(new_files, key=lambda f: f["rel_path"])[:20]:
                console.print(f"  {f['rel_path']}")
        if changed_files and len(changed_files) <= 20:
            console.print("\n[bold]Changed files:[/bold]")
            for f in sorted(changed_files, key=lambda f: f["rel_path"])[:20]:
                console.print(f"  {f['rel_path']}")
        if deleted_ids and len(deleted_ids) <= 20:
            console.print(f"\n[bold]Deleted assets:[/bold] {len(deleted_ids):,}")
        return stats

    # Soft-delete missing assets
    if deleted_ids:
        console.print(f"Removing {len(deleted_ids):,} assets no longer on disk...")
        for batch_start in range(0, len(deleted_ids), 500):
            batch = deleted_ids[batch_start : batch_start + 500]
            client.delete("/v1/assets", json={"asset_ids": batch})
        stats.deleted = len(deleted_ids)

    # Process new + changed files
    to_scan = [(f, "new") for f in new_files] + [(f, "changed") for f in changed_files]
    if to_scan:
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
        with scan_progress:
            tid = scan_progress.add_task("Scanning", total=len(to_scan))
            pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="scan")
            inflight: set[Future] = set()
            for f, kind in to_scan:
                fut = pool.submit(
                    _scan_one,
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
            while inflight:
                done, inflight = _drain(inflight)
            pool.shutdown(wait=True)
    else:
        proxy_cache = ProxyCache(root_path=root_path, client=client)

    # Populate cache for unchanged files
    stats.unchanged = len(unchanged_files)
    _populate_cache_for_unchanged(client, unchanged_files, proxy_cache, stats, console)

    return stats
