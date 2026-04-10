import Foundation

/// Persistent (or in-memory) cache for full-resolution proxy JPEGs.
///
/// **Per-platform behavior:**
/// - macOS uses `MacProxyDiskCache` — unbounded disk cache at
///   `~/.cache/lumiverb/proxies/`, shared with the Python CLI via SHA
///   sidecars. The scan-specific methods (`putScan`, `getSHA`, `isValid`)
///   are NOT part of this protocol; enrichment code uses the concrete
///   `MacProxyDiskCache` type directly.
/// - iOS uses `MemoryImageCache` (proxy slot) — in-memory NSCache only,
///   ~150 MB budget, no disk fallback.
public protocol ProxyCache: Sendable {
    /// Get cached proxy bytes. Returns nil if not cached.
    func get(assetId: String) -> Data?
    /// Cache proxy bytes. Implementations may evict to satisfy budgets.
    func put(assetId: String, data: Data)
    /// Whether `get(assetId:)` would currently return non-nil.
    func has(assetId: String) -> Bool
    /// Remove a single entry. No-op if absent.
    func remove(assetId: String)
}

/// Persistent (or in-memory) cache for thumbnail JPEGs.
///
/// **Per-platform behavior:**
/// - macOS uses `MacThumbnailDiskCache` — unbounded disk cache at
///   `~/.cache/lumiverb/thumbnails/`. No sidecars (thumbnails are macOS-app-
///   local, not shared with the CLI).
/// - iOS uses `IOSThumbnailDiskCache` — disk cache in `.cachesDirectory`
///   capped at ~200 MB with approximate-LRU (oldest-by-mtime) eviction.
public protocol ThumbnailCache: Sendable {
    func get(assetId: String) -> Data?
    func put(assetId: String, data: Data)
    func has(assetId: String) -> Bool
    func remove(assetId: String)
    /// Remove every cached thumbnail. Used when the server-side thumbnail
    /// rendering changes in a way that invalidates the existing cache.
    func removeAll()
}

/// A bundle of caches injected via SwiftUI environment so views don't need
/// to thread two protocol-existential parameters everywhere.
///
/// macOS app:
/// `CacheBundle(proxies: MacProxyDiskCache.shared, thumbnails: MacThumbnailDiskCache.shared)`
///
/// iOS app:
/// `CacheBundle(proxies: MemoryImageCache(name: "ios.proxies"), thumbnails: IOSThumbnailDiskCache())`
public struct CacheBundle: Sendable {
    public let proxies: any ProxyCache
    public let thumbnails: any ThumbnailCache

    public init(proxies: any ProxyCache, thumbnails: any ThumbnailCache) {
        self.proxies = proxies
        self.thumbnails = thumbnails
    }
}
