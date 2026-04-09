import Foundation

/// Persistent disk cache for asset thumbnails.
///
/// Distinct from `ProxyCacheOnDisk` because proxies follow a
/// Python-CLI-compatible protocol (SHA sidecars, shared cache dir under
/// `~/.cache/lumiverb/proxies/`), while thumbnails are macOS-app-local
/// and don't need that ceremony:
///
/// - No SHA sidecar — thumbnails are cached by asset id only. The server
///   regenerates thumbnails when an asset changes and gives them a new
///   asset id only when the file identity changes, so staleness on long-
///   lived assets is acceptable. Call `removeAll()` for a manual flush.
/// - No eviction policy — cache grows unbounded. Thumbnails are tiny
///   (~10-30 KB). 20 000 assets ≈ 400 MB, which is reasonable for a
///   media management app. Eviction can come later if it matters.
///
/// The cache dir is `~/.cache/lumiverb/thumbnails/`. Files are named by
/// raw asset id; writes are atomic (temp + rename) so concurrent readers
/// never see partial files.
///
/// Without this cache, every library switch re-fetches every visible
/// thumbnail from the server — a latent UX trap against remote servers
/// that manifests as a multi-second "library click does nothing" stall
/// for browse sessions that span an `NSCache` eviction or app restart.
public final class ThumbnailCacheOnDisk: @unchecked Sendable {
    public static let shared = ThumbnailCacheOnDisk()

    private let cacheDir: URL

    public init(cacheDir: URL? = nil) {
        if let cacheDir {
            self.cacheDir = cacheDir
        } else {
            self.cacheDir = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".cache/lumiverb/thumbnails")
        }
        try? FileManager.default.createDirectory(
            at: self.cacheDir,
            withIntermediateDirectories: true
        )
    }

    // MARK: - Browse operations

    /// Get cached thumbnail bytes. Returns nil if not cached.
    public func get(assetId: String) -> Data? {
        let url = thumbnailURL(assetId)
        return try? Data(contentsOf: url)
    }

    /// Cache thumbnail bytes downloaded from the server.
    public func put(assetId: String, data: Data) {
        atomicWrite(data: data, to: thumbnailURL(assetId))
    }

    /// Check if a thumbnail exists in the cache.
    public func has(assetId: String) -> Bool {
        FileManager.default.fileExists(atPath: thumbnailURL(assetId).path)
    }

    // MARK: - Maintenance

    /// Remove a single entry.
    public func remove(assetId: String) {
        try? FileManager.default.removeItem(at: thumbnailURL(assetId))
    }

    /// Remove every cached thumbnail. Useful after a server-side change
    /// that invalidates thumbnails (e.g. changing the thumbnail endpoint's
    /// target size).
    public func removeAll() {
        try? FileManager.default.removeItem(at: cacheDir)
        try? FileManager.default.createDirectory(
            at: cacheDir,
            withIntermediateDirectories: true
        )
    }

    // MARK: - Paths

    private func thumbnailURL(_ assetId: String) -> URL {
        cacheDir.appendingPathComponent(assetId)
    }

    // MARK: - Atomic write

    private func atomicWrite(data: Data, to destination: URL) {
        let tempURL = cacheDir.appendingPathComponent(UUID().uuidString + ".tmp")
        do {
            try data.write(to: tempURL, options: .atomic)
            if FileManager.default.fileExists(atPath: destination.path) {
                try FileManager.default.removeItem(at: destination)
            }
            try FileManager.default.moveItem(at: tempURL, to: destination)
        } catch {
            try? FileManager.default.removeItem(at: tempURL)
        }
    }
}
