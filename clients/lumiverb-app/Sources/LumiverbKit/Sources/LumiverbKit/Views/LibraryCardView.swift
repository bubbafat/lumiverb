import SwiftUI

/// A library card showing the library name and its cover image.
/// Used in the iOS library picker and anywhere a library needs a
/// visual preview.
public struct LibraryCardView: View {
    public let library: Library
    public let isSelected: Bool
    public let client: APIClient?

    public init(
        library: Library,
        isSelected: Bool = false,
        client: APIClient?
    ) {
        self.library = library
        self.isSelected = isSelected
        self.client = client
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            coverImage
            Text(library.name)
                .font(.subheadline.weight(.medium))
                .lineLimit(1)
        }
        .padding(10)
        .background(isSelected ? Color.accentColor.opacity(0.12) : cardBackground)
        .cornerRadius(12)
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(isSelected ? Color.accentColor : Color.clear, lineWidth: 2)
        )
    }

    private var cardBackground: Color {
        #if os(iOS)
        Color(.secondarySystemGroupedBackground)
        #else
        Color(.controlBackgroundColor)
        #endif
    }

    private var emptyBackground: Color {
        #if os(iOS)
        Color(.tertiarySystemGroupedBackground)
        #else
        Color(.windowBackgroundColor)
        #endif
    }

    @ViewBuilder
    private var coverImage: some View {
        if let coverId = library.coverAssetId {
            AuthenticatedImageView(
                assetId: coverId,
                client: client,
                type: .thumbnail
            )
            .aspectRatio(16 / 9, contentMode: .fill)
            .frame(maxWidth: .infinity)
            .frame(height: 100)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: 8))
        } else {
            RoundedRectangle(cornerRadius: 8)
                .fill(emptyBackground)
                .frame(height: 100)
                .overlay {
                    Image(systemName: "photo.on.rectangle.angled")
                        .font(.title2)
                        .foregroundStyle(.quaternary)
                }
        }
    }
}
