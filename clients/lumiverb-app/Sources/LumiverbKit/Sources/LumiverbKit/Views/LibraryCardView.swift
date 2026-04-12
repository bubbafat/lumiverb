import SwiftUI

/// A library/album card showing the name and a 2x2 thumbnail grid,
/// matching the Google Photos album card style. The grid always
/// reserves a 2x2 layout — empty cells are filled with a placeholder
/// background so single-image libraries don't make the card collapse
/// or distort the parent's flex column.
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
        } else {
            // Always render a 2x2 grid. Missing cells use the placeholder
            // background so the card maintains a square aspect ratio
            // regardless of how many previews are available.
            let spacing: CGFloat = 2
            let columns = Array(
                repeating: GridItem(.flexible(), spacing: spacing),
                count: 2
            )
            LazyVGrid(columns: columns, spacing: spacing) {
                ForEach(0..<4, id: \.self) { i in
                    if i < ids.count {
                        // Color.clear sizes the cell to a square via
                        // aspectRatio. The overlay puts the image inside
                        // that frame, and .clipped() crops the image's
                        // .fill content to the cell's bounds. Without
                        // this pattern, AuthenticatedImageView's internal
                        // .aspectRatio(.fill) lets the image overflow
                        // and bleed over neighbouring cells.
                        Color.clear
                            .aspectRatio(1, contentMode: .fit)
                            .overlay(
                                AuthenticatedImageView(
                                    assetId: ids[i],
                                    client: client,
                                    type: .thumbnail
                                )
                            )
                            .clipped()
                    } else {
                        emptyCell
                    }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: 12))
        }
    }

    private var emptyCell: some View {
        Color.gray.opacity(0.15)
            .aspectRatio(1, contentMode: .fit)
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
