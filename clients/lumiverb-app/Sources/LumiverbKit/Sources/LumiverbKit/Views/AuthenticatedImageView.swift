import SwiftUI

/// Loads and displays an authenticated image (thumbnail or proxy) from the API.
///
/// Resolution order:
/// 1. In-memory cache (`ImageCache.shared`) — instant, on main actor
/// 2. Disk/memory cache from the injected `CacheBundle` (per platform)
/// 3. Server download — authenticated fetch, written back to both cache layers
///
/// Steps 2–3 run on a **detached Task** so disk I/O and `PlatformImage(data:)`
/// decoding stay off the main actor. Only the final assignment of
/// `self.image` (and bookkeeping flags) hops back to main. Without this,
/// 20 thumbnails landing in parallel after a library switch trigger 20
/// back-to-back image decodes on main, each one a few tens of milliseconds.
/// Cumulative hundreds of ms of main-thread churn is what made the sidebar
/// feel locked during a library switch.
///
/// **Cache injection.** The view reads `@Environment(\.cacheBundle)` to get
/// the right pair of caches for the current platform. macOS installs
/// `MacProxyDiskCache.shared` + `MacThumbnailDiskCache.shared`; iOS
/// installs `MemoryImageCache(name: "ios.proxies")` +
/// `IOSThumbnailDiskCache()`. Tests/previews get a default pair of
/// in-memory `MemoryImageCache`s — see `CacheEnvironment.swift`.
public struct AuthenticatedImageView: View {
    public let assetId: String
    public let client: APIClient?
    public let type: ImageType

    public enum ImageType: Sendable {
        case thumbnail
        case proxy

        var path: String {
            switch self {
            case .thumbnail: return "thumbnail"
            case .proxy: return "proxy"
            }
        }
    }

    @State private var image: PlatformImage?
    @State private var isLoading = false
    @State private var failed = false

    @Environment(\.cacheBundle) private var cacheBundle

    private var cacheKey: String { "\(type.path)-\(assetId)" }

    public init(assetId: String, client: APIClient?, type: ImageType) {
        self.assetId = assetId
        self.client = client
        self.type = type
    }

    public var body: some View {
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
                #if canImport(AppKit)
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                #elseif canImport(UIKit)
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                #endif
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
        // Capture the protocol-existential cache from the environment so
        // the detached task can read/write it without re-touching `self`
        // (and thus without forcing main-actor isolation on the work).
        // Both `any ProxyCache` and `any ThumbnailCache` are `Sendable`
        // (the protocols require it), so the closure capture is safe.
        let proxies: any ProxyCache = cacheBundle.proxies
        let thumbnails: any ThumbnailCache = cacheBundle.thumbnails

        // 2+3. Disk-check, network fetch, and image decode all off main.
        // `Task.detached` runs on the global concurrent pool, not the
        // view's main actor.
        let result: LoadedImage? = await Task.detached(priority: .userInitiated) {
            // Disk/memory cache via the injected protocols. Both reads
            // are sync — acceptable off main.
            let diskData: Data?
            switch type {
            case .thumbnail:
                diskData = thumbnails.get(assetId: assetId)
            case .proxy:
                diskData = proxies.get(assetId: assetId)
            }
            if let diskData, let decoded = PlatformImage.from(data: diskData) {
                return LoadedImage(image: decoded, data: diskData, fromDisk: true)
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
                guard let decoded = PlatformImage.from(data: data) else {
                    return nil
                }
                return LoadedImage(image: decoded, data: data, fromDisk: false)
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
            switch type {
            case .thumbnail:
                cacheBundle.thumbnails.put(assetId: assetId, data: result.data)
            case .proxy:
                cacheBundle.proxies.put(assetId: assetId, data: result.data)
            }
        }

        self.image = result.image
        isLoading = false
    }

    /// Result shuttled from the detached load task back to the main
    /// actor. Sendable because both PlatformImage variants conform on
    /// their respective platforms and `Data` is trivially value-semantic.
    private struct LoadedImage: @unchecked Sendable {
        let image: PlatformImage
        let data: Data
        let fromDisk: Bool
    }
}
