import SwiftUI
import LumiverbKit

/// "Favorites" virtual collection — a 3-column grid of every favorited
/// photo across all libraries. Fetches `/v1/query?f=favorite:yes`
/// directly so it doesn't have to mutate the shared `BrowseState`'s
/// browse filters. Tapping a thumbnail opens the shared lightbox via
/// `browseState.loadAssetDetail`, the same way the cluster review
/// per-face flow does.
///
/// Selection: same model as Photos / PersonDetailView /
/// CollectionDetailView — uses `browseState.selectedAssetIds` so the
/// shared `SelectionToolbarView` works without modification, and the
/// selection is cleared on disappear so it doesn't leak across tabs.
struct FavoritesView: View {
    @ObservedObject var appState: iOSAppState
    @ObservedObject var browseState: BrowseState

    @State private var items: [QueryItem] = []
    @State private var isLoading = false
    @State private var error: String?

    var body: some View {
        VStack(spacing: 0) {
            if browseState.isSelecting {
                SelectionToolbarView(browseState: browseState, client: appState.client)
                Divider()
            }
            content
        }
        .navigationTitle("Favorites")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            // Refresh on every appearance, not just first task — so
            // unfavoriting from the lightbox and tapping back is
            // reflected immediately.
            Task { await loadFavorites() }
        }
        .refreshable {
            await loadFavorites()
        }
        .onDisappear {
            if browseState.isSelecting {
                browseState.clearSelection()
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        if isLoading && items.isEmpty {
            ProgressView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if items.isEmpty {
            ContentUnavailableView(
                "No Favorites Yet",
                systemImage: "star",
                description: Text("Tap the star in the lightbox to favorite a photo.")
            )
        } else {
            DateGroupedGrid(
                browseState: browseState,
                items: items,
                client: appState.client,
                dateString: { $0.takenAt },
                assetId: { $0.assetId },
                isVideo: { $0.isVideo },
                isLoading: isLoading,
                onTap: { item in
                    Task { await browseState.loadAssetDetail(assetId: item.assetId) }
                }
            )
        }
    }

    private func loadFavorites() async {
        guard let client = appState.client else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            let response: QueryResponse = try await client.get(
                "/v1/query",
                queryItems: [
                    URLQueryItem(name: "f", value: "favorite:yes"),
                    URLQueryItem(name: "limit", value: "500"),
                ]
            )
            items = response.items
            error = nil
        } catch {
            self.error = "Couldn't load favorites: \(error.localizedDescription)"
        }
    }
}
