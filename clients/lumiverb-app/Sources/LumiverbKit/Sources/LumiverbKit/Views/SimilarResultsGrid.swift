import SwiftUI

/// Justified-row grid for similarity search results. Same layout +
/// scroll-nav pattern as `MediaGridView` and `SearchResultsGrid`.
public struct SimilarResultsGrid<ScrollIntrospector: View>: View {
    @ObservedObject public var browseState: BrowseState
    public let sourceAssetId: String
    public let client: APIClient?
    public let scrollIntrospector: ScrollIntrospector

    @Environment(\.scrollAccessor) private var scrollAccessor

    public init(
        browseState: BrowseState,
        sourceAssetId: String,
        client: APIClient?,
        @ViewBuilder scrollIntrospector: () -> ScrollIntrospector
    ) {
        self.browseState = browseState
        self.sourceAssetId = sourceAssetId
        self.client = client
        self.scrollIntrospector = scrollIntrospector()
    }

    public var body: some View {
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
                .onChange(of: browseState.pendingScrollCommand) { _, token in
                    guard let token, let accessor = scrollAccessor else { return }
                    accessor.apply(token.command)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private func justifiedRow(row: [Int], layout: MediaLayout) -> some View {
        let rowHeight = row.first.map { layout.frames[$0].height } ?? MediaGridLayoutConstants.targetRowHeight
        HStack(spacing: MediaGridLayoutConstants.spacing) {
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

public extension SimilarResultsGrid where ScrollIntrospector == EmptyView {
    init(browseState: BrowseState, sourceAssetId: String, client: APIClient?) {
        self.init(
            browseState: browseState,
            sourceAssetId: sourceAssetId,
            client: client
        ) { EmptyView() }
    }
}

/// A single cell for a similarity result. Sized externally by the parent.
public struct SimilarHitCellView: View {
    public let hit: SimilarHit
    public let client: APIClient?

    public init(hit: SimilarHit, client: APIClient?) {
        self.hit = hit
        self.client = client
    }

    public var body: some View {
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
