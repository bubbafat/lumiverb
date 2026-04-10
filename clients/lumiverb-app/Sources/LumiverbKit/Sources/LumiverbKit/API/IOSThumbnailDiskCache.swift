#if os(iOS)
import Foundation

/// Disk-backed thumbnail cache for iOS, sized for the sandboxed app
/// container. Distinct from `MacThumbnailDiskCache` because:
///
/// - **Path:** `FileManager.default.urls(for: .cachesDirectory, ...)` —
///   iOS apps cannot write to `~/.cache/`, and `cachesDirectory` lets the
///   OS purge under storage pressure.
/// - **Bounded:** ~200 MB with approximate-LRU eviction (oldest by
///   `contentModificationDate`). Reads bump mtime via `setAttributes`
///   to approximate true LRU. Acceptable for tens of thousands of small
///   files; revisit only if churn analysis shows useful entries being
///   evicted prematurely.
/// - **No sidecars:** the macOS variant has no sidecars either; called out
///   here so a reader doesn't think there's a divergence.
/// - **No atomic-write ceremony:** iOS apps are single-process inside
///   their own container, so we use `Data.write(to:options:.atomic)`
///   directly without the temp+rename dance.
public final class IOSThumbnailDiskCache: @unchecked Sendable, ThumbnailCache {
    /// Hard cap on disk usage. Eviction triggers when `put` would exceed
    /// this; eviction continues until total size is below
    /// `lowWatermarkBytes` (the 20 MB hysteresis prevents thrashing on
    /// the boundary).
    public let highWatermarkBytes: Int
    public let lowWatermarkBytes: Int

    private let cacheDir: URL
    private let queue = DispatchQueue(label: "io.lumiverb.iosThumbnailCache", qos: .utility)

    public init(
        highWatermarkBytes: Int = 200 * 1024 * 1024,
        lowWatermarkBytes: Int = 180 * 1024 * 1024,
        cacheDir: URL? = nil
    ) {
        self.highWatermarkBytes = highWatermarkBytes
        self.lowWatermarkBytes = lowWatermarkBytes
        if let cacheDir {
            self.cacheDir = cacheDir
        } else {
            let base = FileManager.default
                .urls(for: .cachesDirectory, in: .userDomainMask)
                .first ?? URL(fileURLWithPath: NSTemporaryDirectory())
            self.cacheDir = base
                .appendingPathComponent("lumiverb", isDirectory: true)
                .appendingPathComponent("thumbnails", isDirectory: true)
        }
        try? FileManager.default.createDirectory(
            at: self.cacheDir,
            withIntermediateDirectories: true
        )
    }

    // MARK: - ThumbnailCache

    public func get(assetId: String) -> Data? {
        let url = thumbnailURL(assetId)
        guard let data = try? Data(contentsOf: url) else { return nil }
        // Touch mtime to approximate LRU. Best-effort: a failure here just
        // means this entry is more likely to be evicted than its real
        // access pattern would suggest. Acceptable.
        try? FileManager.default.setAttributes(
            [.modificationDate: Date()],
            ofItemAtPath: url.path
        )
        return data
    }

    public func put(assetId: String, data: Data) {
        let url = thumbnailURL(assetId)
        try? data.write(to: url, options: .atomic)
        // Eviction is best-effort and runs off-caller-thread so a `put`
        // burst during scrolling doesn't stall the UI on a directory
        // enumeration. The hysteresis (180/200) tolerates the brief
        // window where the cache may be slightly over the cap.
        queue.async { [weak self] in
            self?.evictIfNeeded()
        }
    }

    public func has(assetId: String) -> Bool {
        FileManager.default.fileExists(atPath: thumbnailURL(assetId).path)
    }

    public func remove(assetId: String) {
        try? FileManager.default.removeItem(at: thumbnailURL(assetId))
    }

    public func removeAll() {
        try? FileManager.default.removeItem(at: cacheDir)
        try? FileManager.default.createDirectory(
            at: cacheDir,
            withIntermediateDirectories: true
        )
    }

    // MARK: - Eviction

    /// Sum the cache directory's total file size; if over the high
    /// watermark, delete oldest-mtime files until under the low watermark.
    private func evictIfNeeded() {
        let fm = FileManager.default
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey]
        guard let entries = try? fm.contentsOfDirectory(
            at: cacheDir,
            includingPropertiesForKeys: keys,
            options: [.skipsHiddenFiles]
        ) else { return }

        // Build (url, size, mtime) tuples once. We reuse the size sum and
        // the mtime ordering, so a second enumeration is wasteful.
        struct Entry {
            let url: URL
            let size: Int
            let mtime: Date
        }
        var total = 0
        var infos: [Entry] = []
        infos.reserveCapacity(entries.count)
        for url in entries {
            guard let values = try? url.resourceValues(forKeys: Set(keys)),
                  let size = values.fileSize,
                  let mtime = values.contentModificationDate
            else { continue }
            total += size
            infos.append(Entry(url: url, size: size, mtime: mtime))
        }
        guard total > highWatermarkBytes else { return }

        // Oldest-first; pop until under the low watermark.
        infos.sort { $0.mtime < $1.mtime }
        for entry in infos {
            if total <= lowWatermarkBytes { break }
            try? fm.removeItem(at: entry.url)
            total -= entry.size
        }
    }

    // MARK: - Paths

    private func thumbnailURL(_ assetId: String) -> URL {
        cacheDir.appendingPathComponent(assetId)
    }
}
#endif
