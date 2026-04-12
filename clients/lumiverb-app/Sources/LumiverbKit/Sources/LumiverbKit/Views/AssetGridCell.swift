import SwiftUI

/// Single source of truth for the iOS asset grid cell. Used by Photos
/// (`MediaGridView`), Collections (`CollectionDetailView`), People
/// (`PersonDetailView`), and Favorites (`FavoritesView`) so all four
/// grids share the same look, selection behavior, and aspect-handling
/// — change here once and it propagates everywhere.
///
/// Renders a square cell with:
/// - Authenticated thumbnail image, frame-first + overlay so the
///   image's internal `.aspectRatio(.fill)` doesn't bleed over the
///   selection indicators.
/// - Optional video play icon overlay.
/// - Tappable selection circle in the top-leading corner. The
///   `onToggleSelect` closure is the only selection mutator the cell
///   knows about — call sites wire it to `BrowseState.toggleSelection`.
/// - Accent border when selected.
public struct AssetGridCell: View {
    public let assetId: String
    public let isVideo: Bool
    public let isSelected: Bool
    public let client: APIClient?
    public let onToggleSelect: () -> Void

    public init(
        assetId: String,
        isVideo: Bool = false,
        isSelected: Bool,
        client: APIClient?,
        onToggleSelect: @escaping () -> Void
    ) {
        self.assetId = assetId
        self.isVideo = isVideo
        self.isSelected = isSelected
        self.client = client
        self.onToggleSelect = onToggleSelect
    }

    public var body: some View {
        Color.clear
            .aspectRatio(1, contentMode: .fit)
            .overlay(
                AuthenticatedImageView(
                    assetId: assetId,
                    client: client,
                    type: .thumbnail
                )
            )
            .background(Color.gray.opacity(0.1))
            .clipped()
            .cornerRadius(2)
            .overlay {
                if isVideo {
                    Image(systemName: "play.fill")
                        .font(.title2)
                        .foregroundColor(.white.opacity(0.9))
                        .shadow(color: .black.opacity(0.5), radius: 4)
                }
            }
            .overlay(alignment: .topLeading) {
                Button(action: onToggleSelect) {
                    Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                        .font(.title3)
                        .foregroundColor(isSelected ? .accentColor : .white.opacity(0.85))
                        .shadow(color: .black.opacity(0.5), radius: 2)
                        .padding(8)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
            .overlay {
                if isSelected {
                    RoundedRectangle(cornerRadius: 2)
                        .strokeBorder(Color.accentColor, lineWidth: 3)
                }
            }
            .contentShape(Rectangle())
    }
}
