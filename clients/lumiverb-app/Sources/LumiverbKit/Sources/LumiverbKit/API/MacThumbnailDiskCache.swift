#if os(macOS)
import Foundation

/// Persistent disk cache for asset thumbnails on macOS: the macOS
/// implementation of `ThumbnailCache`.
///
/// Its own (ADR-020 decision 7): `~/Library/Caches/io.lumiverb.app/thumbnails/`,
/// at most `defaultCapBytes`, the least recently used going first
/// (`BoundedDiskCache`). What was in `~/.cache/lumiverb/thumbnails/`
/// (before ADR-020 phase 5, unbounded) is moved in once.
///
/// - No sidecar: thumbnails are cached by asset id only. The server
///   regenerates thumbnails when an asset changes and gives them a new
///   asset id only when the file identity changes, so staleness on long-
///   lived assets is acceptable. Call `removeAll()` for a manual flush.
/// - Files are named by raw asset id; writes are atomic (temp + rename) so
///   concurrent readers never see partial files.
///
/// Without this cache, every library switch re-fetches every visible
/// thumbnail from the server — a latent UX trap against remote servers
/// that manifests as a multi-second "library click does nothing" stall
/// for browse sessions that span an `NSCache` eviction or app restart.
///
/// **Renamed from `ThumbnailCacheOnDisk` in ADR-015 M1.** Same on-disk
/// layout, same behavior — only the type name changed so the iOS app
/// can have a `ThumbnailCache` protocol with a distinct implementation.
public final class MacThumbnailDiskCache: @unchecked Sendable {
    public static let shared = MacThumbnailDiskCache()

    /// About 25,000 thumbnails (20 KB each): a big library's grid.
    public static let defaultCapBytes = 500 * 1024 * 1024

    private let files: BoundedDiskCache

    /// `cacheDir`: a folder of its own (tests); without it, the app's, with
    /// the old folder's thumbnails moved in.
    public init(cacheDir: URL? = nil, capBytes: Int = MacThumbnailDiskCache.defaultCapBytes) {
        files = BoundedDiskCache(directory: cacheDir ?? BoundedDiskCache.macDirectory("thumbnails"),
                                 highWatermarkBytes: capBytes)
        if cacheDir == nil {
            BoundedDiskCache.adopt(
                old: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/lumiverb/thumbnails"),
                into: files)
        }
    }

    public var directory: URL { files.directory }

    /// Cached thumbnail bytes, or nil.
    public func get(assetId: String) -> Data? { files.get(assetId) }

    /// Keep thumbnail bytes downloaded from the server.
    public func put(assetId: String, data: Data) { files.put(assetId, data: data) }

    public func has(assetId: String) -> Bool { files.has(assetId) }

    public func remove(assetId: String) { files.remove(assetId) }

    /// Remove every cached thumbnail. Useful after a server-side change
    /// that invalidates thumbnails (e.g. changing the thumbnail endpoint's
    /// target size).
    public func removeAll() { files.removeAll() }

    func settle() { files.settle() }
}

extension MacThumbnailDiskCache: ThumbnailCache {
    // The ThumbnailCache requirements (`get`, `put`, `has`, `remove`,
    // `removeAll`) are already satisfied by the public methods above;
    // this empty extension declares conformance.
}
#endif
