import SwiftUI
import LumiverbKit

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
/// keyboard nav delegated to the underlying `NSScrollView` via the
/// `NSScrollViewIntrospector`. PgUp/PgDn/Home/End/Up/Down all go
/// through AppKit's native scroll API which handles edges and
/// disposed cells correctly. See `AppKitScrollIntrospector.swift`.
struct MediaGridView: View {
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    /// Aspirational row height before scaling. Wider rows shrink, narrower
    /// ones grow, but the average stays close. 180pt feels right at
    /// typical macOS window widths.
    private let targetRowHeight: CGFloat = 180

    /// Cell-to-cell gap, both horizontal (within a row) and vertical
    /// (between rows).
    private let spacing: CGFloat = 4

    @StateObject private var scrollBox = NSScrollViewBox()

    var body: some View {
        GeometryReader { geo in
            let layout = MediaLayout.compute(
                aspectRatios: browseState.assets.map { $0.aspectRatio },
                containerWidth: geo.size.width - spacing * 2,
                targetRowHeight: targetRowHeight,
                spacing: spacing
            )

            ScrollView {
                LazyVStack(alignment: .leading, spacing: spacing) {
                    ForEach(Array(layout.rows.enumerated()), id: \.offset) { _, row in
                        justifiedRow(row: row, layout: layout)
                    }

                    if browseState.isLoadingAssets {
                        ProgressView()
                            .padding()
                            .frame(maxWidth: .infinity)
                    }
                }
                .padding(spacing)
                .background(
                    NSScrollViewIntrospector { sv in
                        scrollBox.scrollView = sv
                        // One arrow-key tap should scroll roughly one
                        // row, not the AppKit default ~10pt.
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

    /// One row of cells laid out left-to-right with explicit per-cell
    /// frames from `MediaLayout`. Wrapping in HStack with the row's
    /// computed height keeps SwiftUI from doing its own layout pass —
    /// the frame sizes are authoritative.
    @ViewBuilder
    private func justifiedRow(row: [Int], layout: MediaLayout) -> some View {
        let rowHeight = row.first.map { layout.frames[$0].height } ?? targetRowHeight
        HStack(spacing: spacing) {
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

/// A single cell in the media grid. Sized externally by `MediaGridView`
/// — the cell itself fills the frame given to it. There is no focus /
/// selection state in Lumiverb's grid; click → lightbox.
struct AssetCellView: View {
    let asset: AssetPageItem
    let client: APIClient?

    var body: some View {
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
