#if os(macOS)
import Foundation

/// The Mac app's disk cache of the proxies it downloads for browsing: the
/// macOS implementation of `ProxyCache`, read before the server.
///
/// Its own (ADR-020 decision 7): `~/Library/Caches/io.lumiverb.app/proxies/`,
/// at most `defaultCapBytes`, the least recently used going first
/// (`BoundedDiskCache`). Until ADR-020 phase 5 it shared
/// `~/.cache/lumiverb/proxies/` with the Python CLI's scan, unbounded; what
/// was there is moved in once (`BoundedDiskCache.adopt`), without the CLI's
/// `.sha` sidecars, which the app never read.
///
/// File layout: `{asset_id}`, the JPEG proxy bytes (2048px max long edge).
///
/// **Renamed from `ProxyCacheOnDisk` in ADR-015 M1** so the iOS app can
/// have a `ProxyCache` protocol with two distinct implementations.
public final class MacProxyDiskCache: @unchecked Sendable {
    public static let shared = MacProxyDiskCache()

    /// About 3,000 proxies at 2048px.
    public static let defaultCapBytes = 1024 * 1024 * 1024

    private let files: BoundedDiskCache

    /// `cacheDir`: a folder of its own (tests); without it, the app's, with
    /// the old shared folder's proxies moved in.
    public init(cacheDir: URL? = nil, capBytes: Int = MacProxyDiskCache.defaultCapBytes) {
        files = BoundedDiskCache(directory: cacheDir ?? BoundedDiskCache.macDirectory("proxies"),
                                 highWatermarkBytes: capBytes)
        if cacheDir == nil {
            BoundedDiskCache.adopt(
                old: FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".cache/lumiverb/proxies"),
                into: files, dropping: [".sha"])
        }
    }

    public var directory: URL { files.directory }

    /// Cached proxy bytes for display, or nil.
    public func get(assetId: String) -> Data? { files.get(assetId) }

    /// Keep proxy bytes downloaded from the server for next time.
    public func put(assetId: String, data: Data) { files.put(assetId, data: data) }

    public func has(assetId: String) -> Bool { files.has(assetId) }

    /// The entry, and a `.sha` sidecar beside it (an old shared folder's).
    public func remove(assetId: String) { files.remove(assetId, alsoSuffixes: [".sha"]) }

    func settle() { files.settle() }
}

extension MacProxyDiskCache: ProxyCache {
    // The ProxyCache requirements (`get`, `put`, `has`, `remove`) are
    // already satisfied by the public methods above; this empty extension
    // declares conformance.
}
#endif
