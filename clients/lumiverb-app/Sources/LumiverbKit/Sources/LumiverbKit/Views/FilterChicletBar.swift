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
        let chiclets = browseState.filters.activeFilters
        if !chiclets.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(chiclets) { chiclet in
                        FilterChiclet(label: chiclet.label) {
                            var f = browseState.filters
                            chiclet.clear(&f)
                            browseState.filters = f
                        }
                    }

                    if chiclets.count > 1 {
                        Button("Clear all") {
                            browseState.filters.clearAll()
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
