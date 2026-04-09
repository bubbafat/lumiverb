import SwiftUI
import LumiverbKit

/// Loads and displays an authenticated image (thumbnail or proxy) from the API.
///
/// Resolution order:
/// 1. In-memory cache (ImageCache) — instant, on main actor
/// 2. Disk cache — thumbnails: `~/.cache/lumiverb/thumbnails/`,
///    proxies: `~/.cache/lumiverb/proxies/` (shared with Python CLI)
/// 3. Server download — authenticated fetch, written to both cache layers
///
/// Steps 2–3 run on a **detached Task** so disk I/O and `NSImage(data:)`
/// decoding stay off the main actor. Only the final assignment of
/// `self.image` (and bookkeeping flags) hops back to main. Without this,
/// 20 thumbnails landing in parallel after a library switch trigger 20
/// back-to-back `NSImage(data:)` decodes on main, each one a few tens of
/// milliseconds. Cumulative hundreds of ms of main-thread churn is what
/// made the sidebar feel locked during a library switch.
struct AuthenticatedImageView: View {
    let assetId: String
    let client: APIClient?
    let type: ImageType

    enum ImageType: Sendable {
        case thumbnail
        case proxy

        var path: String {
            switch self {
            case .thumbnail: return "thumbnail"
            case .proxy: return "proxy"
            }
        }
    }

    @State private var image: NSImage?
    @State private var isLoading = false
    @State private var failed = false

    private var cacheKey: String { "\(type.path)-\(assetId)" }

    var body: some View {
        Group {
            if let image {
                // Thumbnails: `.fill` because grid cells now pre-size
                // their frames to the asset's natural aspect ratio
                // (justified-row layout — see `MediaLayout`), so
                // `.fill` and `.fit` produce identical pixel output
                // when the frame matches and `.fill` does the right
                // thing if the frame happens to be slightly off.
                // Proxies: `.fit` so the lightbox letterboxes instead
                // of cropping — proxies live in a maxed-out frame.
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: type == .thumbnail ? .fill : .fit)
            } else if isLoading {
                ProgressView()
                    .controlSize(.small)
            } else if failed {
                Image(systemName: "photo")
                    .foregroundColor(.secondary)
                    .font(.title2)
            } else {
                Color.clear
            }
        }
        .task(id: assetId) {
            await loadImage()
        }
    }

    private func loadImage() async {
        // 1. In-memory cache — fast path, runs on main actor. NSCache
        // lookups are thread-safe and cheap, no reason to dispatch.
        if let cached = ImageCache.shared.image(forKey: cacheKey) {
            self.image = cached
            return
        }

        guard let client else {
            failed = true
            return
        }

        isLoading = true
        failed = false

        // Capture values needed by the detached task. These are the only
        // things the off-main work needs — Sendable by construction.
        let assetId = self.assetId
        let type = self.type
        let cacheKey = self.cacheKey

        // 2+3. Disk-check, network fetch, and NSImage decode all off
        // main. `Task.detached` runs on the global concurrent pool, not
        // the view's main actor.
        let result: LoadedImage? = await Task.detached(priority: .userInitiated) {
            // Disk cache (thumbnails → ThumbnailCacheOnDisk, proxies →
            // ProxyCacheOnDisk). Same pattern, different store. Both
            // reads are sync Data(contentsOf:) — acceptable off main.
            if let diskData = Self.readDiskCache(assetId: assetId, type: type),
               let nsImage = NSImage(data: diskData) {
                return LoadedImage(image: nsImage, data: diskData, fromDisk: true)
            }

            // Network fetch via the APIClient actor. `client` is Sendable
            // (it's an actor), so passing it into the detached closure is
            // safe. The await hops to APIClient's executor, URLSession
            // does its thing, control returns here on the detached task's
            // executor — still off main.
            do {
                guard let data = try await client.getData("/v1/assets/\(assetId)/\(type.path)") else {
                    return nil
                }
                guard let nsImage = NSImage(data: data) else {
                    return nil
                }
                return LoadedImage(image: nsImage, data: data, fromDisk: false)
            } catch {
                return nil
            }
        }.value

        // Hop back to main to update view state and populate caches.
        guard let result else {
            failed = true
            isLoading = false
            return
        }

        ImageCache.shared.setImage(result.image, forKey: cacheKey, cost: result.data.count)

        // Only write to the disk cache if the bytes came from the network.
        // A disk-hit round-trips the same bytes we'd be writing back.
        if !result.fromDisk {
            Self.writeDiskCache(assetId: assetId, type: type, data: result.data)
        }

        self.image = result.image
        isLoading = false
    }

    // MARK: - Off-main cache helpers

    /// Result shuttled from the detached load task back to the main
    /// actor. Sendable because `NSImage` conforms on macOS and `Data`
    /// is trivially value-semantic.
    private struct LoadedImage: @unchecked Sendable {
        let image: NSImage
        let data: Data
        let fromDisk: Bool
    }

    /// Read from whichever disk cache corresponds to `type`. Safe to call
    /// from any actor — both cache stores are `@unchecked Sendable` and
    /// do their own file locking via atomic writes.
    private static func readDiskCache(assetId: String, type: ImageType) -> Data? {
        switch type {
        case .thumbnail:
            return ThumbnailCacheOnDisk.shared.get(assetId: assetId)
        case .proxy:
            return ProxyCacheOnDisk.shared.get(assetId: assetId)
        }
    }

    private static func writeDiskCache(assetId: String, type: ImageType, data: Data) {
        switch type {
        case .thumbnail:
            ThumbnailCacheOnDisk.shared.put(assetId: assetId, data: data)
        case .proxy:
            ProxyCacheOnDisk.shared.put(assetId: assetId, data: data)
        }
    }
}
