import Foundation

/// Persistent (or in-memory) cache for full-resolution proxy JPEGs.
///
/// **Per-platform behavior:**
/// - macOS uses `MacProxyDiskCache` — the app's own disk cache at
///   `~/Library/Caches/io.lumiverb.app/proxies/`, capped at 1 GB, least
///   recently used first (`BoundedDiskCache`).
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
/// - macOS uses `MacThumbnailDiskCache` — disk cache at
///   `~/Library/Caches/io.lumiverb.app/thumbnails/`, capped at 500 MB.
/// - iOS uses `IOSThumbnailDiskCache` — disk cache in `.cachesDirectory`
///   capped at ~200 MB.
/// Both are a `BoundedDiskCache`: least recently used (oldest by mtime) first.
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

/// `dir/<assetId><suffix>`, or nil when the id isn't a plain name
/// (`[A-Za-z0-9_-]+`). Asset ids come from the server, so one like ".." or
/// "a/b" never becomes a path out of the cache.
func cacheEntryURL(_ dir: URL, _ assetId: String, suffix: String = "") -> URL? {
    guard !assetId.isEmpty, assetId.unicodeScalars.allSatisfy({ assetIdScalars.contains($0) }) else {
        return nil
    }
    return dir.appendingPathComponent(assetId + suffix)
}

private let assetIdScalars = CharacterSet(
    charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
