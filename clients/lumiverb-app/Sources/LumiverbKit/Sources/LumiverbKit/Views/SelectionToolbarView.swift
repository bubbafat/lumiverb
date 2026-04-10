import SwiftUI

/// Toolbar that appears above the grid when assets are selected.
/// Shows selection count and batch operation buttons.
public struct SelectionToolbarView: View {
    @ObservedObject public var browseState: BrowseState
    public let client: APIClient?

    @Environment(\.collectionsState) private var collectionsState
    @State private var showAddToCollection = false
    @State private var batchRating = Rating.empty

    public init(browseState: BrowseState, client: APIClient?) {
        self.browseState = browseState
        self.client = client
    }

    public var body: some View {
        if !browseState.selectedAssetIds.isEmpty {
            HStack(spacing: 16) {
                // Count + deselect
                Text("\(browseState.selectedAssetIds.count) selected")
                    .font(.subheadline)
                    .fontWeight(.medium)

                Button("Deselect All") {
                    browseState.clearSelection()
                }
                .controlSize(.small)

                Divider()
                    .frame(height: 20)

                // Batch rating
                batchRatingControls

                if collectionsState != nil {
                    Divider()
                        .frame(height: 20)

                    Button {
                        showAddToCollection = true
                    } label: {
                        Label("Add to Collection", systemImage: "folder.badge.plus")
                    }
                    .controlSize(.small)
                }

                Spacer()
            }
            .padding(.horizontal, MediaGridLayoutConstants.spacing)
            .padding(.vertical, 6)
            .background(.bar)
            .sheet(isPresented: $showAddToCollection) {
                if let cs = collectionsState {
                    AddToCollectionSheet(
                        collectionsState: cs,
                        assetIds: Array(browseState.selectedAssetIds)
                    )
                }
            }
            #if os(macOS)
            .onKeyPress(.escape) {
                browseState.clearSelection()
                return .handled
            }
            .onKeyPress(characters: .init(charactersIn: "aA")) { press in
                // Cmd+A selects all
                if press.modifiers.contains(.command) {
                    browseState.selectAll()
                    return .handled
                }
                return .ignored
            }
            #endif
        }
    }

    // MARK: - Batch rating controls

    @ViewBuilder
    private var batchRatingControls: some View {
        HStack(spacing: 8) {
            // Favorite toggle
            Button {
                let ids = Array(browseState.selectedAssetIds)
                let body = BatchRatingUpdateBody(assetIds: ids, favorite: true)
                Task { _ = try? await client?.batchUpdateRatings(body: body) }
            } label: {
                Image(systemName: "heart")
                    .font(.body)
            }
            .buttonStyle(.plain)
            .help("Favorite selected")

            // Star buttons
            ForEach(1...5, id: \.self) { star in
                Button {
                    let ids = Array(browseState.selectedAssetIds)
                    let body = BatchRatingUpdateBody(assetIds: ids, stars: star)
                    Task { _ = try? await client?.batchUpdateRatings(body: body) }
                } label: {
                    Image(systemName: "star")
                        .font(.caption)
                }
                .buttonStyle(.plain)
                .help("\(star) star\(star == 1 ? "" : "s")")
            }

            // Color swatches
            ForEach(ColorLabel.allCases, id: \.self) { label in
                Button {
                    let ids = Array(browseState.selectedAssetIds)
                    let body = BatchRatingUpdateBody(assetIds: ids, color: .set(label))
                    Task { _ = try? await client?.batchUpdateRatings(body: body) }
                } label: {
                    Circle()
                        .fill(colorForLabel(label))
                        .frame(width: 14, height: 14)
                }
                .buttonStyle(.plain)
                .help(label.rawValue.capitalized)
            }
        }
    }

    private func colorForLabel(_ label: ColorLabel) -> Color {
        switch label {
        case .red: return .red
        case .orange: return .orange
        case .yellow: return .yellow
        case .green: return .green
        case .blue: return .blue
        case .purple: return .purple
        }
    }
}
