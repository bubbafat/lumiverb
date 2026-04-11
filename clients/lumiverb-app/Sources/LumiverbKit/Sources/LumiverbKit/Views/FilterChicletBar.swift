import SwiftUI

/// Horizontal bar of filter chiclets shown above the grid when any
/// browse filter is active. Each chiclet shows the filter label and
/// an X button to clear it. A "..." menu offers "Clear all" and
/// "Save as Smart Collection".
public struct FilterChicletBar: View {
    @ObservedObject public var browseState: BrowseState
    public var onSaveSmartCollection: (() -> Void)?

    public init(browseState: BrowseState, onSaveSmartCollection: (() -> Void)? = nil) {
        self.browseState = browseState
        self.onSaveSmartCollection = onSaveSmartCollection
    }

    public var body: some View {
        let filterChiclets = browseState.filters.activeFilters
        let hasPath = browseState.selectedPath != nil
        let hasSearch = browseState.mode == .search && !browseState.searchQuery.isEmpty
        let totalCount = filterChiclets.count + (hasPath ? 1 : 0) + (hasSearch ? 1 : 0)

        if totalCount > 0 {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    // Search query chiclet
                    if hasSearch {
                        FilterChiclet(label: "Search: \"\(browseState.searchQuery)\"") {
                            browseState.clearSearch()
                        }
                    }

                    // Path filter (lives on BrowseState, not BrowseFilter)
                    if let path = browseState.selectedPath {
                        FilterChiclet(label: "\u{1F4C1} \(path)") {
                            browseState.selectedPath = nil
                        }
                    }

                    // BrowseFilter chiclets
                    ForEach(filterChiclets) { chiclet in
                        FilterChiclet(label: chiclet.label) {
                            var f = browseState.filters
                            chiclet.clear(&f)
                            browseState.filters = f
                        }
                    }

                    Menu {
                        if onSaveSmartCollection != nil {
                            Button {
                                onSaveSmartCollection?()
                            } label: {
                                Label("Save as Smart Collection", systemImage: "wand.and.stars")
                            }
                            Divider()
                        }

                        Button(role: .destructive) {
                            browseState.filters.clearAll()
                            browseState.selectedPath = nil
                        } label: {
                            Label("Clear All Filters", systemImage: "xmark.circle")
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                            .font(.body)
                            .foregroundColor(.secondary)
                            .frame(minWidth: 32, minHeight: 32)
                    }
                }
                .padding(.horizontal, MediaGridLayoutConstants.spacing)
                .padding(.vertical, 6)
            }
            .background(.bar)
        }
    }
}

/// A single filter chiclet: rounded pill with label + dismiss button.
struct FilterChiclet: View {
    let label: String
    let onClear: () -> Void

    var body: some View {
        HStack(spacing: 4) {
            Text(label)
                .font(.caption)
                .lineLimit(1)

            Button {
                onClear()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundColor(.secondary)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(.quaternary)
        .cornerRadius(12)
    }
}
