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
            HStack(spacing: 12) {
                // Compact X to clear the selection. The previous
                // "Deselect All" text button rendered awkwardly on
                // narrow iPhone screens (label wrapped vertically and
                // ate half the toolbar).
                Button {
                    browseState.clearSelection()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .font(.title3)
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)

                Text("\(browseState.selectedAssetIds.count)")
                    .font(.subheadline.weight(.semibold))

                Divider().frame(height: 20)

                // Batch rating
                batchRatingControls

                if collectionsState != nil {
                    Divider().frame(height: 20)

                    Button {
                        showAddToCollection = true
                    } label: {
                        Image(systemName: "folder.badge.plus")
                            .font(.title3)
                    }
                    .buttonStyle(.plain)
                }

                Spacer()
            }
            .padding(.horizontal, MediaGridLayoutConstants.spacing + 4)
            .padding(.vertical, 8)
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

    /// Stateful favorite indicator: nil = unknown, true = all selected
    /// are favorited (icon shows filled/yellow), false = none/mixed
    /// (icon shows outline). Updated by `refreshFavoriteState` when
    /// the selection changes and after a successful batch toggle.
    @State private var allFavorited: Bool = false

    @ViewBuilder
    private var batchRatingControls: some View {
        HStack(spacing: 8) {
            // Favorite toggle. Tap inspects the current state of the
            // selection and flips it: if every selected asset is
            // already favorited we unfavorite all, otherwise we
            // favorite all. Mirrors the lightbox star button's
            // toggle semantics for the multi-selection case.
            Button {
                Task { await toggleFavorite() }
            } label: {
                Image(systemName: allFavorited ? "star.fill" : "star")
                    .font(.title3)
                    .foregroundColor(allFavorited ? .yellow : .primary)
            }
            .buttonStyle(.plain)
            .help(allFavorited ? "Unfavorite selected" : "Favorite selected")
            .task(id: browseState.selectedAssetIds) {
                await refreshFavoriteState()
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

    /// Look up the favorite state of every selected asset and update
    /// `allFavorited` so the toolbar icon shows the right fill.
    /// Triggered on selection changes via `.task(id:)`.
    private func refreshFavoriteState() async {
        guard let client else {
            allFavorited = false
            return
        }
        let ids = Array(browseState.selectedAssetIds)
        guard !ids.isEmpty else {
            allFavorited = false
            return
        }
        do {
            let ratings = try await client.lookupRatings(assetIds: ids)
            allFavorited = ids.allSatisfy { ratings[$0]?.favorite == true }
        } catch {
            allFavorited = false
        }
    }

    /// Toggle favorite for all selected assets. Inspects the current
    /// state and writes the opposite — if every selected asset is
    /// already favorited, unfavorites all. Otherwise favorites all.
    private func toggleFavorite() async {
        guard let client else { return }
        let ids = Array(browseState.selectedAssetIds)
        guard !ids.isEmpty else { return }
        let target = !allFavorited
        let body = BatchRatingUpdateBody(assetIds: ids, favorite: target)
        _ = try? await client.batchUpdateRatings(body: body)
        // Optimistic update — flip locally so the icon reflects the
        // new state immediately. refreshFavoriteState() would also
        // run if the selection changes, but it doesn't on a no-op
        // tap so we update by hand.
        allFavorited = target
    }
}
