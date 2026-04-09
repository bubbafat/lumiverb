import SwiftUI
import LumiverbKit

/// Justified-row grid for search results. Same layout + scroll-nav
/// pattern as `MediaGridView` — see that file for the rationale.
struct SearchResultsGrid: View {
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    private let targetRowHeight: CGFloat = 180
    private let spacing: CGFloat = 4

    @StateObject private var scrollBox = NSScrollViewBox()

    var body: some View {
        GeometryReader { geo in
            let layout = MediaLayout.compute(
                aspectRatios: browseState.searchResults.map { $0.aspectRatio },
                containerWidth: geo.size.width - spacing * 2,
                targetRowHeight: targetRowHeight,
                spacing: spacing
            )

            ScrollView {
                LazyVStack(alignment: .leading, spacing: spacing) {
                    ForEach(Array(layout.rows.enumerated()), id: \.offset) { _, row in
                        justifiedRow(row: row, layout: layout)
                    }
                }
                .padding(spacing)
                .background(
                    NSScrollViewIntrospector { sv in
                        scrollBox.scrollView = sv
                        sv.verticalLineScroll = targetRowHeight + spacing
                    }
                )
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .onChange(of: browseState.pendingScrollCommand) { _, token in
                guard let token, let sv = scrollBox.scrollView else { return }
                applyScrollCommand(token.command, to: sv)
            }
        }
    }

    @ViewBuilder
    private func justifiedRow(row: [Int], layout: MediaLayout) -> some View {
        let rowHeight = row.first.map { layout.frames[$0].height } ?? targetRowHeight
        HStack(spacing: spacing) {
            ForEach(row, id: \.self) { index in
                let hit = browseState.searchResults[index]
                let size = layout.frames[index]
                SearchHitCellView(hit: hit, client: client)
                    .frame(width: size.width, height: size.height)
                    .clipped()
                    .onTapGesture {
                        browseState.focusedIndex = index
                        Task { await browseState.loadAssetDetail(assetId: hit.assetId) }
                    }
            }
        }
        .frame(height: rowHeight)
    }
}

/// A single cell for a search result. Sized externally by the parent.
struct SearchHitCellView: View {
    let hit: SearchHit
    let client: APIClient?

    var body: some View {
        ZStack(alignment: .bottomLeading) {
            AuthenticatedImageView(
                assetId: hit.assetId,
                client: client,
                type: .thumbnail
            )
            .background(Color.gray.opacity(0.1))
            .clipped()

            // Video play icon
            if hit.mediaType == "video" {
                Image(systemName: "play.fill")
                    .font(.title2)
                    .foregroundColor(.white.opacity(0.9))
                    .shadow(color: .black.opacity(0.5), radius: 4)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

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
        .cornerRadius(2)
        .contentShape(Rectangle())
    }
}
