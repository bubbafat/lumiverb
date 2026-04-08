import SwiftUI
import LumiverbKit

/// Virtualized grid of asset thumbnails with infinite scroll.
struct MediaGridView: View {
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 2), count: 4)

    var body: some View {
        ScrollView {
            LazyVGrid(columns: columns, spacing: 2) {
                ForEach(Array(browseState.assets.enumerated()), id: \.element.id) { index, asset in
                    AssetCellView(
                        asset: asset,
                        client: client,
                        isFocused: browseState.focusedIndex == index
                    )
                    .onTapGesture {
                        browseState.focusedIndex = index
                        Task { await browseState.loadAssetDetail(assetId: asset.assetId) }
                    }
                    .onAppear {
                        // Trigger infinite scroll when near the end
                        if index >= browseState.assets.count - 20 {
                            Task { await browseState.loadNextPage() }
                        }
                    }
                }
            }
            .padding(2)

            if browseState.isLoadingAssets {
                ProgressView()
                    .padding()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// A single cell in the media grid.
struct AssetCellView: View {
    let asset: AssetPageItem
    let client: APIClient?
    let isFocused: Bool

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            AuthenticatedImageView(
                assetId: asset.assetId,
                client: client,
                type: .thumbnail
            )
            .frame(minHeight: 120)
            .clipped()
            .background(Color.gray.opacity(0.1))

            // Video overlay: play icon + duration
            if asset.isVideo {
                Image(systemName: "play.fill")
                    .font(.title2)
                    .foregroundColor(.white.opacity(0.9))
                    .shadow(color: .black.opacity(0.5), radius: 4)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)

                if let duration = asset.durationSec {
                    Text(formatDuration(duration))
                        .font(.caption2)
                        .fontWeight(.medium)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(.ultraThinMaterial)
                        .cornerRadius(3)
                        .padding(4)
                }
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

    private func formatDuration(_ seconds: Double) -> String {
        let total = Int(seconds)
        let m = total / 60
        let s = total % 60
        if m >= 60 {
            let h = m / 60
            return String(format: "%d:%02d:%02d", h, m % 60, s)
        }
        return String(format: "%d:%02d", m, s)
    }
}
