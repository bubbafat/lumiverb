import SwiftUI

/// Context-menu items for rating a single asset. Used in `MediaGridView`,
/// `SearchResultsGrid`, and `SimilarResultsGrid` cell context menus.
/// macOS shows these on right-click; iOS on long-press (SwiftUI handles both).
///
/// Fetches the current rating on appear so the menu reflects the latest state.
/// Mutations fire-and-forget (context menus dismiss immediately).
public struct AssetRatingContextMenu: View {
    public let assetId: String
    public let client: APIClient?

    @State private var rating: Rating = .empty
    @State private var loaded = false

    public init(assetId: String, client: APIClient?) {
        self.assetId = assetId
        self.client = client
    }

    public var body: some View {
        // Favorite toggle
        Button {
            let newFav = !rating.favorite
            sendUpdate(RatingUpdateBody(favorite: newFav))
        } label: {
            Label(
                rating.favorite ? "Unfavorite" : "Favorite",
                systemImage: rating.favorite ? "heart.slash" : "heart"
            )
        }

        Divider()

        // Stars
        ForEach(1...5, id: \.self) { star in
            Button {
                sendUpdate(RatingUpdateBody(stars: star))
            } label: {
                Label(
                    "\(star) Star\(star == 1 ? "" : "s")",
                    systemImage: star <= rating.stars ? "star.fill" : "star"
                )
            }
        }

        Button {
            sendUpdate(RatingUpdateBody(stars: 0))
        } label: {
            Label("Clear Stars", systemImage: "star.slash")
        }

        Divider()

        // Colors
        ForEach(ColorLabel.allCases, id: \.self) { label in
            Button {
                if rating.color == label {
                    sendUpdate(RatingUpdateBody(color: .clear))
                } else {
                    sendUpdate(RatingUpdateBody(color: .set(label)))
                }
            } label: {
                Label(
                    label.rawValue.capitalized,
                    systemImage: rating.color == label ? "circle.fill" : "circle"
                )
            }
        }

        if rating.color != nil {
            Button {
                sendUpdate(RatingUpdateBody(color: .clear))
            } label: {
                Label("Clear Color", systemImage: "xmark.circle")
            }
        }
    }

    private func sendUpdate(_ body: RatingUpdateBody) {
        guard let client else { return }
        Task {
            _ = try? await client.updateRating(assetId: assetId, body: body)
        }
    }
}
