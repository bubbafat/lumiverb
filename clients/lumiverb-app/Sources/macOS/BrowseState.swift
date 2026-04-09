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
                UserDefaults.standard.set(selectedLibraryId, forKey: "lastLibraryId")
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

    // MARK: - Filters

    @Published var filters = BrowseFilter() {
        didSet {
            if filters != oldValue {
                // Sort-only changes don't need to reset person state
                reloadAssets()
            }
        }
    }

    // MARK: - Person search suggestions

    @Published var personSuggestions: [PersonItem] = []
    private var personSearchTask: Task<Void, Never>?

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

    /// When set, the lightbox prev/next chevrons (and arrow-key navigation)
    /// iterate this list instead of `displayedAssetIds`'s normal mode-based
    /// list. Used by the People view's `PersonDetailView` so opening a
    /// face lets the user step through that person's other photos rather
    /// than the (likely empty) library grid behind them. Cleared by
    /// `closeLightbox()`.
    @Published var displayedAssetIdsOverride: [String]?

    /// Parallel to ``displayedAssetIdsOverride``: the face_id to highlight
    /// on the asset at the same index in that list. Used by the cluster
    /// review so navigating left/right in the lightbox doesn't just walk
    /// asset to asset — it walks *face to face*, with the cluster's face
    /// for each asset highlighted in turn. Without this, only the first
    /// face the user clicked into got the red border + auto-open popover;
    /// every subsequent navigation arrived at a photo with no clickable
    /// hit target and the user had to manually find the right face.
    ///
    /// `nil` outside the cluster review handoff. Cleared by `closeLightbox()`.
    @Published var displayedFaceIdsOverride: [String]?

    /// When set, the lightbox should:
    /// 1. Force the face overlay on (as if the user pressed `d`)
    /// 2. Auto-open the assign popover on this specific face
    ///
    /// Used by the cluster review (`ClusterCardView`) to give the user
    /// a per-face escape hatch out of "name the whole cluster" — they
    /// can drill into a single face, assign it, and the cluster cache
    /// recomputes on the next refresh leaving the rest of the cluster
    /// behind. Without this, the only path to per-face assignment was
    /// to dismiss the cluster and tag faces from normal browse, which
    /// is much slower.
    ///
    /// Re-set by `navigateLightbox` from `displayedFaceIdsOverride` when
    /// the user steps to the next/prev cluster asset. Cleared by
    /// `closeLightbox()`.
    @Published var pendingHighlightFaceId: String?

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

    /// Root path of the currently selected library, if any.
    var selectedLibraryRootPath: String? {
        guard let id = selectedLibraryId else { return nil }
        return appState.libraries.first { $0.libraryId == id }?.rootPath
    }

    /// The list of asset IDs currently displayed (varies by mode). When
    /// `displayedAssetIdsOverride` is set, that list wins — the People
    /// view installs it before opening the lightbox so prev/next walks
    /// the person's faces rather than the underlying library mode's
    /// (potentially empty) list.
    var displayedAssetIds: [String] {
        if let displayedAssetIdsOverride { return displayedAssetIdsOverride }
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
        filters = BrowseFilter()
        personSuggestions = []
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
            var query = filters.queryParams
            query["library_id"] = libraryId
            query["limit"] = "100"
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
        // Clear any People-view installed prev/next override so the next
        // lightbox open from the library grid uses the normal mode list.
        displayedAssetIdsOverride = nil
        displayedFaceIdsOverride = nil
        // Clear any cluster-review face highlight so the next lightbox
        // open doesn't pop a stale popover.
        pendingHighlightFaceId = nil
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
            // Pass the embedding model so the server looks up the right vectors
            let embeddingModelId = CLIPProvider.isAvailable ? CLIPProvider.modelId : FeaturePrintProvider.modelId
            let embeddingModelVersion = CLIPProvider.isAvailable ? CLIPProvider.modelVersion : FeaturePrintProvider.modelVersion

            let params: [String: String] = [
                "asset_id": assetId,
                "library_id": libraryId,
                "limit": "50",
                "model_id": embeddingModelId,
                "model_version": embeddingModelVersion,
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

    // MARK: - Re-enrichment

    @Published var isReEnriching = false
    @Published var reEnrichPhase = ""
    @Published var reEnrichTotal = 0
    @Published var reEnrichProcessed = 0
    @Published var reEnrichSkipped: [String] = []

    private var reEnrichRunner: ReEnrichmentRunner?
    private var reEnrichPollTask: Task<Void, Never>?

    /// Re-enrich assets in the current library, optionally scoped to a path prefix.
    func reEnrich(operations: Set<EnrichmentOperation>, pathPrefix: String? = nil) {
        guard let client, let libraryId = selectedLibraryId, !isReEnriching else { return }

        Task {
            isReEnriching = true
            reEnrichPhase = "fetching assets"
            reEnrichProcessed = 0
            reEnrichTotal = 0

            let assets = await fetchAllAssets(libraryId: libraryId, pathPrefix: pathPrefix)
            guard !assets.isEmpty else {
                isReEnriching = false
                return
            }

            let runner = ReEnrichmentRunner(
                client: client,
                libraryId: libraryId,
                libraryRootPath: selectedLibraryRootPath,
                visionApiUrl: appState.resolvedVisionApiUrl,
                visionApiKey: appState.resolvedVisionApiKey,
                visionModelId: appState.resolvedVisionModelId,
                whisperModelSize: appState.whisperModelSize,
                whisperLanguage: appState.whisperLanguage,
                whisperBinaryPath: appState.whisperBinaryPath
            )
            reEnrichRunner = runner
            startReEnrichPolling(runner: runner)

            let result = await runner.run(assets: assets, operations: operations)

            stopReEnrichPolling()
            reEnrichRunner = nil
            isReEnriching = false
            reEnrichPhase = ""
            reEnrichSkipped = result.skipped

            // Refresh the grid and detail if open
            reloadAssets()
            if let assetId = selectedAssetId {
                await loadAssetDetail(assetId: assetId)
            }
        }
    }

    /// Re-enrich a single asset (from lightbox actions).
    func reEnrichAsset(assetId: String, operations: Set<EnrichmentOperation>) {
        guard let client, let libraryId = selectedLibraryId, !isReEnriching else { return }

        // Find the asset in current display or fetch it
        let asset = assets.first { $0.assetId == assetId }
        guard let asset else { return }

        Task {
            isReEnriching = true
            reEnrichPhase = ""
            reEnrichProcessed = 0
            reEnrichTotal = 1

            let runner = ReEnrichmentRunner(
                client: client,
                libraryId: libraryId,
                libraryRootPath: selectedLibraryRootPath,
                visionApiUrl: appState.resolvedVisionApiUrl,
                visionApiKey: appState.resolvedVisionApiKey,
                visionModelId: appState.resolvedVisionModelId,
                whisperModelSize: appState.whisperModelSize,
                whisperLanguage: appState.whisperLanguage,
                whisperBinaryPath: appState.whisperBinaryPath
            )
            reEnrichRunner = runner
            startReEnrichPolling(runner: runner)

            let result = await runner.run(assets: [asset], operations: operations)

            stopReEnrichPolling()
            reEnrichRunner = nil
            isReEnriching = false
            reEnrichPhase = ""
            reEnrichSkipped = result.skipped

            // Refresh the lightbox detail
            await loadAssetDetail(assetId: assetId)
        }
    }

    func cancelReEnrich() {
        Task {
            await reEnrichRunner?.cancel()
        }
    }

    /// Fetch all assets for a library/path (paginated).
    private func fetchAllAssets(libraryId: String, pathPrefix: String?) async -> [AssetPageItem] {
        guard let client else { return [] }
        var all: [AssetPageItem] = []
        var cursor: String?

        repeat {
            var query: [String: String] = [
                "library_id": libraryId,
                "limit": "500",
                "sort": "asset_id",
                "dir": "asc",
            ]
            if let cursor { query["after"] = cursor }
            if let pathPrefix { query["path_prefix"] = pathPrefix }

            do {
                let response: AssetPageResponse = try await client.get(
                    "/v1/assets/page", query: query
                )
                all.append(contentsOf: response.items)
                cursor = response.nextCursor
                reEnrichTotal = all.count
            } catch {
                self.error = "Failed to fetch assets: \(error)"
                break
            }
        } while cursor != nil

        return all
    }

    private func startReEnrichPolling(runner: ReEnrichmentRunner) {
        reEnrichPollTask?.cancel()
        reEnrichPollTask = Task {
            while !Task.isCancelled {
                let total = await runner.totalItems
                let processed = await runner.processedItems
                let phase = await runner.phase
                self.reEnrichTotal = total
                self.reEnrichProcessed = processed
                self.reEnrichPhase = phase
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
    }

    private func stopReEnrichPolling() {
        reEnrichPollTask?.cancel()
        reEnrichPollTask = nil
    }

    // MARK: - Person search

    /// Search for people matching the query (debounced by caller).
    func searchPeople(query: String) async {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let client else {
            personSuggestions = []
            return
        }

        do {
            let response: PersonListResponse = try await client.get(
                "/v1/people", query: ["q": trimmed, "limit": "10"]
            )
            personSuggestions = response.items
        } catch {
            personSuggestions = []
        }
    }

    /// Filter the grid to show only a specific person's assets.
    func filterByPerson(_ person: PersonItem) {
        filters.personId = person.personId
        filters.personDisplayName = person.displayName
        personSuggestions = []
        searchQuery = ""
        mode = .library
    }

    /// Clear the person filter.
    func clearPersonFilter() {
        filters.personId = nil
        filters.personDisplayName = nil
    }

    /// Debounced person search triggered by search text changes.
    func debouncedPersonSearch(query: String) {
        personSearchTask?.cancel()
        personSearchTask = Task {
            try? await Task.sleep(for: .milliseconds(400))
            guard !Task.isCancelled else { return }
            await searchPeople(query: query)
        }
    }

    // MARK: - Keyboard navigation

    func navigateLightbox(direction: Int) {
        guard let currentId = selectedAssetId else { return }
        let ids = displayedAssetIds
        guard let currentIndex = ids.firstIndex(of: currentId) else { return }
        let newIndex = currentIndex + direction
        guard ids.indices.contains(newIndex) else { return }
        focusedIndex = newIndex
        // Chain the cluster-review face highlight: if the cluster
        // review installed a parallel face_id list, look up the face
        // id at the new index and arm `pendingHighlightFaceId` so the
        // lightbox auto-opens the assign popover on it. Without this,
        // navigation would walk asset-to-asset without surfacing the
        // cluster's actual face on each photo.
        if let faceIds = displayedFaceIdsOverride,
           faceIds.indices.contains(newIndex) {
            pendingHighlightFaceId = faceIds[newIndex]
        }
        Task { await loadAssetDetail(assetId: ids[newIndex]) }
    }

    /// Has a next asset to navigate to. Used by the lightbox face
    /// auto-advance after tagging a highlighted face — when the user
    /// finishes the last cluster face we close instead of no-op'ing.
    var hasNextAsset: Bool {
        guard let currentId = selectedAssetId else { return false }
        let ids = displayedAssetIds
        guard let currentIndex = ids.firstIndex(of: currentId) else { return false }
        return currentIndex + 1 < ids.count
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
