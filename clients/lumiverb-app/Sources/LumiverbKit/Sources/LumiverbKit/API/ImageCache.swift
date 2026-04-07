import Foundation
#if canImport(AppKit)
import AppKit
#elseif canImport(UIKit)
import UIKit
#endif

/// Thread-safe in-memory image cache using NSCache.
public final class ImageCache: @unchecked Sendable {
    public static let shared = ImageCache()

    private let cache = NSCache<NSString, CacheEntry>()

    private init() {
        cache.countLimit = 2000  // Max 2000 images in memory
        cache.totalCostLimit = 200 * 1024 * 1024  // 200 MB
    }

    public func image(forKey key: String) -> PlatformImage? {
        cache.object(forKey: key as NSString)?.image
    }

    public func setImage(_ image: PlatformImage, forKey key: String, cost: Int = 0) {
        let entry = CacheEntry(image: image)
        cache.setObject(entry, forKey: key as NSString, cost: cost)
    }

    public func removeAll() {
        cache.removeAllObjects()
    }
}

/// Wrapper to store images in NSCache (requires NSObject subclass).
private final class CacheEntry: NSObject {
    let image: PlatformImage
    init(image: PlatformImage) {
        self.image = image
    }
}

// MARK: - Cross-platform image type

#if canImport(AppKit)
public typealias PlatformImage = NSImage

extension NSImage {
    /// Create an NSImage from raw data.
    public static func from(data: Data) -> NSImage? {
        NSImage(data: data)
    }
}
#elseif canImport(UIKit)
public typealias PlatformImage = UIImage

extension UIImage {
    /// Create a UIImage from raw data.
    public static func from(data: Data) -> UIImage? {
        UIImage(data: data)
    }
}
#endif
