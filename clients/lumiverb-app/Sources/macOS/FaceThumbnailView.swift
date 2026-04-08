import SwiftUI
import LumiverbKit

/// Displays a face crop thumbnail from the server, with caching.
struct FaceThumbnailView: View {
    let faceId: String?
    let client: APIClient?

    @State private var image: NSImage?
    @State private var isLoading = false

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .scaledToFill()
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
               let nsImage = NSImage(data: data) {
                ImageCache.shared.setImage(nsImage, forKey: cacheKey, cost: data.count)
                self.image = nsImage
            }
        } catch {
            // Non-fatal — fallback icon is fine
        }
    }
}
