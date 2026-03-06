"""Core scan logic: walk filesystem, upsert assets via API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from src.core.file_extensions import SUPPORTED_EXTENSIONS, VIDEO_EXTENSIONS

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


def _file_mtime_iso(path: Path) -> str | None:
    """Return file mtime as UTC ISO8601 string, or None on error."""
    try:
        st = path.stat()
        dt = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
        return dt.isoformat()
    except OSError:
        return None


def scan_library(
    client: object,
    library: dict,
    path_override: str | None = None,
    force: bool = False,
    worker_id: str | None = None,
) -> ScanResult:
    """
    Scan a library: validate root, check for running scans, create scan record,
    walk filesystem, upsert each file, complete or abort scan.
    """
    library_id = library.get("library_id", "")
    root_path_str = library.get("root_path", "")
    root_path = Path(root_path_str)

    # Root reachability
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

    # Running scan conflict (unless force)
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

    # Create scan record
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

    library_root_resolved = root_path.resolve()
    walk_root = root_path / path_override if path_override else root_path
    try:
        walk_root_resolved = walk_root.resolve()
    except OSError:
        walk_root_resolved = walk_root
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

    files_discovered = 0
    files_added = 0
    files_updated = 0
    files_skipped = 0

    try:
        all_files: list[tuple[Path, str, int, str | None, str]] = []
        for p in Path(walk_root_resolved).rglob("*"):
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            try:
                rel_path = p.relative_to(library_root_resolved)
            except ValueError:
                continue
            rel_path_str = str(rel_path).replace("\\", "/")
            media_type = "video" if ext in VIDEO_EXTENSIONS else "image"
            try:
                st = p.stat()
                file_size = st.st_size
                file_mtime = _file_mtime_iso(p)
            except OSError:
                continue
            all_files.append((p, rel_path_str, file_size, file_mtime, media_type))

        use_progress = console.is_terminal
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
            disable=not use_progress,
        )
        with progress:
            task = progress.add_task("Scanning...", total=len(all_files))
            for p, rel_path_str, file_size, file_mtime, media_type in all_files:
                progress.update(task, description=f"Scanning: {rel_path_str}", advance=1)
                files_discovered += 1
                upsert_body = {
                    "library_id": library_id,
                    "rel_path": rel_path_str,
                    "file_size": file_size,
                    "file_mtime": file_mtime,
                    "media_type": media_type,
                    "scan_id": scan_id,
                    "force": force,
                }
                resp = client.post("/v1/assets/upsert", json=upsert_body)
                action = resp.json().get("action", "updated")
                if action == "added":
                    files_added += 1
                elif action == "updated":
                    files_updated += 1
                else:
                    files_skipped += 1

        # Complete scan
        complete_body = {
            "files_discovered": files_discovered,
            "files_added": files_added,
            "files_updated": files_updated,
            "files_skipped": files_skipped,
        }
        resp = client.post(f"/v1/scans/{scan_id}/complete", json=complete_body)
        data = resp.json()
        files_missing = data.get("files_missing", 0)
        return ScanResult(
            scan_id=scan_id,
            files_discovered=files_discovered,
            files_added=files_added,
            files_updated=files_updated,
            files_skipped=files_skipped,
            files_missing=files_missing,
            status="complete",
        )
    except Exception as e:
        try:
            client.post(
                f"/v1/scans/{scan_id}/abort",
                json={"error_message": str(e)},
            )
        except Exception:
            pass
        return ScanResult(
            scan_id=scan_id,
            files_discovered=files_discovered,
            files_added=files_added,
            files_updated=files_updated,
            files_skipped=files_skipped,
            files_missing=0,
            status="error",
            error_message=str(e),
        )
