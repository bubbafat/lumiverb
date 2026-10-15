"""What a scan would do with files, asked of the server without changing anything.

The server decides what a scan does with each file it finds (src/server/scans.py):
upload it, hash it first, leave it unchanged, or skip it because a person
trashed, archived or deleted it for good. A test about what a scan must
respect asks the real scan calls (routers/scans.py), in a dry run dropped after.
A test of what a scan that adds removed files anyway does (force_add) runs
one for real, as far as its decisions (add_anyway).
"""

from __future__ import annotations


def _ok(r) -> dict:
    assert r.status_code == 200, r.text
    return r.json()


def scan_would(client, headers: dict, library_id: str, files: list[dict],
               hashes: dict[str, str] | None = None, *, force_add: bool = False,
               dry_run: bool = True) -> dict[str, str]:
    """Each file's action in a dry-run scan's plan ("upload", "hash",
    "unchanged" or "skipped"; files: [{rel_path, file_size, file_mtime?}]).
    A file the plan hashes takes the decision for its sha256 in hashes
    ("skipped", "upload", "unchanged" or, adding removed files anyway,
    "restored"). No moves are looked for: a file's own path decides."""
    scan_id = _ok(client.post(f"/v1/libraries/{library_id}/scans",
                              json={"dry_run": dry_run, "moves": "skip", "force_add": force_add},
                              headers=headers))["scan_id"]
    try:
        _ok(client.post(f"/v1/scans/{scan_id}/found", json={"files": files}, headers=headers))
        plan = _ok(client.post(f"/v1/scans/{scan_id}/plan", json={}, params={"action": "upload,hash,unchanged,skipped"},
                               headers=headers))
        assert plan["next_cursor"] is None, "one page of files"
        would = {f["rel_path"]: f["action"] for f in plan["files"]}
        sent = [{"rel_path": p, "sha256": sha} for p, sha in (hashes or {}).items() if would.get(p) == "hash"]
        if sent:
            for d in _ok(client.post(f"/v1/scans/{scan_id}/hashes", json={"files": sent}, headers=headers))["files"]:
                would[d["rel_path"]] = d["decision"]
        return would
    finally:
        r = client.delete(f"/v1/scans/{scan_id}", headers=headers)
        assert r.status_code == 204, r.text


def add_anyway(client, headers: dict, library_id: str, files: list[dict],
               hashes: dict[str, str]) -> dict[str, str]:
    """A scan that adds the files a person removed anyway (force_add, not a
    dry run), as far as its decisions: what it decided for each file, its
    clips restored and its files forgotten as deleted. Dropped then, never
    finished, so nothing else is taken for gone; ingest is the caller's."""
    return scan_would(client, headers, library_id, files, hashes, force_add=True, dry_run=False)
