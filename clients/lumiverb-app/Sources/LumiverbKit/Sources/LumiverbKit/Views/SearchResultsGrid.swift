import SwiftUI

/// Justified-row grid for search results. Same layout + scroll-nav
/// pattern as `MediaGridView` — see that file for the rationale on the
/// generic `ScrollIntrospector` parameter and the `@Environment(\.scrollAccessor)`
/// dispatch.
public struct SearchResultsGrid<ScrollIntrospector: View>: View {
    @ObservedObject public var browseState: BrowseState
    public let client: APIClient?
    public let scrollIntrospector: ScrollIntrospector

    @Environment(\.scrollAccessor) private var scrollAccessor

    public init(
        browseState: BrowseState,
        client: APIClient?,
        @ViewBuilder scrollIntrospector: () -> ScrollIntrospector
    ) {
        self.browseState = browseState
        self.client = client
        self.scrollIntrospector = scrollIntrospector()
    }

    public var body: some View {
        GeometryReader { geo in
            let layout = MediaLayout.compute(
                aspectRatios: browseState.searchResults.map { $0.aspectRatio },
                containerWidth: geo.size.width - MediaGridLayoutConstants.spacing * 2,
                targetRowHeight: MediaGridLayoutConstants.targetRowHeight,
                spacing: MediaGridLayoutConstants.spacing
            )

            ScrollView {
                LazyVStack(alignment: .leading, spacing: MediaGridLayoutConstants.spacing) {
                    ForEach(Array(layout.rows.enumerated()), id: \.offset) { _, row in
                        justifiedRow(row: row, layout: layout)
                    }
                }
                .padding(MediaGridLayoutConstants.spacing)
                .background(scrollIntrospector)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .onChange(of: browseState.pendingScrollCommand) { _, token in
                guard let token, let accessor = scrollAccessor else { return }
                accessor.apply(token.command)
            }
        }
    }

    @ViewBuilder
    private func justifiedRow(row: [Int], layout: MediaLayout) -> some View {
        let rowHeight = row.first.map { layout.frames[$0].height } ?? MediaGridLayoutConstants.targetRowHeight
        HStack(spacing: MediaGridLayoutConstants.spacing) {
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

public extension SearchResultsGrid where ScrollIntrospector == EmptyView {
    init(browseState: BrowseState, client: APIClient?) {
        self.init(browseState: browseState, client: client) { EmptyView() }
    }
}

/// A single cell for a search result. Sized externally by the parent.
public struct SearchHitCellView: View {
    public let hit: SearchHit
    public let client: APIClient?

    public init(hit: SearchHit, client: APIClient?) {
        self.hit = hit
        self.client = client
    }

    public var body: some View {
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
