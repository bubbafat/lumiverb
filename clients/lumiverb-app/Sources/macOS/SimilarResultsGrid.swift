import SwiftUI
import LumiverbKit

/// Justified-row grid for similarity search results. Same layout +
/// scroll-nav pattern as `MediaGridView` and `SearchResultsGrid`.
struct SimilarResultsGrid: View {
    @ObservedObject var browseState: BrowseState
    let sourceAssetId: String
    let client: APIClient?

    private let targetRowHeight: CGFloat = 180
    private let spacing: CGFloat = 4

    @StateObject private var scrollBox = NSScrollViewBox()

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

            GeometryReader { geo in
                let layout = MediaLayout.compute(
                    aspectRatios: browseState.similarResults.map { $0.aspectRatio },
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
                .onChange(of: browseState.pendingScrollCommand) { _, token in
                    guard let token, let sv = scrollBox.scrollView else { return }
                    applyScrollCommand(token.command, to: sv)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private func justifiedRow(row: [Int], layout: MediaLayout) -> some View {
        let rowHeight = row.first.map { layout.frames[$0].height } ?? targetRowHeight
        HStack(spacing: spacing) {
            ForEach(row, id: \.self) { index in
                let hit = browseState.similarResults[index]
                let size = layout.frames[index]
                SimilarHitCellView(hit: hit, client: client)
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

/// A single cell for a similarity result. Sized externally by the parent.
struct SimilarHitCellView: View {
    let hit: SimilarHit
    let client: APIClient?

    var body: some View {
        ZStack(alignment: .bottomTrailing) {
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
        .cornerRadius(2)
        .contentShape(Rectangle())
    }
}
