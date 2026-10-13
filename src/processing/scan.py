"""Scan phase: walk a library, say what's there, upload what the server asks for.

Scan is the only operation that touches source files. It produces three
outputs per file: a server-side asset record, a server-side 2048px proxy,
and a local proxy cache entry with SHA sidecar.

The client says what it found; the server decides (ADR-018 decision 12,
src/server/scans.py). A scan is a session on the server: started before the
walk, it's sent every media file the walk saw (path, size, modified time),
then plans each one: upload it, hash it first, unchanged, or skipped (a
file a person removed). The client hashes what it's asked to, and the
server decides from the content: changed, unchanged, moved (made now, or
asked about first), skipped. The finish archives as missing the clips whose
files weren't found, and the client then makes and uploads previews for
what's new or changed (POST /v1/ingest, as before).

What stays here: the walk (filters, hidden files, no symlinks, control
characters, refused folders, names in NFC, folders it couldn't list),
files still being written, a scope folder that's gone, whether the storage
is still there before anything is moved or archived, reading the files
(hashes, previews, EXIF), the proxy cache, and the CLI's question about moves.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import threading
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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

from src.processing.api import ApiClient
from src.processing.ingest import (
    _build_exif_payload,
    _extract_video_poster,
    _generate_proxy_bytes,
    _generate_video_preview,
    _jpeg_to_webp,
    _load_library_filters,
    _load_tenant_filters,
    _walk_library,
)
from src.processing.proxy.proxy_cache import ProxyCache
from src.processing.roots import local_library_root, reachable_root
from src.processing.video.probe import probe_video
from src.processing.workers.exif_extract import compute_sha256
from src.shared.io_utils import (
    UnsafeRelPathError,
    check_rel_path,
    resolve_source_path,
    stat_if_present,
)
from src.shared.producers import effective_settings
from src.shared.producers import lineage as producer_lineage

logger = logging.getLogger(__name__)

# The macOS scanner's quarantine: a file modified this recently may still be
# copying.
SETTLE_SEC = 30
# A file stamped further ahead than this came from a camera whose clock runs
# ahead, not from a copy in progress.
FUTURE_MTIME_SEC = 300

# Files in one call (found, hashes), and a page of the plan: the server's most.
FILES_PER_CALL = 1000
PLAN_PAGE = 500
# Every planned file, the unchanged too: their clips fill the proxy cache.
EVERY_ACTION = "upload,hash,skipped,unchanged"
# A path the server's rule refuses is never sent (as long as Linux's PATH_MAX).
MAX_PATH = 4096
# How often hashes are sent again when another clip was just put at a move's path.
PATH_TAKEN_TRIES = 3
# The most folders a plan can name as unlisted: beyond, the library's root is named.
MAX_UNLISTED = 10_000


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
    # Clips the server asked about (409 mass_missing) and that weren't archived.
    mass_missing: int = 0


@dataclass
class _Work:
    """What the plan and the server's decisions leave for the client, in walk order."""

    new: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)
    # With their clips, for the proxy cache.
    unchanged: list[dict] = field(default_factory=list)
    # Moves the server asks about (moves "ask"): {rel_path, asset_id, moved_from}.
    candidates: list[dict] = field(default_factory=list)
    # Files a person removed.
    skipped: int = 0
    # Moves made (a dry run's: that would be).
    moved: int = 0


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
    try:
        source_path = resolve_source_path(root_path, rel_path).resolve()
    except UnsafeRelPathError:
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
        # The capture facts (taken_at, its zone, GPS accuracy) came with the EXIF.
        data["lineage"] = json.dumps({"proxy": _made("proxy", f.get("source_sha256")),
                                      "capture": _made("capture", f.get("source_sha256"))})

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
        progress.console.print(f"[red]scan ✗[/red] {rel_path}: {e}")
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
    try:
        source_path = resolve_source_path(root_path, rel_path).resolve()
    except UnsafeRelPathError:
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
        made = {"proxy": _made("proxy", f.get("source_sha256")), "capture": _made("capture", f.get("source_sha256"))}
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
        progress.console.print(f"[red]scan ✗[/red] {rel_path}: {e}")
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
                    client._url(f"/v1/assets/{asset_id}/artifacts/proxy"),
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


def _prompt_move_decision(console: Console, moves: list[dict]) -> str:
    """Show moved files and prompt user for action. Returns 'apply', 'skip', or 'abort'."""
    console.print(f"\n[yellow]Detected {len(moves):,} moved file(s):[/yellow]")
    _show_moves(console, moves)

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


def _show_moves(console: Console, moves: list[dict]) -> None:
    for m in moves[:10]:
        console.print(f"  {m['moved_from']} [dim]→[/dim] {m['rel_path']}")
    if len(moves) > 10:
        console.print(f"  [dim]... and {len(moves) - 10:,} more[/dim]")


def _progress(console: Console, label: str, *, eta: bool = False) -> Progress:
    columns = [SpinnerColumn(), TextColumn(f"[bold]{label}"), BarColumn(bar_width=30), MofNCompleteColumn()]
    return Progress(*columns, *([TimeRemainingColumn()] if eta else []), console=console, refresh_per_second=4)


def _batches(items: list, size: int) -> Iterator[list]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


# ---------------------------------------------------------------------------
# The scan's conversation with the server (routers/scans.py)
# ---------------------------------------------------------------------------


def _error(resp) -> dict:
    """The error envelope's error ({} when there's none)."""
    with contextlib.suppress(AttributeError, TypeError, ValueError):
        return (resp.json() or {}).get("error") or {}
    return {}


def _answer(client: ApiClient, resp) -> dict:
    """A raw response's JSON; any status but 2xx is raised as the client raises it."""
    return client.checked(resp).json()


def _post_again_if_taken(client: ApiClient, path: str, body: dict) -> dict:
    """POST, sent again when another clip was just put at a move's path
    (409 path_taken: the file is then that clip's)."""
    for _ in range(PATH_TAKEN_TRIES):
        resp = client.raw("POST", path, json=body)
        if resp.status_code != 409 or _error(resp).get("code") != "path_taken":
            break
    return _answer(client, resp)


def _abandon(client: ApiClient, scan_id: str) -> None:
    """Drop the scan and its listing: nothing else changes. Never fails the
    scan (an unfinished one expires in a day anyway)."""
    try:
        client.raw("DELETE", f"/v1/scans/{scan_id}")
    except Exception as exc:  # noqa: BLE001 — the scan's outcome stands
        logger.warning("Couldn't drop scan %s: %s", scan_id, exc)


def _sendable(rel_path: str, *, allow_root: bool = False) -> bool:
    """A path the server's rule takes (check_rel_path: no backslash, no
    drive letter, ...; no longer than a path can be)."""
    try:
        check_rel_path(rel_path, allow_root=allow_root)
    except UnsafeRelPathError:
        return False
    return len(rel_path) <= MAX_PATH


def _refused_name(rel_path: str) -> bool:
    """A file the server would refuse by its name: never sent, as the walk
    never takes a name with a control character. Said once, as a warning,
    never a failure to try again."""
    if _sendable(rel_path):
        return False
    logger.warning("Not scanning %r: the server refuses its name", rel_path[:MAX_PATH])
    return True


def _settling(f: dict, now: datetime) -> bool:
    """Written in the last SETTLE_SEC, so maybe still copying: it waits for a
    later scan, as on the Mac. A camera clock far ahead isn't a copy."""
    mtime = f.get("file_mtime")
    return (bool(f["file_size"]) and mtime is not None
            and now - timedelta(seconds=SETTLE_SEC) < mtime <= now + timedelta(seconds=FUTURE_MTIME_SEC))


def _found(f: dict) -> dict:
    return {"rel_path": f["rel_path"], "file_size": f["file_size"],
            "file_mtime": f["file_mtime"].isoformat() if f.get("file_mtime") else None,
            "settling": f["settling"]}


def _send_found(client: ApiClient, scan_id: str, files: list[dict], console: Console) -> None:
    """Every media file the walk saw, FILES_PER_CALL at a time."""
    progress = _progress(console, "Listing")
    with progress:
        tid = progress.add_task("Listing", total=len(files))
        for batch in _batches(files, FILES_PER_CALL):
            client.post(f"/v1/scans/{scan_id}/found", json={"files": [_found(f) for f in batch]})
            progress.advance(tid, len(batch))


def _plan(client: ApiClient, scan_id: str, unlisted: list[str], console: Console) -> tuple[dict, dict[str, dict]]:
    """The listing is complete: the plan's counts, and every planned file by rel_path."""
    # A folder whose name the server refuses can't be named: then nothing in
    # the library is taken for gone ("" is its root).
    named = sorted({u if _sendable(u, allow_root=True) else "" for u in unlisted})
    if len(named) > MAX_UNLISTED:
        named = [""]
    params = {"action": EVERY_ACTION, "limit": PLAN_PAGE}
    first = client.post(f"/v1/scans/{scan_id}/plan", params=params, json={"unlisted": named}).json()
    counts = first["counts"]
    plan = {p["rel_path"]: p for p in first["files"]}
    cursor = first.get("next_cursor")
    if cursor:
        planned = counts["upload"] + counts["hash"] + counts["unchanged"] + counts["skipped"]
        progress = _progress(console, "Plan")
        with progress:
            tid = progress.add_task("Plan", total=planned, completed=len(plan))
            while cursor:
                page = client.get(f"/v1/scans/{scan_id}/plan", params={**params, "after": cursor}).json()
                plan.update((p["rel_path"], p) for p in page["files"])
                progress.update(tid, completed=len(plan))
                cursor = page.get("next_cursor")
    return counts, plan


def _sha_of(root_path: Path, rel_path: str) -> str | None:
    """The file's SHA-256, or None when it can't be read."""
    try:
        return compute_sha256(resolve_source_path(root_path, rel_path))
    except (OSError, UnsafeRelPathError) as exc:
        logger.warning("Can't read %s: %s", rel_path, exc)
        return None


def _hashed(root_path: Path, files: list[dict], concurrency: int) -> Iterator[tuple[dict, str | None]]:
    """Each file with its SHA-256 (None: it couldn't be read), in the order
    given, read `concurrency` at a time and never more than concurrency * 2
    waiting."""
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="hash") as pool:
        waiting: deque[tuple[dict, Future]] = deque()
        for f in files:
            waiting.append((f, pool.submit(_sha_of, root_path, f["rel_path"])))
            if len(waiting) >= concurrency * 2:
                done, fut = waiting.popleft()
                yield done, fut.result()
        while waiting:
            done, fut = waiting.popleft()
            yield done, fut.result()


def _unread(f: dict, work: _Work, stats: ScanStats) -> None:
    """A file the plan asked about that couldn't be read: it was found, so
    it's neither changed nor gone, and no hash is sent. Counted as before the
    server compared: unchanged where a clip is, skipped where a person
    removed a file, else tried as new (failing, it's tried again later)."""
    planned = f["_plan"]
    if planned["asset_id"]:
        f["asset_id"] = planned["asset_id"]
        work.unchanged.append(f)
        stats.unchanged += 1
    elif planned["reason"] == "removed":
        work.skipped += 1
    else:
        work.new.append(f)


def _take(f: dict, d: dict, work: _Work, stats: ScanStats) -> None:
    """The server's decision for a file it was sent the hash of."""
    decision = d["decision"]
    if decision == "unchanged":
        f["asset_id"] = d["asset_id"]
        work.unchanged.append(f)
        stats.unchanged += 1
    elif decision == "upload" and d.get("asset_id"):
        f["asset_id"] = d["asset_id"]
        work.changed.append(f)
    elif decision == "upload":
        work.new.append(f)
    elif decision == "moved":
        work.moved += 1
    elif decision == "move_candidate":
        work.candidates.append(d)
    elif decision == "skipped":
        work.skipped += 1
    # move_skipped: a person said to skip it; it takes no part.


def _storage_gone(library: dict) -> bool:
    """Files that look missing or moved may only be the storage going away
    mid-scan (a clean unmount leaves an empty folder): it's looked at again
    before anything is moved or archived (Robert's call, Oct 8)."""
    return reachable_root(library, require_entries=True) is None


def _hash_and_decide(
    client: ApiClient, scan_id: str, library: dict, root_path: Path, to_hash: list[dict], work: _Work,
    stats: ScanStats, *, moves_now: bool, concurrency: int, console: Console,
) -> bool:
    """Read the files the plan asks about, in walk order, and send their
    hashes FILES_PER_CALL at a time for the server's decisions: when the
    scan applies moves they're made now, the first file sent taking the
    first clip. False when the storage went away before a move could be
    made: nothing more is sent."""
    if not to_hash:
        return True

    def decide(batch: list[dict]) -> bool:
        if moves_now and any(not f["_plan"]["asset_id"] for f in batch) and _storage_gone(library):
            return False
        sent = {f["rel_path"]: f for f in batch}
        body = {"files": [{"rel_path": f["rel_path"], "sha256": f["source_sha256"]} for f in batch]}
        for d in _post_again_if_taken(client, f"/v1/scans/{scan_id}/hashes", body)["files"]:
            _take(sent[d["rel_path"]], d, work, stats)
        return True

    batch: list[dict] = []
    progress = _progress(console, "Hashing")
    with progress, contextlib.closing(_hashed(root_path, to_hash, concurrency)) as hashes:
        tid = progress.add_task("Hashing", total=len(to_hash))
        for f, sha in hashes:
            progress.advance(tid)
            if sha is None:
                _unread(f, work, stats)
                continue
            f["source_sha256"] = sha
            batch.append(f)
            if len(batch) == FILES_PER_CALL:
                if not decide(batch):
                    return False
                batch = []
    return not batch or decide(batch)


def _upload(
    client: ApiClient, library_id: str, root_path: Path, work: _Work, proxy_cache: ProxyCache, stats: ScanStats,
    *, concurrency: int, console: Console,
) -> None:
    """Make and upload the previews of what's new, then what changed,
    `concurrency` at a time and never more than concurrency * 2 waiting."""
    total = len(work.new) + len(work.changed)
    if not total:
        return
    progress = _progress(console, "Scanning", eta=True)
    with progress, ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="scan") as pool:
        tid = progress.add_task("Scanning", total=total)
        inflight: set[Future] = set()
        for kind, files in (("new", work.new), ("changed", work.changed)):
            for f in files:
                handler = _scan_one_video if f["media_type"] == "video" else _scan_one
                inflight.add(pool.submit(
                    handler, client=client, library_id=library_id, root_path=root_path, f=f,
                    proxy_cache=proxy_cache, stats=stats, progress=progress, task_id=tid, counter_field=kind,
                ))
                if len(inflight) >= concurrency * 2:
                    _, inflight = _drain(inflight)
        while inflight:
            _, inflight = _drain(inflight)


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
    """Walk the library, tell the server what's there, read what it asks
    about, and upload what it says is new or changed.

    moves: allow_moves applies them (the scheduler's), skip_moves looks for
    none (and so archives nothing: anything gone may be half a move), else
    the server asks and so does this (the CLI's prompt). allow_mass_delete
    answers the server's question when most of a library looks gone.
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

    # Load path filters
    tenant_filters = _load_tenant_filters(client)
    library_filters = _load_library_filters(client, library_id)
    total_filters = len(tenant_filters) + len(library_filters)
    if total_filters:
        console.print(f"Loaded {len(tenant_filters)} tenant + {len(library_filters)} library filter(s)")

    # A folder that's gone is scanned from the nearest folder still there,
    # which sees what was in it as deleted.
    path_prefix = path_prefix.rstrip("/") if path_prefix else None
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

    # Started before the walk: a clip made or changed after this is never
    # taken for gone by it.
    moves = "apply" if allow_moves else "skip" if skip_moves else "ask"
    scan = client.post(f"/v1/libraries/{library_id}/scans", json={
        "scope": path_prefix or "", "media_type": media_type_filter, "thorough": thorough, "force": force,
        "moves": moves, "dry_run": dry_run,
    }).json()
    scan_id = scan["scan_id"]
    try:
        return _run(client, library, root_path, scan_id, stats, path_prefix=path_prefix,
                    follow_moves=scan.get("follow_moves", True), moves=moves, force=force,
                    media_type_filter=media_type_filter, dry_run=dry_run, allow_mass_delete=allow_mass_delete,
                    tenant_filters=tenant_filters, library_filters=library_filters, concurrency=concurrency,
                    console=console)
    except BaseException:
        _abandon(client, scan_id)
        raise


def _run(
    client: ApiClient, library: dict, root_path: Path, scan_id: str, stats: ScanStats, *,
    path_prefix: str | None, follow_moves: bool, moves: str, force: bool, media_type_filter: str, dry_run: bool,
    allow_mass_delete: bool, tenant_filters: list, library_filters: list, concurrency: int, console: Console,
) -> ScanStats:
    """run_scan once its scan is started: walk, list, plan, hash, finish, upload."""
    library_id = library["library_id"]
    if not follow_moves:
        console.print("[dim]This account doesn't follow moves and renames: files at new paths are new assets.[/dim]")

    console.print("[bold]Discovering files...[/bold]")
    walked = _walk_library(root_path, path_prefix, tenant_filters=tenant_filters, library_filters=library_filters,
                           unlisted=stats.unlisted)
    if stats.unlisted:
        console.print(f"[yellow]Couldn't list {len(stats.unlisted):,} folder(s), so this scan "
                      "doesn't remove anything: {}[/yellow]".format(", ".join(stats.unlisted[:5]) or "/"))

    # Every media file on disk is sent, of every media type, empty ones (on
    # disk, never ingested) and ones still being written (a later scan takes
    # them) too: none of them is gone.
    on_disk: dict[str, dict] = {}
    for f in walked:
        if f["rel_path"] not in on_disk and not _refused_name(f["rel_path"]):
            on_disk[f["rel_path"]] = f
    of_type = [f for f in on_disk.values() if media_type_filter in ("all", f["media_type"])]
    console.print(f"Found {len(of_type):,} media files")

    now = datetime.now(UTC)
    for f in on_disk.values():
        f["settling"] = _settling(f, now)
    stats.settling_paths = [f["rel_path"] for f in of_type if f["settling"]]
    stats.settling = len(stats.settling_paths)
    if stats.settling:
        console.print(f"{stats.settling:,} file(s) still being written; a later scan picks them up")

    _send_found(client, scan_id, list(on_disk.values()), console)
    counts, plan = _plan(client, scan_id, stats.unlisted, console)

    work = _Work()
    to_hash: list[dict] = []
    for f in of_type:
        planned = plan.get(f["rel_path"])
        if planned is None:  # still being written, or empty
            continue
        action = planned["action"]
        if action == "upload" and planned["asset_id"]:  # a video's preview never arrived, or --force
            f["asset_id"] = planned["asset_id"]
            work.changed.append(f)
        elif action == "upload":
            work.new.append(f)
        elif action == "hash":
            f["_plan"] = planned
            to_hash.append(f)
        elif action == "unchanged":
            f["asset_id"] = planned["asset_id"]
            work.unchanged.append(f)
        elif action == "skipped":
            work.skipped += 1
    stats.unchanged += len(work.unchanged)
    console.print(
        f"{len(work.new):,} new, "
        f"{len(to_hash):,} to check, "
        f"{len(work.unchanged):,} unchanged, "
        f"{counts['gone']:,} not found"
        + (f", {len(work.changed):,} to scan again" if work.changed else "")
    )

    # A move needs a clip gone from this scan; the server looks for them
    # when the account follows moves, unless the scan forces or skips them,
    # and makes them as the hashes arrive when the scan applies them.
    moves_now = moves == "apply" and follow_moves and not force and not dry_run and counts["gone"] > 0
    if not _hash_and_decide(client, scan_id, library, root_path, to_hash, work, stats, moves_now=moves_now,
                            concurrency=concurrency, console=console):
        console.print("[red]The library's storage went away during the scan, so nothing was archived or moved. "
                      "It's scanned again once it's back.[/red]")
        stats.root_unreachable = True
        _abandon(client, scan_id)
        return stats
    if work.skipped:
        console.print(f"Skipping {work.skipped:,} file(s) you removed")

    if work.candidates and dry_run:
        console.print(f"\n[yellow]{len(work.candidates):,} moved file(s) detected.[/yellow]")
        _show_moves(console, work.candidates)
        console.print("[dim]Use --allow-moves to apply, or --skip-moves to ignore.[/dim]")
    elif work.candidates:
        choice = _prompt_move_decision(console, work.candidates)
        if choice == "abort":
            console.print("[red]Scan aborted.[/red]")
            _abandon(client, scan_id)
            return stats
        if choice == "apply" and _storage_gone(library):
            console.print("[red]The library's storage went away during the scan, so nothing was archived or "
                          "moved. It's scanned again once it's back.[/red]")
            stats.root_unreachable = True
            _abandon(client, scan_id)
            return stats
        answer = _post_again_if_taken(client, f"/v1/scans/{scan_id}/moves", {"apply": choice == "apply"})
        work.moved += answer["moved"]
        work.new.extend(on_disk[rel] for rel in answer["upload"] if rel in on_disk)
        if answer["skipped"]:
            console.print(f"[dim]Skipping {answer['skipped']:,} moves[/dim]")

    if dry_run:
        finished = _answer(client, client.raw("POST", f"/v1/scans/{scan_id}/finish",
                                               json={"allow_mass_missing": False}))
        _abandon(client, scan_id)
        console.print(
            f"\n[bold]Scan summary:[/bold] "
            f"{len(work.new):,} new, "
            f"{len(work.changed):,} changed, "
            f"{stats.unchanged:,} unchanged, "
            f"{finished['archived']:,} deleted, "
            f"{finished['moved']:,} moved"
        )
        console.print(f"\n[bold]Root path:[/bold]  {root_path}")
        return stats
    stats.moved += work.moved

    # A whole library's scan that found nothing is more likely an unmounted
    # volume than an empty library: the server archives nothing, and the
    # storage is looked at again so the scheduler keeps the changes.
    nothing_found = not of_type and not force and not path_prefix
    if (counts["gone"] or nothing_found) and _storage_gone(library):
        if nothing_found:
            console.print("[red]The library's storage went away during the scan; it's scanned again once it's back.[/red]")
        else:
            console.print("[red]The library's storage went away during the scan, so nothing was archived or "
                          "moved. It's scanned again once it's back.[/red]")
        stats.root_unreachable = True
        _abandon(client, scan_id)
        return stats

    # What's gone is archived as missing ("missing", not the user's trash:
    # restored if the file reappears), all at once so the server's
    # mass-missing rule sees the whole scan. When it would take most of a
    # library the server asks first (409 mass_missing, usually a volume not
    # fully mounted): skipped, unless --allow-mass-delete says yes.
    resp = client.raw("POST", f"/v1/scans/{scan_id}/finish", json={"allow_mass_missing": allow_mass_delete})
    err = _error(resp) if resp.status_code == 409 else {}
    if err.get("code") == "mass_missing":
        stats.mass_missing = (err.get("details") or {}).get("count") or counts["gone"]
        console.print(f"[yellow]Skipping {stats.mass_missing:,} deletions: {err.get('message', '')} "
                      "If the files really are gone, re-run with --allow-mass-delete.[/yellow]")
        _abandon(client, scan_id)
    else:
        finished = _answer(client, resp)
        # Some may have moved to an empty copy of their file instead (copy, then delete).
        stats.deleted = finished["archived"]
        stats.moved += finished["handed_over"]
        held, why = finished["held_back"], finished.get("held_back_reason")
        if why == "unlisted":
            console.print(f"[yellow]Skipping {held:,} deletions: some folders couldn't be listed[/yellow]")
        elif why == "skip_moves":
            console.print(f"[dim]Skipping {held:,} deletions (--skip-moves)[/dim]")
        elif why == "nothing_found":
            console.print(f"[yellow]Found no media files, so {held:,} clip(s) aren't archived: "
                          "is the volume fully mounted?[/yellow]")
        if stats.deleted:
            console.print(f"Archived {stats.deleted:,} clip(s) no longer on disk")

    # After the finish: a file whose content was just archived as missing
    # comes back at its new path.
    proxy_cache = ProxyCache(root_path=root_path, client=client)
    _upload(client, library_id, root_path, work, proxy_cache, stats, concurrency=concurrency, console=console)
    _populate_cache_for_unchanged(client, work.unchanged, proxy_cache, stats, console)
    return stats
