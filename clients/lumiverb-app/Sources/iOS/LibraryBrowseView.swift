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
        .fullScreenCover(isPresented: lightboxBinding) {
            iOSLightboxView(browseState: browseState, client: appState.client)
        }
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
            .task {
                await fetchAllPreviews()
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

    // MARK: - Lightbox binding

    private var lightboxBinding: Binding<Bool> {
        Binding(
            get: { browseState.selectedAssetId != nil },
            set: { isPresented in
                if !isPresented {
                    browseState.closeLightbox()
                }
            }
        )
    }

    // MARK: - Preview fetching

    private func fetchAllPreviews() async {
        guard let client = appState.client else { return }
        await withTaskGroup(of: (String, [String]).self) { group in
            for lib in appState.libraries {
                guard libraryPreviews[lib.libraryId] == nil else { continue }
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

    private static func fetchPreviewIds(
        client: APIClient, libraryId: String
    ) async -> [String] {
        do {
            let response: QueryResponse = try await client.get(
                "/v1/query",
                queryItems: [
                    URLQueryItem(name: "library_id", value: libraryId),
                    URLQueryItem(name: "page_size", value: "4"),
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
