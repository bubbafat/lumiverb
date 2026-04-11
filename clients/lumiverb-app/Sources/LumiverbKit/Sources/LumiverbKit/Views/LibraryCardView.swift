import SwiftUI

/// A library/album card showing the name and a 2x2 thumbnail grid,
/// matching the Google Photos album card style. Falls back to a single
/// cover image or placeholder when fewer than 4 preview IDs are available.
public struct LibraryCardView: View {
    public let library: Library
    public let previewAssetIds: [String]
    public let client: APIClient?

    public init(
        library: Library,
        previewAssetIds: [String] = [],
        client: APIClient?
    ) {
        self.library = library
        // Use provided previews, or fall back to cover_asset_id
        if previewAssetIds.isEmpty, let coverId = library.coverAssetId {
            self.previewAssetIds = [coverId]
        } else {
            self.previewAssetIds = previewAssetIds
        }
        self.client = client
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            thumbnailGrid
            Text(library.name)
                .font(.subheadline.weight(.medium))
                .foregroundColor(.primary)
                .lineLimit(1)
        }
    }

    @ViewBuilder
    private var thumbnailGrid: some View {
        let ids = Array(previewAssetIds.prefix(4))
        if ids.isEmpty {
            emptyPlaceholder
        } else if ids.count == 1 {
            singleImage(ids[0])
        } else {
            multiImageGrid(ids)
        }
    }

    private func singleImage(_ assetId: String) -> some View {
        AuthenticatedImageView(
            assetId: assetId,
            client: client,
            type: .thumbnail
        )
        .aspectRatio(1, contentMode: .fill)
        .clipped()
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    @ViewBuilder
    private func multiImageGrid(_ ids: [String]) -> some View {
        let spacing: CGFloat = 2
        GeometryReader { geo in
            let half = (geo.size.width - spacing) / 2
            VStack(spacing: spacing) {
                HStack(spacing: spacing) {
                    thumbnailCell(ids[0], size: half)
                    if ids.count > 1 {
                        thumbnailCell(ids[1], size: half)
                    }
                }
                if ids.count > 2 {
                    HStack(spacing: spacing) {
                        thumbnailCell(ids[2], size: half)
                        if ids.count > 3 {
                            thumbnailCell(ids[3], size: half)
                        } else {
                            Color.clear.frame(width: half, height: half)
                        }
                    }
                }
            }
        }
        .aspectRatio(1, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private func thumbnailCell(_ assetId: String, size: CGFloat) -> some View {
        AuthenticatedImageView(
            assetId: assetId,
            client: client,
            type: .thumbnail
        )
        .frame(width: size, height: size)
        .clipped()
    }

    private var emptyPlaceholder: some View {
        RoundedRectangle(cornerRadius: 12)
            .fill(Color.gray.opacity(0.2))
            .aspectRatio(1, contentMode: .fit)
            .overlay {
                Image(systemName: "photo.on.rectangle.angled")
                    .font(.title2)
                    .foregroundStyle(.quaternary)
            }
    }
}
