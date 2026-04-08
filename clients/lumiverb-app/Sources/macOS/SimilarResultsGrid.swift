import SwiftUI
import LumiverbKit

/// Grid view for similarity search results.
struct SimilarResultsGrid: View {
    @ObservedObject var browseState: BrowseState
    let sourceAssetId: String
    let client: APIClient?

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 2), count: 4)

    var body: some View {
        VStack(spacing: 0) {
            // Header showing source asset
            HStack {
                Text("Similar to:")
                    .font(.caption)
                    .foregroundColor(.secondary)
                AuthenticatedImageView(
                    assetId: sourceAssetId,
                    client: client,
                    type: .thumbnail
                )
                .frame(width: 40, height: 40)
                .cornerRadius(4)
                .clipped()
                Spacer()
                Button("Back to Library") {
                    browseState.mode = .library
                    browseState.similarResults = []
                }
                .controlSize(.small)
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
            .background(.bar)

            ScrollView {
                LazyVGrid(columns: columns, spacing: 2) {
                    ForEach(Array(browseState.similarResults.enumerated()), id: \.element.id) { index, hit in
                        SimilarHitCellView(
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
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// A single cell for a similarity result.
struct SimilarHitCellView: View {
    let hit: SimilarHit
    let client: APIClient?
    let isFocused: Bool

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
            AuthenticatedImageView(
                assetId: hit.assetId,
                client: client,
                type: .thumbnail
            )
            .frame(minHeight: 120)
            .clipped()
            .background(Color.gray.opacity(0.1))

            // Video play icon
            if hit.mediaType == "video" {
                Image(systemName: "play.fill")
                    .font(.title2)
                    .foregroundColor(.white.opacity(0.9))
                    .shadow(color: .black.opacity(0.5), radius: 4)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            // Distance badge
            Text(String(format: "%.2f", hit.distance))
                .font(.caption2)
                .fontWeight(.medium)
                .padding(.horizontal, 4)
                .padding(.vertical, 1)
                .background(.ultraThinMaterial)
                .cornerRadius(3)
                .padding(4)
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
