import SwiftUI
import LumiverbKit

/// Libraries tab content. When no library is selected, shows a visual
/// picker grid with cover images for each library. After selection,
/// shows the asset grid with a back button to return to the picker.
struct LibraryBrowseView: View {
    @ObservedObject var appState: iOSAppState
    @ObservedObject var browseState: BrowseState

    /// Preview asset IDs per library for 2x2 thumbnail grids.
    @State private var libraryPreviews: [String: [String]] = [:]

    var body: some View {
        Group {
            if browseState.selectedLibraryId != nil {
                libraryDetail
            } else {
                libraryPickerGrid
            }
        }
        // Lightbox is presented from MainTabView at the top level so
        // it works from any tab. No fullScreenCover here.
        .onChange(of: browseState.selectedLibraryId) { _, newValue in
            guard newValue != nil else { return }
            browseState.handleSelectedLibraryChange()
            UserDefaults.standard.set(newValue, forKey: "lastLibraryId")
        }
        .onAppear {
            restoreLastOpenedLibrary()
        }
    }

    // MARK: - Library picker grid

    @ViewBuilder
    private var libraryPickerGrid: some View {
        if appState.libraries.isEmpty {
            ContentUnavailableView(
                "No Libraries",
                systemImage: "photo.stack",
                description: Text("No libraries available on this server")
            )
        } else {
            ScrollView {
                LazyVGrid(
                    columns: [GridItem(.flexible()), GridItem(.flexible())],
                    spacing: 12
                ) {
                    ForEach(appState.libraries) { lib in
                        Button {
                            browseState.selectedLibraryId = lib.libraryId
                        } label: {
                            LibraryCardView(
                                library: lib,
                                previewAssetIds: libraryPreviews[lib.libraryId] ?? [],
                                client: appState.client
                            )
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.top, 8)
            }
            .refreshable {
                await refreshAllPreviews()
            }
            // Use an unstructured Task in onAppear (rather than `.task`)
            // so the fetch survives a transient view teardown — `.task`
            // cancels its work when the view disappears, which surfaces
            // as a `cancelled` URL error and leaves us with no preview
            // data. Same pattern as PeopleView.
            .onAppear {
                Task { await fetchAllPreviews() }
            }
        }
    }

    // MARK: - Library detail (grid + lightbox)

    private var libraryDetail: some View {
        VStack(spacing: 0) {
            FilterChicletBar(browseState: browseState)
            contentArea
        }
        .navigationTitle(selectedLibraryName)
        .toolbar {
            ToolbarItem(placement: .navigationBarLeading) {
                Button {
                    browseState.selectedLibraryId = nil
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "chevron.left")
                        Text("Libraries")
                    }
                }
            }
        }
    }

    private var selectedLibraryName: String {
        guard let id = browseState.selectedLibraryId else { return "Libraries" }
        return appState.libraries.first(where: { $0.libraryId == id })?.name ?? "Library"
    }

    // MARK: - Content area (mode switch)

    @ViewBuilder
    private var contentArea: some View {
        switch browseState.mode {
        case .library:
            libraryContent

        case .search:
            searchContent

        case .similar(let sourceId):
            similarContent(sourceId: sourceId)
        }
    }

    @ViewBuilder
    private var libraryContent: some View {
        if browseState.assets.isEmpty && !browseState.isLoadingAssets {
            ContentUnavailableView(
                "No Assets",
                systemImage: "photo.on.rectangle.angled",
                description: Text("This library has no assets yet")
            )
        } else {
            VStack(spacing: 0) {
                SelectionToolbarView(browseState: browseState, client: appState.client)
                MediaGridView(browseState: browseState, client: appState.client) {
                    EmptyView()
                }
                .refreshable {
                    await browseState.refreshCurrent()
                }
            }
        }
    }

    @ViewBuilder
    private var searchContent: some View {
        if browseState.isSearching {
            ProgressView("Searching...")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if browseState.searchResults.isEmpty {
            ContentUnavailableView.search(text: browseState.committedSearchQuery)
        } else {
            SearchResultsGrid(browseState: browseState, client: appState.client) {
                EmptyView()
            }
        }
    }

    @ViewBuilder
    private func similarContent(sourceId: String) -> some View {
        if browseState.isFindingSimilar {
            ProgressView("Finding similar...")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if browseState.similarResults.isEmpty {
            ContentUnavailableView(
                "No Similar Assets",
                systemImage: "square.stack.3d.up",
                description: Text("No similar assets were found")
            )
        } else {
            SimilarResultsGrid(
                browseState: browseState,
                sourceAssetId: sourceId,
                client: appState.client
            ) {
                EmptyView()
            }
        }
    }


    // MARK: - Preview fetching

    /// Fetch previews for libraries that don't have an entry yet (initial
    /// load). Skips libraries we've already fetched so re-renders don't
    /// re-hit the network.
    private func fetchAllPreviews() async {
        guard let client = appState.client else { return }
        // Snapshot which libraries already have previews. Reading
        // libraryPreviews once up front avoids the SwiftUI struct/state
        // capture trap where the closure sees a stale snapshot.
        let alreadyFetched = libraryPreviews
        await withTaskGroup(of: (String, [String]).self) { group in
            for lib in appState.libraries {
                guard alreadyFetched[lib.libraryId] == nil else { continue }
                group.addTask {
                    let ids = await Self.fetchPreviewIds(
                        client: client, libraryId: lib.libraryId
                    )
                    return (lib.libraryId, ids)
                }
            }
            for await (libId, ids) in group {
                libraryPreviews[libId] = ids
            }
        }
    }

    /// Force-refresh: re-fetch every library's previews and atomically
    /// swap. Builds a new dict locally so SwiftUI's @State capture
    /// semantics don't make the workers see a stale snapshot.
    private func refreshAllPreviews() async {
        guard let client = appState.client else { return }
        var newPreviews: [String: [String]] = [:]
        await withTaskGroup(of: (String, [String]).self) { group in
            for lib in appState.libraries {
                group.addTask {
                    let ids = await Self.fetchPreviewIds(
                        client: client, libraryId: lib.libraryId
                    )
                    return (lib.libraryId, ids)
                }
            }
            for await (libId, ids) in group {
                newPreviews[libId] = ids
            }
        }
        // Atomic swap — never empty the dict before the new data lands,
        // so cards don't flash to the cover_asset_id fallback.
        libraryPreviews = newPreviews
    }

    private static func fetchPreviewIds(
        client: APIClient, libraryId: String
    ) async -> [String] {
        do {
            // Use the f= filter prefix syntax — query endpoint ignores
            // library_id as a plain query param.
            let response: QueryResponse = try await client.get(
                "/v1/query",
                queryItems: [
                    URLQueryItem(name: "f", value: "library:\(libraryId)"),
                    URLQueryItem(name: "limit", value: "4"),
                ]
            )
            return response.items.map(\.assetId)
        } catch {
            return []
        }
    }

    // MARK: - Last-opened library restore

    private func restoreLastOpenedLibrary() {
        guard browseState.selectedLibraryId == nil else { return }
        guard let lastId = UserDefaults.standard.string(forKey: "lastLibraryId") else { return }
        guard appState.libraries.contains(where: { $0.libraryId == lastId }) else { return }
        browseState.selectedLibraryId = lastId
    }
}
