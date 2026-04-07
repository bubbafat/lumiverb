import SwiftUI
import LumiverbKit

/// Grid view for search results.
struct SearchResultsGrid: View {
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 2), count: 4)

    var body: some View {
        ScrollView {
            LazyVGrid(columns: columns, spacing: 2) {
                ForEach(Array(browseState.searchResults.enumerated()), id: \.element.id) { index, hit in
                    SearchHitCellView(
                        hit: hit,
                        client: client,
                        isFocused: browseState.focusedIndex == index
                    )
                    .onTapGesture {
                        browseState.focusedIndex = index
                        Task { await browseState.loadAssetDetail(assetId: hit.assetId) }
                    }
                }
            }
            .padding(2)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// A single cell for a search result.
struct SearchHitCellView: View {
    let hit: SearchHit
    let client: APIClient?
    let isFocused: Bool

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            AuthenticatedImageView(
                assetId: hit.assetId,
                client: client,
                type: .thumbnail
            )
            .frame(minHeight: 120)
            .clipped()
            .background(Color.gray.opacity(0.1))

            // Hit type badge for scenes/transcripts
            if hit.type != "image" {
                Text(hit.type)
                    .font(.caption2)
                    .fontWeight(.medium)
                    .padding(.horizontal, 4)
                    .padding(.vertical, 1)
                    .background(.ultraThinMaterial)
                    .cornerRadius(3)
                    .padding(4)
            }
        }
        .aspectRatio(1, contentMode: .fill)
        .cornerRadius(2)
        .overlay(
            RoundedRectangle(cornerRadius: 2)
                .stroke(isFocused ? Color.accentColor : .clear, lineWidth: 2)
        )
        .contentShape(Rectangle())
    }
}
