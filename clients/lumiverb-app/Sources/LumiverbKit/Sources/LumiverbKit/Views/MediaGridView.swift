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
    /// Height of date section headers.
    public static let headerHeight: CGFloat = 40
}

/// Justified-row grid of asset thumbnails with date-section headers,
/// multi-select, and infinite scroll.
///
/// Assets are grouped by date (taken_at with created_at fallback) and
/// rendered in sections with date headers. Each header shows the date
/// label, asset count, and a select-all checkbox. Cells support both
/// browse mode (tap → lightbox) and select mode (tap → toggle selection,
/// Cmd+click on macOS enters select mode).
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
            let containerWidth = geo.size.width - MediaGridLayoutConstants.spacing * 2
            let dateGroups = groupAssetsByDate(browseState.assets)

            ScrollView {
                LazyVStack(alignment: .leading, spacing: MediaGridLayoutConstants.spacing) {
                    ForEach(dateGroups, id: \.label) { group in
                        dateSection(group: group, containerWidth: containerWidth)
                    }

                    // Infinite scroll sentinel — triggers next page load
                    // when the user scrolls near the bottom.
                    Color.clear
                        .frame(height: 1)
                        .onAppear {
                            Task { await browseState.loadNextPage() }
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

    // MARK: - Date section

    @ViewBuilder
    private func dateSection(group: DateGroup, containerWidth: CGFloat) -> some View {
        let layout = MediaLayout.compute(
            aspectRatios: group.assets.map { $0.aspectRatio },
            containerWidth: containerWidth,
            targetRowHeight: MediaGridLayoutConstants.targetRowHeight,
            spacing: MediaGridLayoutConstants.spacing
        )

        // Date header
        dateHeader(group: group)

        // Rows for this section — use a stable ID combining the section's
        // dateISO with the row offset so SwiftUI can differentiate rows
        // across sections.
        let sectionKey = group.dateISO ?? "unknown"
        ForEach(Array(layout.rows.enumerated()), id: \.offset) { rowIdx, row in
            sectionRow(row: row, layout: layout, assets: group.assets)
                .id("\(sectionKey)-\(rowIdx)")
        }
    }

    @ViewBuilder
    private func dateHeader(group: DateGroup) -> some View {
        let groupIds = group.assets.map(\.assetId)
        let allSelected = !groupIds.isEmpty && Set(groupIds).isSubset(of: browseState.selectedAssetIds)

        HStack {
            Button {
                browseState.selectGroup(groupIds, dateISO: group.dateISO)
            } label: {
                Image(systemName: allSelected ? "checkmark.circle.fill" : "circle")
                    .foregroundColor(allSelected ? .accentColor : .secondary)
            }
            .buttonStyle(.plain)

            Text(group.label)
                .font(.headline)
                .foregroundColor(.primary)

            Text("\(group.assets.count)")
                .font(.caption)
                .foregroundColor(.secondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(.quaternary)
                .cornerRadius(8)

            Spacer()
        }
        .frame(height: MediaGridLayoutConstants.headerHeight)
        .padding(.top, 4)
    }

    // MARK: - Row

    @ViewBuilder
    private func sectionRow(row: [Int], layout: MediaLayout, assets: [AssetPageItem]) -> some View {
        let rowHeight = row.first.map { layout.frames[$0].height } ?? MediaGridLayoutConstants.targetRowHeight
        HStack(spacing: MediaGridLayoutConstants.spacing) {
            ForEach(row, id: \.self) { index in
                let asset = assets[index]
                let size = layout.frames[index]
                let isSelected = browseState.selectedAssetIds.contains(asset.assetId)

                assetCell(asset: asset, isSelected: isSelected)
                    .frame(width: size.width, height: size.height)
                    .clipped()
                    .onTapGesture {
                        handleTap(asset: asset)
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
            }
        }
        .frame(height: rowHeight)
    }

    // MARK: - Cell

    @ViewBuilder
    private func assetCell(asset: AssetPageItem, isSelected: Bool) -> some View {
        ZStack(alignment: .topLeading) {
            AssetCellView(asset: asset, client: client)

            // Selection circle — always visible so users know they can
            // select. Becomes filled + blue when selected.
            Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                .font(.title3)
                .foregroundColor(isSelected ? .accentColor : .white.opacity(0.6))
                .shadow(color: .black.opacity(0.5), radius: 2)
                .padding(6)

            // Selected border
            if isSelected {
                RoundedRectangle(cornerRadius: 2)
                    .strokeBorder(Color.accentColor, lineWidth: 3)
            }
        }
        .cornerRadius(2)
        .contentShape(Rectangle())
    }

    // MARK: - Tap handling

    private func handleTap(asset: AssetPageItem) {
        if browseState.isSelecting {
            browseState.toggleSelection(assetId: asset.assetId)
        } else {
            // Browse mode: open lightbox
            if let idx = browseState.assets.firstIndex(where: { $0.assetId == asset.assetId }) {
                browseState.focusedIndex = idx
            }
            Task { await browseState.loadAssetDetail(assetId: asset.assetId) }
        }
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
/// — the cell itself fills the frame given to it.
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
