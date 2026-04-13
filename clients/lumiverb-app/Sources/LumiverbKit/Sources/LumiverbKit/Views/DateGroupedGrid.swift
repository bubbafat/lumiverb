import SwiftUI

#if os(iOS)

/// Shared 2-column iOS grid with date-grouped sections. Single source
/// of truth for Photos / Collections / People / Favorites — change
/// here once and the appearance/behavior propagates everywhere.
///
/// Generic over the item type so each call site keeps its own data
/// shape (AssetPageItem, CollectionAsset, PersonFaceItem, QueryItem).
/// Each item supplies its own date string, asset id, and is-video flag
/// via closures, plus a tap handler and a "near the end" hook for
/// pagination.
///
/// Selection is shared via `BrowseState.selectedAssetIds` so the
/// existing `SelectionToolbarView` works without modification across
/// every grid that uses this view.
public struct DateGroupedGrid<Item: Identifiable>: View {
    @ObservedObject public var browseState: BrowseState
    public let items: [Item]
    public let client: APIClient?
    public let dateString: (Item) -> String?
    public let assetId: (Item) -> String
    public let isVideo: (Item) -> Bool
    public let onTap: (Item) -> Void
    public let onLastItemAppear: (Item) -> Void
    public let isLoading: Bool
    /// Optional caption rendered below each cell. Used by search to
    /// surface the matched AI description so users can see *why* a
    /// result came back without tapping into every photo. Default nil
    /// keeps Photos / Collections / People / Favorites unaffected.
    /// Returns `AttributedString` so the search grid can highlight
    /// matched terms inside the caption.
    public let caption: ((Item) -> AttributedString?)?
    /// When true (default), items are grouped into date sections
    /// sorted most-recent-first. When false, items render in their
    /// insertion order with no section headers — used by search,
    /// where the input is already in BM25 relevance order and date
    /// grouping would silently re-sort it chronologically and undo
    /// the server-side ranking.
    public let groupByDate: Bool

    public init(
        browseState: BrowseState,
        items: [Item],
        client: APIClient?,
        dateString: @escaping (Item) -> String?,
        assetId: @escaping (Item) -> String,
        isVideo: @escaping (Item) -> Bool = { _ in false },
        isLoading: Bool = false,
        onTap: @escaping (Item) -> Void,
        onLastItemAppear: @escaping (Item) -> Void = { _ in },
        caption: ((Item) -> AttributedString?)? = nil,
        groupByDate: Bool = true
    ) {
        self.browseState = browseState
        self.items = items
        self.client = client
        self.dateString = dateString
        self.assetId = assetId
        self.isVideo = isVideo
        self.isLoading = isLoading
        self.onTap = onTap
        self.onLastItemAppear = onLastItemAppear
        self.caption = caption
        self.groupByDate = groupByDate
    }

    private static var columns: [GridItem] {
        [
            GridItem(.flexible(), spacing: MediaGridLayoutConstants.spacing),
            GridItem(.flexible(), spacing: MediaGridLayoutConstants.spacing),
        ]
    }

    public var body: some View {
        let lastId = items.last.map(assetId)
        ScrollView {
            LazyVGrid(
                columns: Self.columns,
                spacing: MediaGridLayoutConstants.spacing
            ) {
                if groupByDate {
                    let buckets = bucketByDate(items, dateString: dateString, assetId: assetId)
                    ForEach(buckets) { bucket in
                        Section {
                            ForEach(bucket.items) { item in
                                cell(item, lastId: lastId)
                            }
                        } header: {
                            DateBucketHeader(
                                label: bucket.label,
                                assetIds: bucket.assetIds,
                                dateISO: bucket.dateISO,
                                browseState: browseState
                            )
                        }
                    }
                } else {
                    // Flat insertion-order render. Used by search,
                    // where the input is already in BM25 relevance
                    // order — bucketing would silently re-sort it
                    // chronologically and undo the server ranking.
                    ForEach(items) { item in
                        cell(item, lastId: lastId)
                    }
                }

                if isLoading {
                    ProgressView()
                        .padding()
                        .frame(maxWidth: .infinity)
                        .gridCellColumns(2)
                }
            }
            .padding(MediaGridLayoutConstants.spacing)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private func cell(_ item: Item, lastId: String?) -> some View {
        let id = assetId(item)
        let selected = browseState.selectedAssetIds.contains(id)
        VStack(alignment: .leading, spacing: 4) {
            AssetGridCell(
                assetId: id,
                isVideo: isVideo(item),
                isSelected: selected,
                client: client,
                onToggleSelect: {
                    browseState.toggleSelection(assetId: id)
                }
            )
            if let captionText = caption?(item),
               !String(captionText.characters).isEmpty {
                Text(captionText)
                    .font(.caption2)
                    .foregroundColor(.secondary)
                    .lineLimit(2)
                    .truncationMode(.tail)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 2)
            }
        }
        .onTapGesture {
            if browseState.isSelecting {
                browseState.toggleSelection(assetId: id)
            } else {
                onTap(item)
            }
        }
        .onAppear {
            if id == lastId {
                onLastItemAppear(item)
            }
        }
    }
}

/// Section header for `DateGroupedGrid`. Tappable to select / deselect
/// every asset in the date. Mirrors the existing `DateHeaderView`
/// behaviour but takes plain `[String]` IDs so it works with any item
/// type the generic grid is rendering.
struct DateBucketHeader: View {
    let label: String
    let assetIds: [String]
    let dateISO: String?
    @ObservedObject var browseState: BrowseState

    var body: some View {
        let allSelected = !assetIds.isEmpty &&
            Set(assetIds).isSubset(of: browseState.selectedAssetIds)

        HStack {
            Image(systemName: allSelected ? "checkmark.circle.fill" : "circle")
                .foregroundColor(allSelected ? .accentColor : .secondary)

            Text(label)
                .font(.headline)
                .foregroundColor(.primary)

            Text("\(assetIds.count)")
                .font(.caption)
                .foregroundColor(.secondary)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(.quaternary)
                .cornerRadius(8)

            Spacer()
        }
        .frame(height: MediaGridLayoutConstants.headerHeight)
        .frame(maxWidth: .infinity)
        .background(Color.gray.opacity(0.15))
        .contentShape(Rectangle())
        .onTapGesture {
            browseState.selectGroup(assetIds, dateISO: dateISO)
        }
    }
}

#endif
