import SwiftUI
import LumiverbKit

/// View mode for the content area.
enum BrowseMode: Equatable {
    case library         // browsing a library's assets
    case search          // showing search results
    case similar(String) // showing similar assets to the given asset ID
}

/// Observable state for the browse window.
@MainActor
class BrowseState: ObservableObject {
    let appState: AppState

    // MARK: - Library selection

    @Published var selectedLibraryId: String? {
        didSet {
            if selectedLibraryId != oldValue {
                resetAndLoad()
            }
        }
    }

    // MARK: - Directory tree

    @Published var directories: [DirectoryNode] = []
    @Published var expandedPaths: Set<String> = []
    @Published var childDirectories: [String: [DirectoryNode]] = [:]
    @Published var selectedPath: String? {
        didSet {
            if selectedPath != oldValue {
                reloadAssets()
            }
        }
    }

    // MARK: - Asset grid

    @Published var assets: [AssetPageItem] = []
    @Published var isLoadingAssets = false
    @Published var hasMoreAssets = true
    private var nextCursor: String?

    // MARK: - Search

    @Published var searchQuery = ""
    @Published var searchResults: [SearchHit] = []
    @Published var searchTotal = 0
    @Published var isSearching = false

    // MARK: - Similarity

    @Published var similarResults: [SimilarHit] = []
    @Published var similarTotal = 0
    @Published var isFindingSimilar = false
    @Published var similarSourceId: String?

    // MARK: - Lightbox

    @Published var selectedAssetId: String?
    @Published var assetDetail: AssetDetail?
    @Published var isLoadingDetail = false

    // MARK: - Mode

    @Published var mode: BrowseMode = .library

    // MARK: - Error

    @Published var error: String?

    // MARK: - Grid selection (for keyboard nav)

    @Published var focusedIndex: Int = 0

    init(appState: AppState) {
        self.appState = appState
    }

    var client: APIClient? { appState.client }

    /// The list of asset IDs currently displayed (varies by mode).
    var displayedAssetIds: [String] {
        switch mode {
        case .library:
            return assets.map(\.assetId)
        case .search:
            return searchResults.map(\.assetId)
        case .similar:
            return similarResults.map(\.assetId)
        }
    }

    /// Total count of items in current view.
    var displayedCount: Int { displayedAssetIds.count }

    // MARK: - Load assets

    func resetAndLoad() {
        assets = []
        nextCursor = nil
        hasMoreAssets = true
        error = nil
        mode = .library
        searchQuery = ""
        searchResults = []
        similarResults = []
        selectedAssetId = nil
        assetDetail = nil
        focusedIndex = 0
        selectedPath = nil
        directories = []
        expandedPaths = []
        childDirectories = [:]
        Task {
            await loadRootDirectories()
            await loadNextPage()
        }
    }

    /// Reload assets when path filter changes (without resetting directory tree).
    private func reloadAssets() {
        assets = []
        nextCursor = nil
        hasMoreAssets = true
        focusedIndex = 0
        Task { await loadNextPage() }
    }

    func loadNextPage() async {
        guard let client, let libraryId = selectedLibraryId else { return }
        guard !isLoadingAssets, hasMoreAssets else { return }

        isLoadingAssets = true
        error = nil

        do {
            var query: [String: String] = [
                "library_id": libraryId,
                "limit": "100",
                "sort": "taken_at",
                "dir": "desc",
            ]
            if let cursor = nextCursor {
                query["after"] = cursor
            }
            if let path = selectedPath {
                query["path_prefix"] = path
            }

            let response: AssetPageResponse = try await client.get(
                "/v1/assets/page", query: query
            )
            assets.append(contentsOf: response.items)
            nextCursor = response.nextCursor
            hasMoreAssets = response.nextCursor != nil
        } catch {
            self.error = "Failed to load assets: \(error)"
        }

        isLoadingAssets = false
    }

    // MARK: - Asset detail

    func loadAssetDetail(assetId: String) async {
        guard let client else { return }

        selectedAssetId = assetId
        isLoadingDetail = true
        assetDetail = nil

        do {
            let detail: AssetDetail = try await client.get("/v1/assets/\(assetId)")
            assetDetail = detail
        } catch {
            self.error = "Failed to load asset: \(error)"
        }

        isLoadingDetail = false
    }

    func closeLightbox() {
        selectedAssetId = nil
        assetDetail = nil
    }

    // MARK: - Search

    func performSearch() async {
        let query = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty, let client else { return }

        mode = .search
        isSearching = true
        searchResults = []
        searchTotal = 0
        error = nil

        do {
            var params: [String: String] = [
                "q": query,
                "limit": "100",
            ]
            if let libraryId = selectedLibraryId {
                params["library_id"] = libraryId
            }

            let response: SearchResponse = try await client.get(
                "/v1/search", query: params
            )
            searchResults = response.hits
            searchTotal = response.total
        } catch {
            self.error = "Search failed: \(error)"
        }

        isSearching = false
    }

    func clearSearch() {
        searchQuery = ""
        searchResults = []
        searchTotal = 0
        mode = .library
    }

    // MARK: - Similarity

    func findSimilar(assetId: String) async {
        guard let client, let libraryId = selectedLibraryId else { return }

        mode = .similar(assetId)
        similarSourceId = assetId
        isFindingSimilar = true
        similarResults = []
        similarTotal = 0
        error = nil
        closeLightbox()

        do {
            let params: [String: String] = [
                "asset_id": assetId,
                "library_id": libraryId,
                "limit": "50",
            ]

            let response: SimilarityResponse = try await client.get(
                "/v1/similar", query: params
            )
            similarResults = response.hits
            similarTotal = response.total
        } catch {
            self.error = "Similarity search failed: \(error)"
        }

        isFindingSimilar = false
    }

    // MARK: - Directory tree

    func loadRootDirectories() async {
        guard let client, let libraryId = selectedLibraryId else { return }
        do {
            let nodes: [DirectoryNode] = try await client.get(
                "/v1/libraries/\(libraryId)/directories"
            )
            directories = nodes
        } catch {
            // Non-fatal — grid still works without tree
        }
    }

    func loadChildDirectories(parentPath: String) async {
        guard let client, let libraryId = selectedLibraryId else { return }
        do {
            let nodes: [DirectoryNode] = try await client.get(
                "/v1/libraries/\(libraryId)/directories",
                query: ["parent": parentPath]
            )
            childDirectories[parentPath] = nodes
        } catch {
            // Non-fatal
        }
    }

    func toggleExpanded(path: String) {
        if expandedPaths.contains(path) {
            expandedPaths.remove(path)
        } else {
            expandedPaths.insert(path)
            if childDirectories[path] == nil {
                Task { await loadChildDirectories(parentPath: path) }
            }
        }
    }

    func selectPath(_ path: String?) {
        selectedPath = path
    }

    // MARK: - Keyboard navigation

    func navigateLightbox(direction: Int) {
        guard let currentId = selectedAssetId else { return }
        let ids = displayedAssetIds
        guard let currentIndex = ids.firstIndex(of: currentId) else { return }
        let newIndex = currentIndex + direction
        guard ids.indices.contains(newIndex) else { return }
        focusedIndex = newIndex
        Task { await loadAssetDetail(assetId: ids[newIndex]) }
    }

    func openFocusedAsset() {
        let ids = displayedAssetIds
        guard ids.indices.contains(focusedIndex) else { return }
        Task { await loadAssetDetail(assetId: ids[focusedIndex]) }
    }

    func moveFocus(direction: Int, columns: Int = 1) {
        let ids = displayedAssetIds
        let newIndex = focusedIndex + direction * columns
        if ids.indices.contains(newIndex) {
            focusedIndex = newIndex
        }
    }
}
