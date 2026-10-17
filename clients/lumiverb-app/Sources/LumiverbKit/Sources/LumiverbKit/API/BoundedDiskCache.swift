import Foundation

/// A folder of cached files named by asset id, with a cap: the disk caches
/// of both apps are built on it (ADR-020 decision 7: a client keeps only
/// bounded caches of its own).
///
/// - **Bounded:** when a `put` takes the folder past `highWatermarkBytes`,
///   the least recently used files go (oldest `contentModificationDate`; a
///   `get` touches it) until it's under `lowWatermarkBytes`. The gap between
///   the two keeps it from evicting on every write at the line. Eviction
///   runs off the caller's thread, so a burst of writes while scrolling
///   doesn't stall the UI on a folder listing; the folder may be over the cap
///   for that moment.
/// - **Atomic writes:** a temp file in the folder, renamed into place, so a
///   reader never sees half a file. A temp file a crash left is the oldest
///   thing there, and goes first.
/// - **Plain names only:** an asset id that isn't one (`cacheEntryURL`) is
///   neither read nor written.
public final class BoundedDiskCache: @unchecked Sendable {
    public let directory: URL
    public let highWatermarkBytes: Int
    public let lowWatermarkBytes: Int

    private let queue: DispatchQueue

    /// `lowWatermarkBytes` defaults to 90% of the high one.
    public init(directory: URL, highWatermarkBytes: Int, lowWatermarkBytes: Int? = nil) {
        self.directory = directory
        self.highWatermarkBytes = highWatermarkBytes
        self.lowWatermarkBytes = lowWatermarkBytes ?? highWatermarkBytes / 10 * 9
        self.queue = DispatchQueue(label: "io.lumiverb.cache.\(directory.lastPathComponent)", qos: .utility)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    public func get(_ assetId: String) -> Data? {
        guard let url = cacheEntryURL(directory, assetId),
              let data = try? Data(contentsOf: url) else { return nil }
        // Used now: the last to go. Best-effort; a failure only makes it
        // likelier to be evicted than its use would say.
        try? FileManager.default.setAttributes([.modificationDate: Date()], ofItemAtPath: url.path)
        return data
    }

    public func put(_ assetId: String, data: Data) {
        guard let url = cacheEntryURL(directory, assetId) else { return }
        let temp = directory.appendingPathComponent(UUID().uuidString + ".tmp")
        do {
            try data.write(to: temp)
            if FileManager.default.fileExists(atPath: url.path) {
                try FileManager.default.removeItem(at: url)
            }
            try FileManager.default.moveItem(at: temp, to: url)
        } catch {
            try? FileManager.default.removeItem(at: temp)
            return
        }
        queue.async { [weak self] in self?.evictIfNeeded() }
    }

    public func has(_ assetId: String) -> Bool {
        cacheEntryURL(directory, assetId).map { FileManager.default.fileExists(atPath: $0.path) } ?? false
    }

    /// The entry, and the files beside it named with these suffixes.
    public func remove(_ assetId: String, alsoSuffixes suffixes: [String] = []) {
        for suffix in [""] + suffixes {
            if let url = cacheEntryURL(directory, assetId, suffix: suffix) {
                try? FileManager.default.removeItem(at: url)
            }
        }
    }

    /// Everything in it; the folder stays, ready for the next `put`.
    public func removeAll() {
        try? FileManager.default.removeItem(at: directory)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    /// Eviction now, on its queue (once at start, for a folder that may
    /// already be over the cap).
    public func trim() {
        queue.async { [weak self] in self?.evictIfNeeded() }
    }

    /// Waits for the eviction queued so far (tests).
    func settle() {
        queue.sync {}
    }

    /// Sums the folder's files; past the high watermark, removes the least
    /// recently used until it's under the low one.
    func evictIfNeeded() {
        let fm = FileManager.default
        let keys: [URLResourceKey] = [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey]
        guard let entries = try? fm.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: keys, options: [.skipsHiddenFiles]
        ) else { return }
        var total = 0
        var files: [(url: URL, size: Int, used: Date)] = []
        files.reserveCapacity(entries.count)
        for url in entries {
            guard let values = try? url.resourceValues(forKeys: Set(keys)), values.isRegularFile == true,
                  let size = values.fileSize, let used = values.contentModificationDate else { continue }
            total += size
            files.append((url, size, used))
        }
        guard total > highWatermarkBytes else { return }
        files.sort { $0.used < $1.used }
        for file in files {
            if total <= lowWatermarkBytes { break }
            try? fm.removeItem(at: file.url)
            total -= file.size
        }
    }
}

#if os(macOS)
extension BoundedDiskCache {
    /// The app's own cache folder on a Mac: `~/Library/Caches/io.lumiverb.app/<name>`.
    static func macDirectory(_ name: String, bundleIdentifier: String = "io.lumiverb.app") -> URL {
        let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Caches")
        return caches.appendingPathComponent(bundleIdentifier, isDirectory: true)
            .appendingPathComponent(name, isDirectory: true)
    }

    /// What the app kept before its caches had a folder of their own (in
    /// `~/.cache/lumiverb/`, beside what the CLI's scan wrote), moved in
    /// once: renamed when the new folder is empty (the same disk: nothing is
    /// copied, and it's done at once), else dropped, since the new one has
    /// replaced it. The files beside each entry with these suffixes (the
    /// CLI's `.sha` sidecars, which the app never reads) go, and the folder
    /// is trimmed to its cap, on the cache's queue: a big folder never holds
    /// up the app's start.
    static func adopt(old: URL, into cache: BoundedDiskCache, dropping suffixes: [String] = []) {
        let fm = FileManager.default
        var isFolder: ObjCBool = false
        guard fm.fileExists(atPath: old.path, isDirectory: &isFolder), isFolder.boolValue else { return }
        let fresh = (try? fm.contentsOfDirectory(atPath: cache.directory.path))?.isEmpty ?? true
        var moved = false
        if fresh {
            try? fm.removeItem(at: cache.directory)
            moved = (try? fm.moveItem(at: old, to: cache.directory)) != nil
            if !moved {  // left where it was; the next start tries again
                try? fm.createDirectory(at: cache.directory, withIntermediateDirectories: true)
                return
            }
        }
        cache.queue.async {
            if moved {
                for name in (try? fm.contentsOfDirectory(atPath: cache.directory.path)) ?? []
                    where suffixes.contains(where: { name.hasSuffix($0) }) {
                    try? fm.removeItem(at: cache.directory.appendingPathComponent(name))
                }
            } else {
                try? fm.removeItem(at: old)
            }
            cache.evictIfNeeded()
        }
    }
}
#endif
