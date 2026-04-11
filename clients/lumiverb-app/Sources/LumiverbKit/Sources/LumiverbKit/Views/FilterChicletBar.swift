import SwiftUI

/// Horizontal bar of filter chiclets shown above the grid when any
/// browse filter is active. Each chiclet shows the filter label and
/// an X button to clear it. A "Clear all" button clears everything.
public struct FilterChicletBar: View {
    @ObservedObject public var browseState: BrowseState

    public init(browseState: BrowseState) {
        self.browseState = browseState
    }

    public var body: some View {
        let filterChiclets = browseState.filters.activeFilters
        let hasPath = browseState.selectedPath != nil
        let totalCount = filterChiclets.count + (hasPath ? 1 : 0)

        if totalCount > 0 {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    // Path filter (lives on BrowseState, not BrowseFilter)
                    if let path = browseState.selectedPath {
                        FilterChiclet(label: "📁 \(path)") {
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

                    if totalCount > 1 {
                        Button("Clear all") {
                            browseState.filters.clearAll()
                            browseState.selectedPath = nil
                        }
                        .font(.caption)
                        .foregroundColor(.secondary)
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
