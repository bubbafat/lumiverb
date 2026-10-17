#if os(iOS)
import Foundation

/// Disk-backed thumbnail cache for iOS, sized for the sandboxed app
/// container. Distinct from `MacThumbnailDiskCache` only in where it is and
/// its cap; both are a `BoundedDiskCache`:
///
/// - **Path:** `FileManager.default.urls(for: .cachesDirectory, ...)` —
///   iOS apps cannot write to `~/.cache/`, and `cachesDirectory` lets the
///   OS purge under storage pressure.
/// - **Bounded:** ~200 MB with approximate-LRU eviction (oldest by
///   `contentModificationDate`; a read touches it), down to 180 MB.
/// - **No sidecars.**
public final class IOSThumbnailDiskCache: @unchecked Sendable, ThumbnailCache {
    public var highWatermarkBytes: Int { files.highWatermarkBytes }
    public var lowWatermarkBytes: Int { files.lowWatermarkBytes }

    private let files: BoundedDiskCache

    public init(
        highWatermarkBytes: Int = 200 * 1024 * 1024,
        lowWatermarkBytes: Int = 180 * 1024 * 1024,
        cacheDir: URL? = nil
    ) {
        let directory = cacheDir ?? (FileManager.default
            .urls(for: .cachesDirectory, in: .userDomainMask)
            .first ?? URL(fileURLWithPath: NSTemporaryDirectory()))
            .appendingPathComponent("lumiverb", isDirectory: true)
            .appendingPathComponent("thumbnails", isDirectory: true)
        files = BoundedDiskCache(directory: directory, highWatermarkBytes: highWatermarkBytes,
                                 lowWatermarkBytes: lowWatermarkBytes)
    }

    // MARK: - ThumbnailCache

    public func get(assetId: String) -> Data? { files.get(assetId) }

    public func put(assetId: String, data: Data) { files.put(assetId, data: data) }

    public func has(assetId: String) -> Bool { files.has(assetId) }

    public func remove(assetId: String) { files.remove(assetId) }

    public func removeAll() { files.removeAll() }
}
#endif
