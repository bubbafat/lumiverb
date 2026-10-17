import XCTest
@testable import LumiverbKit

/// The apps' disk caches are bounded (ADR-020 decision 7): past the cap the
/// least recently used go, down to the low watermark. Every test works in a
/// folder of its own: none touches the app's real caches.
final class BoundedDiskCacheTests: XCTestCase {

    private var dir: URL!

    override func setUp() {
        super.setUp()
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("bounded-cache-\(UUID().uuidString)")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: dir)
        super.tearDown()
    }

    private func used(_ cache: BoundedDiskCache, _ id: String, secondsAgo: TimeInterval) throws {
        try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSinceNow: -secondsAgo)],
                                              ofItemAtPath: cache.directory.appendingPathComponent(id).path)
    }

    func testItKeepsWhatItsGiven() {
        let cache = BoundedDiskCache(directory: dir, highWatermarkBytes: 1000)
        cache.put("ast_1", data: Data("one".utf8))
        XCTAssertEqual(cache.get("ast_1"), Data("one".utf8))
        XCTAssertTrue(cache.has("ast_1"))
        cache.put("ast_1", data: Data("again".utf8))
        XCTAssertEqual(cache.get("ast_1"), Data("again".utf8))
        cache.remove("ast_1")
        XCTAssertNil(cache.get("ast_1"))
        XCTAssertFalse(cache.has("ast_1"))
    }

    func testPastItsCapTheLeastRecentlyUsedGoDownToTheLowWatermark() throws {
        let cache = BoundedDiskCache(directory: dir, highWatermarkBytes: 100, lowWatermarkBytes: 60)
        for (n, id) in ["ast_a", "ast_b", "ast_c"].enumerated() {
            cache.put(id, data: Data(repeating: 1, count: 30))
            try used(cache, id, secondsAgo: Double(300 - n * 100))  // a the oldest
        }
        cache.settle()
        XCTAssertTrue(cache.has("ast_a"), "90 bytes: under the cap, nothing goes")
        cache.put("ast_d", data: Data(repeating: 1, count: 30))  // 120: past it
        cache.settle()
        XCTAssertFalse(cache.has("ast_a"))
        XCTAssertFalse(cache.has("ast_b"), "down to 60, not just under 100")
        XCTAssertTrue(cache.has("ast_c"))
        XCTAssertTrue(cache.has("ast_d"))
    }

    func testAReadKeepsAnEntry() throws {
        let cache = BoundedDiskCache(directory: dir, highWatermarkBytes: 100, lowWatermarkBytes: 60)
        for (n, id) in ["ast_a", "ast_b", "ast_c"].enumerated() {
            cache.put(id, data: Data(repeating: 1, count: 30))
            try used(cache, id, secondsAgo: Double(300 - n * 100))
        }
        cache.settle()
        XCTAssertNotNil(cache.get("ast_a"))  // used now: the last to go
        cache.put("ast_d", data: Data(repeating: 1, count: 30))
        cache.settle()
        XCTAssertTrue(cache.has("ast_a"))
        XCTAssertFalse(cache.has("ast_b"))
        XCTAssertFalse(cache.has("ast_c"))
    }

    func testANameThatIsntAnAssetIdIsNeitherWrittenNorRead() {
        let cache = BoundedDiskCache(directory: dir.appendingPathComponent("in"), highWatermarkBytes: 1000)
        cache.put("../out", data: Data("x".utf8))
        XCTAssertFalse(FileManager.default.fileExists(atPath: dir.appendingPathComponent("out").path))
        XCTAssertNil(cache.get("../out"))
        XCTAssertFalse(cache.has("../out"))
    }

    func testRemoveTakesTheFilesBesideAnEntryWhenAsked() throws {
        let cache = BoundedDiskCache(directory: dir, highWatermarkBytes: 1000)
        cache.put("ast_1", data: Data("p".utf8))
        try Data("sha".utf8).write(to: dir.appendingPathComponent("ast_1.sha"))
        cache.remove("ast_1", alsoSuffixes: [".sha"])
        XCTAssertFalse(FileManager.default.fileExists(atPath: dir.appendingPathComponent("ast_1.sha").path))
    }

    func testRemoveAllLeavesItReadyForTheNextPut() {
        let cache = BoundedDiskCache(directory: dir, highWatermarkBytes: 1000)
        cache.put("ast_1", data: Data("p".utf8))
        cache.removeAll()
        XCTAssertFalse(cache.has("ast_1"))
        cache.put("ast_2", data: Data("q".utf8))
        XCTAssertTrue(cache.has("ast_2"))
    }

    func testTheMacCachesHaveCaps() {
        #if os(macOS)
        XCTAssertEqual(MacProxyDiskCache.defaultCapBytes, 1024 * 1024 * 1024)
        XCTAssertEqual(MacThumbnailDiskCache.defaultCapBytes, 500 * 1024 * 1024)
        let small = MacThumbnailDiskCache(cacheDir: dir, capBytes: 50)
        small.put(assetId: "ast_1", data: Data(repeating: 1, count: 30))
        small.put(assetId: "ast_2", data: Data(repeating: 1, count: 30))
        small.settle()
        XCTAssertEqual([small.has(assetId: "ast_1"), small.has(assetId: "ast_2")].filter { $0 }.count, 1)
        #endif
    }
}

#if os(macOS)
/// The Mac app's caches have a folder of their own: what the old shared
/// one held (`~/.cache/lumiverb/...`) is moved in once, never copied.
final class MacCacheFolderTests: XCTestCase {

    private var dir: URL!

    override func setUp() {
        super.setUp()
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("mac-cache-\(UUID().uuidString)")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: dir)
        super.tearDown()
    }

    func testTheAppsFolderIsItsOwnInLibraryCaches() {
        let proxies = BoundedDiskCache.macDirectory("proxies")
        XCTAssertEqual(Array(proxies.pathComponents.suffix(4)), ["Library", "Caches", "io.lumiverb.app", "proxies"])
    }

    func testTheOldFoldersEntriesAreMovedInWithoutTheCLIsSidecars() throws {
        let old = dir.appendingPathComponent("old")
        try FileManager.default.createDirectory(at: old, withIntermediateDirectories: true)
        try Data("proxy".utf8).write(to: old.appendingPathComponent("ast_1"))
        try Data("sha256:ab".utf8).write(to: old.appendingPathComponent("ast_1.sha"))
        let cache = BoundedDiskCache(directory: dir.appendingPathComponent("new"), highWatermarkBytes: 1000)

        BoundedDiskCache.adopt(old: old, into: cache, dropping: [".sha"])
        cache.settle()

        XCTAssertEqual(cache.get("ast_1"), Data("proxy".utf8))
        XCTAssertFalse(FileManager.default.fileExists(atPath: cache.directory.appendingPathComponent("ast_1.sha").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: old.path), "moved, not copied")
    }

    func testAnOldFolderTheNewOneHasReplacedGoes() throws {
        let old = dir.appendingPathComponent("old")
        try FileManager.default.createDirectory(at: old, withIntermediateDirectories: true)
        try Data("stale".utf8).write(to: old.appendingPathComponent("ast_1"))
        let cache = BoundedDiskCache(directory: dir.appendingPathComponent("new"), highWatermarkBytes: 1000)
        cache.put("ast_2", data: Data("current".utf8))

        BoundedDiskCache.adopt(old: old, into: cache)
        cache.settle()

        XCTAssertFalse(FileManager.default.fileExists(atPath: old.path))
        XCTAssertEqual(cache.get("ast_2"), Data("current".utf8))
        XCTAssertNil(cache.get("ast_1"))
    }

    func testWhatsMovedInIsTrimmedToTheCap() throws {
        let old = dir.appendingPathComponent("old")
        try FileManager.default.createDirectory(at: old, withIntermediateDirectories: true)
        for n in 0..<10 {
            let file = old.appendingPathComponent("ast_\(n)")
            try Data(repeating: 1, count: 30).write(to: file)
            try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSinceNow: Double(n - 100))],
                                                  ofItemAtPath: file.path)
        }
        let cache = BoundedDiskCache(directory: dir.appendingPathComponent("new"), highWatermarkBytes: 100,
                                     lowWatermarkBytes: 90)

        BoundedDiskCache.adopt(old: old, into: cache)
        cache.settle()

        let kept = (0..<10).filter { cache.has("ast_\($0)") }
        XCTAssertEqual(kept, [7, 8, 9], "the most recently used, within the cap")
    }

    func testNoOldFolderIsNothingToDo() {
        let cache = BoundedDiskCache(directory: dir.appendingPathComponent("new"), highWatermarkBytes: 1000)
        BoundedDiskCache.adopt(old: dir.appendingPathComponent("nowhere"), into: cache)
        cache.settle()
        XCTAssertTrue(FileManager.default.fileExists(atPath: cache.directory.path))
    }
}
#endif
