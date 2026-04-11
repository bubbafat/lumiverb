import SwiftUI
import LumiverbKit

/// Libraries tab content. When no library is selected, shows a visual
/// picker grid with cover images for each library. After selection,
/// shows the asset grid with a back button to return to the picker.
struct LibraryBrowseView: View {
    @ObservedObject var appState: iOSAppState
    @ObservedObject var browseState: BrowseState

    var body: some View {
        Group {
            if browseState.selectedLibraryId != nil {
                libraryDetail
            } else {
                libraryPickerGrid
            }
        }
        .fullScreenCover(isPresented: lightboxBinding) {
            LightboxView(browseState: browseState, client: appState.client)
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
                    columns: [GridItem(.adaptive(minimum: 160), spacing: 12)],
                    spacing: 12
                ) {
                    ForEach(appState.libraries) { lib in
                        Button {
                            browseState.selectedLibraryId = lib.libraryId
                        } label: {
                            LibraryCardView(
                                library: lib,
                                client: appState.client
                            )
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding()
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

    // MARK: - Last-opened library restore

    private func restoreLastOpenedLibrary() {
        guard browseState.selectedLibraryId == nil else { return }
        guard let lastId = UserDefaults.standard.string(forKey: "lastLibraryId") else { return }
        guard appState.libraries.contains(where: { $0.libraryId == lastId }) else { return }
        browseState.selectedLibraryId = lastId
    }
}
