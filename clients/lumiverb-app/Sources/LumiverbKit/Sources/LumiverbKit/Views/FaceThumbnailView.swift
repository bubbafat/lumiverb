import SwiftUI

/// Displays a face crop thumbnail from the server, with caching.
public struct FaceThumbnailView: View {
    public let faceId: String?
    public let client: APIClient?

    @State private var image: PlatformImage?
    @State private var isLoading = false

    public init(faceId: String?, client: APIClient?) {
        self.faceId = faceId
        self.client = client
    }

    public var body: some View {
        Group {
            if let image {
                #if canImport(AppKit)
                Image(nsImage: image)
                    .resizable()
                    .scaledToFill()
                #elseif canImport(UIKit)
                Image(uiImage: image)
                    .resizable()
                    .scaledToFill()
                #endif
            } else if isLoading {
                ProgressView()
                    .controlSize(.mini)
            } else {
                Image(systemName: "person.crop.circle.fill")
                    .resizable()
                    .foregroundColor(.secondary.opacity(0.5))
            }
        }
        .task(id: faceId) {
            await loadCrop()
        }
    }

    private func loadCrop() async {
        guard let faceId, let client else { return }

        let cacheKey = "face-crop-\(faceId)"
        if let cached = ImageCache.shared.image(forKey: cacheKey) {
            self.image = cached
            return
        }

        isLoading = true
        defer { isLoading = false }

        do {
            if let data = try await client.getData("/v1/faces/\(faceId)/crop"),
               let decoded = PlatformImage.from(data: data) {
                ImageCache.shared.setImage(decoded, forKey: cacheKey, cost: data.count)
                self.image = decoded
            }
        } catch {
            // Non-fatal — fallback icon is fine
        }
    }
}
