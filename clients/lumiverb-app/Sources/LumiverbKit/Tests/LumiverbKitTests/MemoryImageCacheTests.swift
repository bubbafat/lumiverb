import XCTest
@testable import LumiverbKit

/// Tests for `MemoryImageCache`. Cross-platform code path — runs on the
/// macOS test bundle today, would also pass on iOS if the test target
/// gains an iOS variant. Exercises the type both as a `ProxyCache` and
/// as a `ThumbnailCache` because it conforms to both protocols.
final class MemoryImageCacheTests: XCTestCase {

    // MARK: - ProxyCache surface

    func testPutAndGetReturnsSameBytes() {
        let cache = MemoryImageCache(name: "test.proxies")
        let data = Data("hello-proxy".utf8)
        cache.put(assetId: "ast_1", data: data)
        XCTAssertEqual(cache.get(assetId: "ast_1"), data)
    }

    func testGetMissingReturnsNil() {
        let cache = MemoryImageCache(name: "test.proxies")
        XCTAssertNil(cache.get(assetId: "nope"))
    }

    func testHasReflectsResidency() {
        let cache = MemoryImageCache(name: "test.proxies")
        XCTAssertFalse(cache.has(assetId: "ast_2"))
        cache.put(assetId: "ast_2", data: Data("x".utf8))
        XCTAssertTrue(cache.has(assetId: "ast_2"))
    }

    func testRemoveDropsEntry() {
        let cache = MemoryImageCache(name: "test.proxies")
        cache.put(assetId: "ast_3", data: Data("x".utf8))
        XCTAssertTrue(cache.has(assetId: "ast_3"))
        cache.remove(assetId: "ast_3")
        XCTAssertFalse(cache.has(assetId: "ast_3"))
        XCTAssertNil(cache.get(assetId: "ast_3"))
    }

    func testPutOverwritesExisting() {
        let cache = MemoryImageCache(name: "test.proxies")
        cache.put(assetId: "ast_4", data: Data("v1".utf8))
        cache.put(assetId: "ast_4", data: Data("v2".utf8))
        XCTAssertEqual(cache.get(assetId: "ast_4"), Data("v2".utf8))
    }

    // MARK: - ThumbnailCache surface

    func testRemoveAllClearsEverything() {
        let cache = MemoryImageCache(name: "test.thumbs")
        cache.put(assetId: "a", data: Data("a".utf8))
        cache.put(assetId: "b", data: Data("b".utf8))
        cache.put(assetId: "c", data: Data("c".utf8))
        XCTAssertTrue(cache.has(assetId: "a"))
        XCTAssertTrue(cache.has(assetId: "b"))
        XCTAssertTrue(cache.has(assetId: "c"))

        cache.removeAll()

        XCTAssertFalse(cache.has(assetId: "a"))
        XCTAssertFalse(cache.has(assetId: "b"))
        XCTAssertFalse(cache.has(assetId: "c"))

        // Cache must still be usable after a flush — no re-init needed.
        cache.put(assetId: "d", data: Data("d".utf8))
        XCTAssertTrue(cache.has(assetId: "d"))
    }

    // MARK: - Protocol existential round-trip

    /// The point of `MemoryImageCache` conforming to both protocols is
    /// that the same instance can be passed as either an `any ProxyCache`
    /// or `any ThumbnailCache` — that's how the iOS app uses it as the
    /// proxy slot and how previews/tests use two separate instances as
    /// the default `CacheBundle`. Verify the protocol existentials work.
    func testConformsToProxyCacheProtocol() {
        let proxy: any ProxyCache = MemoryImageCache(name: "test.proxy")
        proxy.put(assetId: "p1", data: Data("via-proxy-protocol".utf8))
        XCTAssertEqual(proxy.get(assetId: "p1"), Data("via-proxy-protocol".utf8))
        XCTAssertTrue(proxy.has(assetId: "p1"))
        proxy.remove(assetId: "p1")
        XCTAssertFalse(proxy.has(assetId: "p1"))
    }

    func testConformsToThumbnailCacheProtocol() {
        let thumbs: any ThumbnailCache = MemoryImageCache(name: "test.thumb")
        thumbs.put(assetId: "t1", data: Data("via-thumbnail-protocol".utf8))
        XCTAssertEqual(thumbs.get(assetId: "t1"), Data("via-thumbnail-protocol".utf8))
        XCTAssertTrue(thumbs.has(assetId: "t1"))
        thumbs.removeAll()
        XCTAssertFalse(thumbs.has(assetId: "t1"))
    }

    // MARK: - CacheBundle wiring

    /// Two instances → independent storage. This is the invariant that
    /// the iOS runtime relies on (the proxy budget never colliding with
    /// the thumbnail budget) and that the preview/test default uses to
    /// avoid silently sharing one NSCache for both slots.
    func testTwoInstancesHaveIndependentStorage() {
        let proxies: any ProxyCache = MemoryImageCache(name: "ind.proxies")
        let thumbnails: any ThumbnailCache = MemoryImageCache(name: "ind.thumbs")

        proxies.put(assetId: "shared", data: Data("proxy-bytes".utf8))
        thumbnails.put(assetId: "shared", data: Data("thumb-bytes".utf8))

        XCTAssertEqual(proxies.get(assetId: "shared"), Data("proxy-bytes".utf8))
        XCTAssertEqual(thumbnails.get(assetId: "shared"), Data("thumb-bytes".utf8))

        // Removing from one slot must not affect the other.
        proxies.remove(assetId: "shared")
        XCTAssertFalse(proxies.has(assetId: "shared"))
        XCTAssertTrue(thumbnails.has(assetId: "shared"))
    }

    func testCacheBundleCarriesBothCaches() {
        let bundle = CacheBundle(
            proxies: MemoryImageCache(name: "bundle.p"),
            thumbnails: MemoryImageCache(name: "bundle.t")
        )
        bundle.proxies.put(assetId: "b1", data: Data("p".utf8))
        bundle.thumbnails.put(assetId: "b1", data: Data("t".utf8))
        XCTAssertEqual(bundle.proxies.get(assetId: "b1"), Data("p".utf8))
        XCTAssertEqual(bundle.thumbnails.get(assetId: "b1"), Data("t".utf8))
    }

    // MARK: - Cost limit behavior

    /// `NSCache` evicts based on cost. With a tiny 1 KB budget, putting
    /// many entries should leave at least one of them evicted. We don't
    /// assert exact eviction order (NSCache is opaque), only that the
    /// cache is bounded — i.e. it doesn't grow without limit.
    func testCostLimitBoundsTotalSize() {
        let cache = MemoryImageCache(
            name: "test.budget",
            costLimit: 1024,         // 1 KB total budget
            countLimit: 100
        )
        // Put 20 entries of 200 bytes each = 4 KB worth, well over 1 KB.
        let blob = Data(repeating: 0xAB, count: 200)
        for i in 0..<20 {
            cache.put(assetId: "ast_\(i)", data: blob)
        }
        // Some entries must have been evicted to fit the budget.
        var resident = 0
        for i in 0..<20 {
            if cache.has(assetId: "ast_\(i)") { resident += 1 }
        }
        XCTAssertLessThan(resident, 20, "NSCache should evict to honor costLimit")
    }
}
