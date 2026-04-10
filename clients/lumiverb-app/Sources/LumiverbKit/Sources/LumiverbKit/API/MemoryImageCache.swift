import Foundation

/// In-memory image cache backed by `NSCache<NSString, NSData>`. Conforms to
/// **both** `ProxyCache` and `ThumbnailCache` so it can be used as the iOS
/// proxy cache, as the default environment value, and as a test/preview
/// stand-in.
///
/// `NSCache` evicts under memory pressure and respects `totalCostLimit` /
/// `countLimit`. There is no disk layer.
///
/// **Why one type for both protocols:** the iOS proxy cache and the
/// preview/default cache have identical shape (NSCache, byte budget, no
/// disk). Two distinct types would be duplication. The iOS runtime still
/// uses **two separate instances** of `MemoryImageCache` so the proxy
/// budget and the thumbnail budget never collide.
public final class MemoryImageCache: @unchecked Sendable, ProxyCache, ThumbnailCache {
    private let cache = NSCache<NSString, NSData>()
    private let name: String

    /// - Parameters:
    ///   - name: Identifier surfaced via `NSCache.name`. Purely diagnostic;
    ///     used to distinguish multiple instances in Instruments / log
    ///     dumps. Behavior is identical regardless of name.
    ///   - costLimit: Total bytes the cache may hold. Default 150 MB —
    ///     sized for an iOS lightbox session that touches 20-50 proxies
    ///     of ~200 KB each, with headroom for back/forward sweeps.
    ///   - countLimit: Maximum number of cached images. Default 400.
    public init(
        name: String,
        costLimit: Int = 150 * 1024 * 1024,
        countLimit: Int = 400
    ) {
        self.name = name
        cache.name = name
        cache.totalCostLimit = costLimit
        cache.countLimit = countLimit
    }

    public func get(assetId: String) -> Data? {
        cache.object(forKey: assetId as NSString) as Data?
    }

    public func put(assetId: String, data: Data) {
        cache.setObject(data as NSData, forKey: assetId as NSString, cost: data.count)
    }

    public func has(assetId: String) -> Bool {
        cache.object(forKey: assetId as NSString) != nil
    }

    public func remove(assetId: String) {
        cache.removeObject(forKey: assetId as NSString)
    }

    public func removeAll() {
        cache.removeAllObjects()
    }
}
