# Preview cache

The scheduler's own disk cache of clips' previews (`src/processing/proxy/proxy_cache.py`), for a scheduler on a machine apart from the API. Nothing else writes it: a client keeps no copy of the scheduler's (ADR-020 decision 7). The CLI's `lumiverb scan` caches nothing, and the Mac app keeps its own browse cache (below).

**Beside the API there's none.** Where the API keeps the previews on the same machine (`STORAGE_PROVIDER=local`, as on a Mac's install), a job reads each clip's stored preview from the Store, by the key its queue row carries (`src/server/scheduler/inputs.py` `Inputs.preview_bytes`), sized for CLIP, faces, OCR and descriptions; this cache isn't asked. Its scans write nothing here and don't list unchanged files (`run_scan`, `cache_previews` false), and at its start it removes the copies an earlier scheduler kept when its cache is inside the server's data folder (`forget_preview_copies`).

**Apart from the API** (`cache_previews` true, `src/server/scheduler/scans.py`), its scans keep each preview they make and download the unchanged clips', and its jobs read them here, downloading one it lacks (`GET /v1/assets/{asset_id}/artifacts/lumiverb.preview`, by its name: ADR-024).

## Location

```
$XDG_CACHE_HOME/lumiverb/proxies/
```

An install sets `XDG_CACHE_HOME` to its data disk (`DATA_DIR/cache`); without it, `~/.cache`. The scheduler's lock and state are beside it (`src/processing/cache_dir.py`).

## Cap

At most `LUMIVERB_PREVIEW_CACHE_GB` (2 by default): past it the least recently used proxies go, each with its sidecar, checked on the first write and every 50th after. A sidecar's size counts toward it.

## File layout

For each asset:

| File | Contents | Purpose |
|------|----------|---------|
| `{asset_id}` | JPEG image bytes (no extension) | 2048px max long edge proxy image |
| `{asset_id}.sha` | The source file's content hash (UTF-8, no newline): `sha256:` and 64 hex, or `fp1:` and 64 for a file over 64 MiB (a bare 64 hex, written before hashes named their scheme, is read as `sha256:`) | The source's content hash when the proxy was made (`src/shared/content_hash.py`) |

## Operations

### Write (a scan's, or a download)

1. Make the 2048px JPEG proxy from the source (a scan), or download the server's.
2. Write the proxy atomically: a temp file in the same folder, renamed to `{asset_id}`.
3. Write the sidecar the same way, **after** the proxy: a reader that sees a `.sha` can trust the proxy is whole.

### Read (a job's)

1. Read `{asset_id}`, sized to the job's edge.
2. A sidecar naming other content than the clip's (`content_hash`) isn't handed out: the server's copy is downloaded and kept, with its hash.

### Delete

Remove both `{asset_id}` and `{asset_id}.sha`.

## Constraints

- JPEG, not WebP, for local use; the server keeps its own WebP preview and makes its own thumbnail.
- At most 2048px on the long edge (the server's preview size); JPEG quality 75.
- No subfolders: every file flat in the folder.
- Asset ids are checked as plain names before they're used in a path (`cache_dir.cache_entry`).

## The apps' caches

Each app keeps its own, bounded (`BoundedDiskCache` in LumiverbKit: past its cap the least recently used go, a read counting as a use):

| Cache | Where | Cap |
|---|---|---|
| Mac proxies (`MacProxyDiskCache`) | `~/Library/Caches/io.lumiverb.app/proxies/` | 1 GB |
| Mac thumbnails (`MacThumbnailDiskCache`) | `~/Library/Caches/io.lumiverb.app/thumbnails/` | 500 MB |
| iOS thumbnails (`IOSThumbnailDiskCache`) | the app's caches folder, `lumiverb/thumbnails/` | 200 MB |

iOS keeps its proxies in memory only. The Mac app's caches were in `~/.cache/lumiverb/` before ADR-020 phase 5, the proxies beside what the CLI's scan wrote, with no cap; at its first start after, what was there is moved in (renamed, not copied), without the CLI's `.sha` sidecars, and trimmed to the cap.
