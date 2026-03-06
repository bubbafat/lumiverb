"""Core scan logic: walk filesystem, bulk reconcile via API."""

from __future__ import annotations

import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.core.file_extensions import SUPPORTED_EXTENSIONS, VIDEO_EXTENSIONS

SCAN_PAGE_SIZE = 500
SCAN_BATCH_SIZE = 500

console = Console()


@dataclass
class ScanResult:
    scan_id: str
    files_discovered: int
    files_added: int
    files_updated: int
    files_skipped: int
    files_missing: int
    status: str
    error_message: str | None = None


def _is_unchanged(asset: dict, local_file: dict, force: bool) -> bool:
    """Return True if asset matches local file (skip); False if needs update."""
    if force:
        return False
    if asset.get("sha256") is None:
        return (
            asset.get("file_size") == local_file["file_size"]
            and asset.get("file_mtime") == local_file["file_mtime"]
        )
    return (
        asset.get("file_size") == local_file["file_size"]
        and asset.get("file_mtime") == local_file["file_mtime"]
    )


def _build_local_map(root_path: Path, walk_root: Path) -> dict[str, dict]:
    """Walk filesystem and build rel_path -> {file_size, file_mtime, media_type} map."""
    local_map: dict[str, dict] = {}
    for p in walk_root.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        try:
            rel_path = p.relative_to(root_path)
        except ValueError:
            continue
        rel_path_str = str(rel_path).replace("\\", "/")
        media_type = "video" if ext in VIDEO_EXTENSIONS else "image"
        try:
            st = p.stat()
            dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
            file_mtime = dt.isoformat()
        except OSError:
            continue
        local_map[rel_path_str] = {
            "file_size": st.st_size,
            "file_mtime": file_mtime,
            "media_type": media_type,
        }
    return local_map


def scan_library(
    client: object,
    library: dict,
    path_override: str | None = None,
    force: bool = False,
    worker_id: str | None = None,
) -> ScanResult:
    """
    Scan a library: build local map, check root, check running scans, create scan,
    page through server assets, reconcile, add remaining, complete.
    """
    scan_id: str | None = None
    library_id = library.get("library_id", "")
    root_path_str = library.get("root_path", "")
    root_path = Path(root_path_str)

    # Step 1: Root reachability
    if not root_path.is_dir():
        err_msg = f"Library root unreachable: {root_path_str}"
        resp = client.post(
            "/v1/scans",
            json={
                "library_id": library_id,
                "status": "aborted",
                "error_message": err_msg,
            },
        )
        data = resp.json()
        return ScanResult(
            scan_id=data.get("scan_id", ""),
            files_discovered=0,
            files_added=0,
            files_updated=0,
            files_skipped=0,
            files_missing=0,
            status="aborted",
            error_message=err_msg,
        )

    # Step 2: Path override validation and walk root
    library_root_resolved = root_path.resolve()
    walk_root = root_path / path_override if path_override else root_path
    try:
        walk_root_resolved = walk_root.resolve()
    except OSError:
        walk_root_resolved = walk_root
    try:
        walk_root_resolved.relative_to(library_root_resolved)
    except ValueError:
        # Will create scan after signal handler setup; abort with error
        pass

    # Step 3: Running scan conflict (unless force)
    if not force:
        resp = client.get(f"/v1/scans/running?library_id={library_id}")
        running = resp.json()
        if running:
            for s in running:
                console.print(
                    f"[yellow]Warning: scan already running: {s.get('scan_id')} "
                    f"started_at={s.get('started_at')} worker_id={s.get('worker_id') or '—'}[/yellow]"
                )
            try:
                answer = input("A scan is already running. Proceed anyway? [yN] ").strip().lower()
            except EOFError:
                answer = "n"
            if answer != "y":
                return ScanResult(
                    scan_id="",
                    files_discovered=0,
                    files_added=0,
                    files_updated=0,
                    files_skipped=0,
                    files_missing=0,
                    status="aborted",
                )

    def _abort_handler(signum: int, frame: object) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            sig_name = str(signum)
        if scan_id is not None:
            try:
                client.post(
                    f"/v1/scans/{scan_id}/abort",
                    json={"error_message": f"Scan aborted by signal {sig_name}"},
                )
            except Exception:
                pass
        console.print(f"\n[yellow]Scan aborted ({sig_name}).[/yellow]")
        raise SystemExit(130 if signum == signal.SIGINT else 143)

    old_sigint = signal.signal(signal.SIGINT, _abort_handler)
    old_sigterm = signal.signal(signal.SIGTERM, _abort_handler)

    try:
        # Step 4: Create scan record
        body = {
            "library_id": library_id,
            "status": "running",
            "root_path_override": path_override,
            "worker_id": worker_id,
        }
        resp = client.post("/v1/scans", json=body)
        data = resp.json()
        scan_id = data.get("scan_id", "")
        if not scan_id:
            return ScanResult(
                scan_id="",
                files_discovered=0,
                files_added=0,
                files_updated=0,
                files_skipped=0,
                files_missing=0,
                status="error",
                error_message="No scan_id returned",
            )

        # Path override escape check (after scan created)
        try:
            walk_root_resolved.relative_to(library_root_resolved)
        except ValueError:
            err_msg = "Path override escapes library root"
            try:
                client.post(f"/v1/scans/{scan_id}/abort", json={"error_message": err_msg})
            except Exception:
                pass
            return ScanResult(
                scan_id=scan_id,
                files_discovered=0,
                files_added=0,
                files_updated=0,
                files_skipped=0,
                files_missing=0,
                status="error",
                error_message=err_msg,
            )

        # Step 5: Build local map (no API calls)
        use_progress = console.is_terminal
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            disable=not use_progress,
        ) as progress:
            task = progress.add_task("Building file map...", total=None)
            local_map = _build_local_map(library_root_resolved, walk_root_resolved)
            progress.update(task, description="File map built", completed=True)

        # Step 6: Page through server assets and reconcile
        cursor = None
        while True:
            params = {"library_id": library_id, "limit": SCAN_PAGE_SIZE}
            if cursor:
                params["after"] = cursor
            resp = client.get("/v1/assets/page", params=params)
            if resp.status_code == 204:
                break
            page = resp.json()
            if not page:
                break

            batch_items = []
            for asset in page:
                rel_path = asset["rel_path"]
                if rel_path in local_map:
                    local_file = local_map.pop(rel_path)
                    if _is_unchanged(asset, local_file, force):
                        batch_items.append({"action": "skip", "asset_id": asset["asset_id"]})
                    else:
                        batch_items.append({
                            "action": "update",
                            "asset_id": asset["asset_id"],
                            "file_size": local_file["file_size"],
                            "file_mtime": local_file["file_mtime"],
                        })
                else:
                    batch_items.append({"action": "missing", "asset_id": asset["asset_id"]})

            if batch_items:
                client.post(f"/v1/scans/{scan_id}/batch", json={"items": batch_items})

            cursor = page[-1]["asset_id"]

        # Step 7: Add remaining new files (local_map now only has files server doesn't know)
        new_items = [
            {
                "action": "add",
                "rel_path": rel_path,
                "file_size": info["file_size"],
                "file_mtime": info["file_mtime"],
                "media_type": info["media_type"],
            }
            for rel_path, info in local_map.items()
        ]
        for i in range(0, len(new_items), SCAN_BATCH_SIZE):
            batch = new_items[i : i + SCAN_BATCH_SIZE]
            client.post(f"/v1/scans/{scan_id}/batch", json={"items": batch})

        # Step 8: Complete scan
        resp = client.post(f"/v1/scans/{scan_id}/complete", json={})
        data = resp.json()
        return ScanResult(
            scan_id=data.get("scan_id", scan_id),
            files_discovered=data.get("files_discovered", 0),
            files_added=data.get("files_added", 0),
            files_updated=data.get("files_updated", 0),
            files_skipped=data.get("files_skipped", 0),
            files_missing=data.get("files_missing", 0),
            status=data.get("status", "complete"),
        )
    except Exception as e:
        if scan_id is not None:
            try:
                client.post(
                    f"/v1/scans/{scan_id}/abort",
                    json={"error_message": str(e)},
                )
            except Exception:
                pass
        return ScanResult(
            scan_id=scan_id or "",
            files_discovered=0,
            files_added=0,
            files_updated=0,
            files_skipped=0,
            files_missing=0,
            status="error",
            error_message=str(e),
        )
    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)
