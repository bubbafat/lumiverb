import SwiftUI

/// Layout constants for the justified-row media grid. Public so the
/// macOS-side `NSScrollViewIntrospector` callback can match the grid's
/// row height when configuring `NSScrollView.verticalLineScroll` (so one
/// arrow-key press scrolls roughly one row, not the AppKit default
/// ~10pt). iOS doesn't need this — touch scrolling has no concept of
/// "one line" — but it's harmless to expose.
public enum MediaGridLayoutConstants {
    public static let targetRowHeight: CGFloat = 180
    public static let spacing: CGFloat = 4
    /// `verticalLineScroll` target for AppKit's `NSScrollView`. One arrow
    /// key tap should advance roughly one row.
    public static var verticalLineScrollHeight: CGFloat {
        targetRowHeight + spacing
    }
}

/// Justified-row grid of asset thumbnails with infinite scroll.
///
/// Google Photos / Flickr-style layout: rows have uniform height but
/// variable item count, items keep their natural aspect ratio (no
/// cropping). The row packing happens in `MediaLayout` (LumiverbKit,
/// unit-tested), driven by the asset's `aspectRatio` and the container
/// width measured by a `GeometryReader`.
///
/// **No focus / selection.** Click opens the lightbox directly.
///
/// **Scrolling:** uses `LazyVStack` for lazy loading of cells, with
/// keyboard nav delegated to the underlying scroll view via a generic
/// `ScrollIntrospector` parameter. macOS callers pass an
/// `NSScrollViewIntrospector` that walks up to find the `NSScrollView`
/// and stashes it in `MacScrollAccessor.box.scrollView`. iOS callers
/// (M6) pass a `UIScrollViewIntrospector` doing the same with
/// `UIScrollView`. The introspector view sits inside the
/// `LazyVStack`'s `.background` so the superview walk reaches the real
/// scroll view rather than walking up out of it.
///
/// Scroll commands themselves are dispatched via
/// `@Environment(\.scrollAccessor)` — `BrowseState.pendingScrollCommand`
/// triggers an `accessor.apply(token.command)` call. The accessor is
/// the same instance the introspector populated.
public struct MediaGridView<ScrollIntrospector: View>: View {
    @ObservedObject public var browseState: BrowseState
    public let client: APIClient?
    public let scrollIntrospector: ScrollIntrospector

    @Environment(\.scrollAccessor) private var scrollAccessor
    @Environment(\.collectionsState) private var collectionsState
    @State private var addToCollectionAssetId: String?

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
                aspectRatios: browseState.assets.map { $0.aspectRatio },
                containerWidth: geo.size.width - MediaGridLayoutConstants.spacing * 2,
                targetRowHeight: MediaGridLayoutConstants.targetRowHeight,
                spacing: MediaGridLayoutConstants.spacing
            )

            ScrollView {
                LazyVStack(alignment: .leading, spacing: MediaGridLayoutConstants.spacing) {
                    ForEach(Array(layout.rows.enumerated()), id: \.offset) { _, row in
                        justifiedRow(row: row, layout: layout)
                    }

                    if browseState.isLoadingAssets {
                        ProgressView()
                            .padding()
                            .frame(maxWidth: .infinity)
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
            .sheet(isPresented: Binding(
                get: { addToCollectionAssetId != nil },
                set: { if !$0 { addToCollectionAssetId = nil } }
            )) {
                if let assetId = addToCollectionAssetId, let cs = collectionsState {
                    AddToCollectionSheet(collectionsState: cs, assetIds: [assetId])
                }
            }
        }
    }

    /// One row of cells laid out left-to-right with explicit per-cell
    /// frames from `MediaLayout`. Wrapping in HStack with the row's
    /// computed height keeps SwiftUI from doing its own layout pass —
    /// the frame sizes are authoritative.
    @ViewBuilder
    private func justifiedRow(row: [Int], layout: MediaLayout) -> some View {
        let rowHeight = row.first.map { layout.frames[$0].height } ?? MediaGridLayoutConstants.targetRowHeight
        HStack(spacing: MediaGridLayoutConstants.spacing) {
            ForEach(row, id: \.self) { index in
                let asset = browseState.assets[index]
                let size = layout.frames[index]
                AssetCellView(asset: asset, client: client)
                    .frame(width: size.width, height: size.height)
                    // Outer .clipped() prevents .aspectRatio(.fill)
                    // overflow from bleeding into neighboring cells.
                    .clipped()
                    .onTapGesture {
                        // Click → lightbox. focusedIndex tracks the
                        // current lightbox position so prev/next
                        // arrow keys can advance through the list.
                        browseState.focusedIndex = index
                        Task { await browseState.loadAssetDetail(assetId: asset.assetId) }
                    }
                    .contextMenu {
                        AssetRatingContextMenu(
                            assetId: asset.assetId,
                            client: client
                        )
                        if collectionsState != nil {
                            Divider()
                            Button {
                                addToCollectionAssetId = asset.assetId
                            } label: {
                                Label("Add to Collection...", systemImage: "folder.badge.plus")
                            }
                        }
                    }
                    .onAppear {
                        // Trigger infinite scroll when near the end.
                        if index >= browseState.assets.count - 20 {
                            Task { await browseState.loadNextPage() }
                        }
                    }
            }
        }
        .frame(height: rowHeight)
    }
}

/// Convenience initializer for callers (e.g. previews and test contexts)
/// that don't have a real platform scroll introspector to attach. The
/// grid will render correctly but scroll commands will silently no-op.
public extension MediaGridView where ScrollIntrospector == EmptyView {
    init(browseState: BrowseState, client: APIClient?) {
        self.init(browseState: browseState, client: client) { EmptyView() }
    }
}

/// A single cell in the media grid. Sized externally by `MediaGridView`
/// — the cell itself fills the frame given to it. There is no focus /
/// selection state in Lumiverb's grid; click → lightbox.
public struct AssetCellView: View {
    public let asset: AssetPageItem
    public let client: APIClient?

    public init(asset: AssetPageItem, client: APIClient?) {
        self.asset = asset
        self.client = client
    }

    public var body: some View {
        ZStack(alignment: .bottomLeading) {
            AuthenticatedImageView(
                assetId: asset.assetId,
                client: client,
                type: .thumbnail
            )
            .background(Color.gray.opacity(0.1))
            .clipped()

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
        .cornerRadius(2)
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
