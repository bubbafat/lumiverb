import XCTest
import Foundation
@testable import LumiverbKit

/// Paths built from server data (rel paths, asset ids) stay where they belong.
final class LibraryPathTests: XCTestCase {

    func testRelPathInsideTheLibrary() {
        let url = LibraryPath.fileURL(root: "/Volumes/Photos", relPath: "2024/trip/./a.mov")
        XCTAssertEqual(url?.path, "/Volumes/Photos/2024/trip/a.mov")
    }

    func testRelPathOutOfTheLibraryIsRefused() {
        XCTAssertNil(LibraryPath.fileURL(root: "/Volumes/Photos", relPath: "../../etc/passwd"))
        XCTAssertNil(LibraryPath.fileURL(root: "/Volumes/Photos", relPath: "a/../../Photos2/x.mov"))
        XCTAssertNil(LibraryPath.fileURL(root: "/Volumes/Photos", relPath: ".."))
    }

    func testCacheEntryTakesPlainIds() {
        let dir = URL(fileURLWithPath: "/tmp/cache", isDirectory: true)
        XCTAssertEqual(cacheEntryURL(dir, "ast_01H-x9")?.lastPathComponent, "ast_01H-x9")
        XCTAssertEqual(cacheEntryURL(dir, "ast_1", suffix: ".sha")?.lastPathComponent, "ast_1.sha")
    }

    func testCacheEntryRefusesIdsThatAreNotPlainNames() {
        let dir = URL(fileURLWithPath: "/tmp/cache", isDirectory: true)
        for id in ["", ".", "..", "../evil", "a/b", "a.b", "a b", "/etc/passwd"] {
            XCTAssertNil(cacheEntryURL(dir, id), id)
        }
    }

    func testDiskCacheIgnoresABadId() throws {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("lumiverb-test-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let cache = MacProxyDiskCache(cacheDir: dir.appendingPathComponent("proxies"))

        cache.put(assetId: "../escaped", data: Data("x".utf8))

        XCTAssertFalse(FileManager.default.fileExists(atPath: dir.appendingPathComponent("escaped").path))
        XCTAssertNil(cache.get(assetId: "../escaped"))
        XCTAssertFalse(cache.has(assetId: "../escaped"))
    }
}
