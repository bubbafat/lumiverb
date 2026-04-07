import SwiftUI
import LumiverbKit

/// Loads and displays an authenticated image (thumbnail or proxy) from the API.
///
/// Resolution order:
/// 1. In-memory cache (ImageCache) — instant
/// 2. Disk cache (~/.cache/lumiverb/proxies/) — fast, shared with Python CLI
/// 3. Server download — authenticated fetch, cached to both layers
struct AuthenticatedImageView: View {
    let assetId: String
    let client: APIClient?
    let type: ImageType

    enum ImageType {
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
        // 1. In-memory cache
        if let cached = ImageCache.shared.image(forKey: cacheKey) {
            self.image = cached
            return
        }

        // 2. Disk cache (shared with Python CLI)
        // Only for proxy type — thumbnails are smaller and server-generated
        if type == .proxy {
            if let diskData = ProxyCacheOnDisk.shared.get(assetId: assetId),
               let nsImage = NSImage(data: diskData) {
                ImageCache.shared.setImage(nsImage, forKey: cacheKey, cost: diskData.count)
                self.image = nsImage
                return
            }
        }

        // 3. Server download
        guard let client else {
            failed = true
            return
        }

        isLoading = true
        failed = false

        do {
            if let data = try await client.getData("/v1/assets/\(assetId)/\(type.path)"),
               let nsImage = NSImage(data: data) {
                // Cache in memory
                ImageCache.shared.setImage(nsImage, forKey: cacheKey, cost: data.count)
                // Cache proxy to disk for reuse across app restarts and by Python CLI
                if type == .proxy {
                    ProxyCacheOnDisk.shared.put(assetId: assetId, data: data)
                }
                self.image = nsImage
            } else {
                failed = true
            }
        } catch {
            failed = true
        }

        isLoading = false
    }
}
