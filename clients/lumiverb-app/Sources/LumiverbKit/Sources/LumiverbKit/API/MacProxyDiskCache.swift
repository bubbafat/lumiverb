#if os(macOS)
import Foundation

/// Persistent disk cache for proxy images, compatible with the Python CLI's
/// proxy cache. This is the macOS implementation of `ProxyCache`.
///
/// It shares `~/.cache/lumiverb/proxies/` with the Python CLI, whose scan
/// writes the proxies it makes there; the app reads them, and keeps the
/// proxies it downloads there for browsing. See
/// `docs/proxy-cache-protocol.md`.
///
/// File layout per asset:
/// - `{asset_id}` — JPEG proxy bytes (2048px max long edge)
/// - `{asset_id}.sha` — SHA-256 hex string of the source file (the CLI's
///   scan writes it; the app only removes it with its proxy)
///
/// **Renamed from `ProxyCacheOnDisk` in ADR-015 M1** so the iOS app can
/// have a `ProxyCache` protocol with two distinct implementations.
public final class MacProxyDiskCache: @unchecked Sendable {
    public static let shared = MacProxyDiskCache()

    private let cacheDir: URL

    public init(cacheDir: URL? = nil) {
        if let cacheDir {
            self.cacheDir = cacheDir
        } else {
            self.cacheDir = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent(".cache/lumiverb/proxies")
        }
        try? FileManager.default.createDirectory(
            at: self.cacheDir,
            withIntermediateDirectories: true
        )
    }

    // MARK: - Browse operations

    /// Get cached proxy bytes for display. Returns nil if not cached.
    public func get(assetId: String) -> Data? {
        guard let url = proxyURL(assetId) else { return nil }
        return try? Data(contentsOf: url)
    }

    /// Cache proxy bytes downloaded from the server for future display.
    /// Writes are atomic (temp + rename) so concurrent readers never see
    /// partial files.
    public func put(assetId: String, data: Data) {
        guard let url = proxyURL(assetId) else { return }
        atomicWrite(data: data, to: url)
    }

    /// Check if a proxy exists in the cache.
    public func has(assetId: String) -> Bool {
        proxyURL(assetId).map { FileManager.default.fileExists(atPath: $0.path) } ?? false
    }

    // MARK: - Maintenance

    /// Remove a single entry and its SHA sidecar.
    public func remove(assetId: String) {
        guard let proxy = proxyURL(assetId), let sha = shaURL(assetId) else { return }
        try? FileManager.default.removeItem(at: proxy)
        try? FileManager.default.removeItem(at: sha)
    }

    // MARK: - Paths

    private func proxyURL(_ assetId: String) -> URL? {
        cacheEntryURL(cacheDir, assetId)
    }

    private func shaURL(_ assetId: String) -> URL? {
        cacheEntryURL(cacheDir, assetId, suffix: ".sha")
    }

    // MARK: - Atomic write

    private func atomicWrite(data: Data, to destination: URL) {
        let tempURL = cacheDir.appendingPathComponent(UUID().uuidString + ".tmp")
        do {
            try data.write(to: tempURL, options: .atomic)
            // .atomic already writes to temp + renames, but we do our own
            // to match the Python protocol exactly
            if FileManager.default.fileExists(atPath: destination.path) {
                try FileManager.default.removeItem(at: destination)
            }
            try FileManager.default.moveItem(at: tempURL, to: destination)
        } catch {
            try? FileManager.default.removeItem(at: tempURL)
        }
    }
}

extension MacProxyDiskCache: ProxyCache {
    // The ProxyCache requirements (`get`, `put`, `has`, `remove`) are
    // already satisfied by the public methods above; this empty extension
    // declares conformance.
}
#endif
