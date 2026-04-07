# Proxy Cache Protocol

Shared disk cache protocol for proxy images. All Lumiverb clients (Python CLI, macOS app, future clients) use the same format and location so proxies are reusable across clients.

## Location

```
~/.cache/lumiverb/proxies/
```

## File Layout

For each asset:

| File | Contents | Purpose |
|------|----------|---------|
| `{asset_id}` | JPEG image bytes (no extension) | 2048px max long edge proxy image |
| `{asset_id}.sha` | 64-char hex SHA-256 string (UTF-8, no newline) | SHA-256 of the source file at time of proxy generation |

## Operations

### Write (during scan)

1. Generate 2048px JPEG proxy from source file
2. Compute SHA-256 of source file
3. Atomic write proxy: write to temp file in same directory, then `rename()` to `{asset_id}`
4. Atomic write sidecar: write to temp file, then `rename()` to `{asset_id}.sha`
5. Sidecar written **after** proxy — a reader that sees a `.sha` file can trust the proxy is complete

### Read (during scan — skip check)

1. Read `{asset_id}.sha` → cached SHA
2. Compare cached SHA with current source file SHA
3. If match → proxy is valid, skip regeneration
4. If mismatch or missing → regenerate proxy and update both files

### Read (during browse — display)

1. Check if `{asset_id}` exists in cache
2. If yes → load from disk (fast, no network)
3. If no → download from server via `GET /v1/assets/{asset_id}/proxy`, cache for next time

### Delete

Remove both `{asset_id}` and `{asset_id}.sha`.

## Constraints

- Proxy images are JPEG format (not WebP) for local consumption. The server receives WebP via the ingest endpoint and generates its own proxy/thumbnail.
- Maximum long edge: 2048px (matches server proxy size)
- JPEG quality: 75
- No subdirectories — all files flat in the proxies directory
- Asset IDs are UUIDs, safe for filenames on all platforms
- Atomic writes prevent partial reads from concurrent access (Python CLI + macOS app can run simultaneously)

## Compatibility

Both the Python CLI (`src/client/proxy/proxy_cache.py`) and the Swift macOS app (`ProxyCacheOnDisk`) implement this protocol. Files written by either client are readable by the other.
