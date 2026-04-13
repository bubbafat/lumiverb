import Combine
import SwiftUI

/// View mode for the content area.
public enum BrowseMode: Equatable, Sendable {
    case library         // browsing a library's assets
    case search          // showing search results
    case similar(String) // showing similar assets to the given asset ID
}

/// Observable state for the browse window.
@MainActor
public class BrowseState: ObservableObject {
    public let appContext: any BrowseAppContext

    // MARK: - Library selection

    /// ⚠️  Do NOT put a `didSet` that does heavy work on this property.
    ///
    /// `List(selection: $browseState.selectedLibraryId)` invokes this
    /// setter synchronously from inside SwiftUI's click-dispatch code
    /// path. Anything the setter runs blocks the List from dequeueing
    /// the *next* click — which is what caused the "I clicked but
    /// nothing happened" UI lock we used to have here.
    ///
    /// The persistence write + reset cascade is now triggered by a
    /// `.onChange(of: browseState.selectedLibraryId)` handler in
    /// `BrowseWindow`, which runs one tick later and off the binding
    /// write code path. See `BrowseState.handleSelectedLibraryChange()`.
    @Published public var selectedLibraryId: String?

    /// One-shot scroll command sent from keyboard handlers in
    /// `BrowseWindow` to the active grid view. The grid view observes
    /// this via `.onChange` and forwards it to the underlying
    /// `NSScrollView` (introspected via `NSScrollViewIntrospector`).
    ///
    /// AppKit handles the actual scrolling with native pageUp/pageDown
    /// semantics — much simpler and more reliable than the SwiftUI
    /// `ScrollViewProxy.scrollTo` route, which has fatal lazy-render
    /// gotchas (verified empirically: backward scrolls into disposed
    /// `LazyVStack` cells silently no-op).
    @Published public var pendingScrollCommand: ScrollCommandToken?

    public func sendScrollCommand(_ command: ScrollCommand) {
        pendingScrollCommand = ScrollCommandToken(command: command)
    }

    /// True while `resetAndLoad()` is in the middle of clearing state,
    /// so that the didSets on `selectedPath` and `filters` below don't
    /// spawn their own `reloadAssets()` Tasks — the reset already fires
    /// a single `loadNextPage()` once clearing is done.
    private var isResetting = false

    // MARK: - Directory tree

    @Published public var directories: [DirectoryNode] = []
    @Published public var expandedPaths: Set<String> = []
    @Published public var childDirectories: [String: [DirectoryNode]] = [:]
    @Published public var selectedPath: String? {
        didSet {
            if selectedPath != oldValue && !isResetting {
                reloadAssets()
            }
        }
    }

    // MARK: - Filters

    @Published public var filters = BrowseFilter() {
        didSet {
            if filters != oldValue && !isResetting {
                // Dispatch the reload based on the current mode. Without
                // this, toggling the media-type picker (or any filter)
                // while looking at search results would silently kick
                // off a library page reload — the search results would
                // still show the old filter state because performSearch
                // never re-ran. Same for similarity mode.
                switch mode {
                case .library:
                    reloadAssets()
                case .search:
                    Task { await executeSearch() }
                case .similar(let sourceId):
                    Task { await findSimilar(assetId: sourceId) }
                }
            }
        }
    }

    // MARK: - Person search suggestions

    @Published public var personSuggestions: [PersonItem] = []
    private var personSearchTask: Task<Void, Never>?

    // MARK: - Asset grid

    @Published public var assets: [AssetPageItem] = []
    @Published public var isLoadingAssets = false
    @Published public var hasMoreAssets = true
    private var nextCursor: String?

    /// True from the moment the user clicks a new library until the
    /// first page of that library's assets has come back (or failed).
    /// Distinct from `isLoadingAssets` (which tracks any page load,
    /// including infinite-scroll) — this is specifically the "context
    /// switch" phase. The content area shows a full-size overlay so
    /// the user gets immediate feedback that the click was registered
    /// even if the rest of the main thread is momentarily busy doing
    /// reconciliation work.
    @Published public var isChangingLibrary = false

    /// Error raised by the first-page load during a library switch.
    /// Non-nil while the overlay is in its error state; cleared on
    /// retry or when the user picks a different library.
    @Published public var libraryChangeError: String?

    /// Previous library id, captured before a switch, so "Back" in the
    /// error overlay can revert to a known-good library rather than
    /// stranding the user on a dead selection.
    private var previousLibraryId: String?

    /// Timeout for the first-page load during a library switch. Shorter
    /// than URLSession's 60s default because the user needs a faster
    /// "server is unreachable" signal than a browse session normally
    /// demands — 60 seconds of overlay on an offline server is awful.
    private let firstPageTimeoutSeconds: TimeInterval = 10

    /// In-flight asset load Task. Cancelled on library change so the new
    /// library's load isn't blocked by the old library's slow network
    /// request. Without this, clicking a new library during a load would
    /// hit the `!isLoadingAssets` guard in `loadNextPage` and silently
    /// no-op — the user sees a blank grid until the *previous* library's
    /// request returns (the "20+ seconds locked" bug).
    private var currentLoadTask: Task<Void, Never>?

    // MARK: - Search

    @Published public var searchQuery = ""
    /// The search query that was committed (submitted). Survives the
    /// searchable field clearing on ESC. The chiclet and API calls use
    /// this, not searchQuery (which is the live field text).
    @Published public var committedSearchQuery = ""
    @Published public var searchResults: [SearchHit] = []
    @Published public var searchTotal = 0
    @Published public var isSearching = false

    // MARK: - Similarity

    @Published public var similarResults: [SimilarHit] = []
    @Published public var similarTotal = 0
    @Published public var isFindingSimilar = false
    @Published public var similarSourceId: String?

    // MARK: - Selection

    /// Multi-select set of asset IDs. Empty when not in select mode.
    @Published public var selectedAssetIds: Set<String> = []
    /// When true, taps toggle selection instead of opening the lightbox.
    @Published public var isSelecting = false
    /// The last individually toggled asset ID, used for shift-click range select.
    public var lastToggledAssetId: String?
    /// Date keys (YYYY-MM-DD) with active group selections. Assets arriving
    /// after a date-select click are auto-added if their date is in this set.
    private var selectedDateKeys: Set<String> = []

    /// Toggle a single asset's selection. Enters select mode if not already.
    public func toggleSelection(assetId: String) {
        if !isSelecting { isSelecting = true }
        if selectedAssetIds.contains(assetId) {
            selectedAssetIds.remove(assetId)
        } else {
            selectedAssetIds.insert(assetId)
        }
        lastToggledAssetId = assetId
        if selectedAssetIds.isEmpty { isSelecting = false }
    }

    /// Toggle all assets in a date group. Tracks the date key so that
    /// assets arriving later (from page completion) are auto-selected.
    public func selectGroup(_ assetIds: [String], dateISO: String?) {
        if !isSelecting { isSelecting = true }
        let groupSet = Set(assetIds)
        if groupSet.isSubset(of: selectedAssetIds) {
            // Deselect
            selectedAssetIds.subtract(groupSet)
            if let key = dateISO { selectedDateKeys.remove(key) }
        } else {
            // Select
            selectedAssetIds.formUnion(groupSet)
            if let key = dateISO { selectedDateKeys.insert(key) }
        }
        if selectedAssetIds.isEmpty {
            isSelecting = false
            selectedDateKeys.removeAll()
        }
    }

    /// Called after new assets are appended. Auto-selects any that belong
    /// to a date group the user has already selected.
    private func autoSelectNewAssets(_ newAssets: [AssetPageItem]) {
        guard !selectedDateKeys.isEmpty else { return }
        for asset in newAssets {
            if let key = Self.dateKey(for: asset), selectedDateKeys.contains(key) {
                selectedAssetIds.insert(asset.assetId)
            }
        }
    }

    /// Select all currently displayed assets.
    public func selectAll() {
        isSelecting = true
        selectedAssetIds = Set(assets.map(\.assetId))
    }

    /// Exit select mode and clear the selection.
    public func clearSelection() {
        selectedAssetIds.removeAll()
        selectedDateKeys.removeAll()
        isSelecting = false
        lastToggledAssetId = nil
    }

    /// Range-select from `lastToggledAssetId` to `targetId` within the
    /// current asset list. Used for shift-click.
    public func rangeSelect(to targetId: String) {
        guard let fromId = lastToggledAssetId else {
            toggleSelection(assetId: targetId)
            return
        }
        let ids = assets.map(\.assetId)
        guard let fromIdx = ids.firstIndex(of: fromId),
              let toIdx = ids.firstIndex(of: targetId) else {
            toggleSelection(assetId: targetId)
            return
        }
        let range = min(fromIdx, toIdx)...max(fromIdx, toIdx)
        if !isSelecting { isSelecting = true }
        for i in range {
            selectedAssetIds.insert(ids[i])
        }
    }

    // MARK: - Lightbox

    @Published public var selectedAssetId: String?
    @Published public var assetDetail: AssetDetail?
    @Published public var isLoadingDetail = false
    /// Current rating for the asset shown in the lightbox. Fetched on open,
    /// optimistically updated on mutation.
    @Published public var currentRating: Rating = .empty

    /// When set, the lightbox prev/next chevrons (and arrow-key navigation)
    /// iterate this list instead of `displayedAssetIds`'s normal mode-based
    /// list. Used by the People view's `PersonDetailView` so opening a
    /// face lets the user step through that person's other photos rather
    /// than the (likely empty) library grid behind them. Cleared by
    /// `closeLightbox()`.
    @Published public var displayedAssetIdsOverride: [String]?

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
    @Published public var displayedFaceIdsOverride: [String]?

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
    @Published public var pendingHighlightFaceId: String?

    // MARK: - Mode

    @Published public var mode: BrowseMode = .library

    // MARK: - Error

    @Published public var error: String?

    // MARK: - Grid selection (for keyboard nav)

    @Published public var focusedIndex: Int = 0

    /// Mirror of `appState.whisperEnabled`. SwiftUI views that observe
    /// `BrowseState` (LightboxView, LibrarySidebar, DirectoryTreeView) need
    /// to react to whisper enable/disable changes when they host a
    /// `ReEnrichMenu`. They don't observe `appState` directly, so reads
    /// via `browseState.appState.whisperEnabled` would never trigger
    /// re-renders — the menu would stay greyed out forever after the
    /// initial paint. This published mirror is fed by a Combine sink in
    /// `init` so any change to the underlying value flows through to the
    /// observers automatically.
    @Published public var whisperEnabled: Bool = false

    private var cancellables: Set<AnyCancellable> = []

    public init(appContext: any BrowseAppContext) {
        self.appContext = appContext
        self.whisperEnabled = appContext.whisperEnabled
        appContext.whisperEnabledPublisher
            .receive(on: DispatchQueue.main)
            .sink { [weak self] newValue in
                self?.whisperEnabled = newValue
            }
            .store(in: &cancellables)
    }

    public var client: APIClient? { appContext.client }

    /// Root path of the currently selected library, if any.
    public var selectedLibraryRootPath: String? {
        guard let id = selectedLibraryId else { return nil }
        return appContext.libraries.first { $0.libraryId == id }?.rootPath
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

    /// Called by BrowseWindow on `.onChange(of: selectedLibraryId)` — NOT
    /// from a didSet, so that the List's click dispatch code path is not
    /// blocked on reset work.
    public func handleSelectedLibraryChange() {
        UserDefaults.standard.set(selectedLibraryId, forKey: "lastLibraryId")
        // Capture the outgoing library id BEFORE clearing error state so
        // the overlay's "Back" button can restore a known-good selection
        // if the new library's first-page load fails.
        if libraryChangeError == nil {
            previousLibraryId = assets.isEmpty ? previousLibraryId : selectedLibraryId
        }
        libraryChangeError = nil
        // Set FIRST, before resetAndLoad runs its state cascade. Without
        // this the overlay appears only after the cascade's render pass,
        // which defeats the purpose of "show something immediately".
        isChangingLibrary = selectedLibraryId != nil
        resetAndLoad()
    }

    /// Retry the current library's first-page load after a timeout or
    /// network failure. Called from the overlay's Retry button.
    public func retryLibraryChange() {
        guard selectedLibraryId != nil else { return }
        libraryChangeError = nil
        isChangingLibrary = true
        resetAndLoad()
    }

    /// Revert to the previous library after a failed switch. Called from
    /// the overlay's Back button. If there's no previous library, clears
    /// the selection entirely so the user sees the "Select a library"
    /// empty state instead of a stuck error.
    public func revertLibraryChange() {
        libraryChangeError = nil
        isChangingLibrary = false
        let target = previousLibraryId
        previousLibraryId = nil
        // Cancel any in-flight load from the failed switch so it doesn't
        // land late and clobber the previous library's state.
        currentLoadTask?.cancel()
        currentLoadTask = nil
        selectedLibraryId = target
        // `.onChange(of: selectedLibraryId)` in BrowseWindow will fire
        // `handleSelectedLibraryChange()` if this is a real change —
        // reloading the previous library fresh. If target == current
        // (user already reverted via another path) nothing happens.
    }

    func resetAndLoad() {
        // Cancel the previous load. Without this, a slow network request
        // from the old library holds `isLoadingAssets = true` and blocks
        // the new library's load behind the guard in `loadNextPage`,
        // manifesting as a 20+ second "UI lock" on library switch. The
        // cancelled task's network call throws `CancellationError`;
        // `loadNextPage`'s libraryId-mismatch check below also prevents
        // stale writebacks if the old task's await returns after the
        // reset has already populated new state.
        currentLoadTask?.cancel()
        currentLoadTask = nil

        // Guard so selectedPath/filters didSets don't fire their own
        // reloadAssets() Tasks during the clear — we fire exactly one
        // `loadNextPage()` at the end of the reset.
        isResetting = true
        assets = []
        nextCursor = nil
        hasMoreAssets = true
        // Reset explicitly because the cancelled task may not have
        // reached its `isLoadingAssets = false` epilogue yet.
        isLoadingAssets = false
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
        isResetting = false

        currentLoadTask = Task { [weak self] in
            guard let self else { return }
            await self.loadRootDirectories()
            await self.loadNextPage()
        }
    }

    /// Async pull-to-refresh entry point. Re-fetches the first page
    /// without going through `loadNextPage` so iOS's `.refreshable`
    /// awaits the actual network call. We can't use `loadNextPage`
    /// here because clearing `assets = []` causes `MediaGridView`'s
    /// infinite-scroll sentinel to re-fire `loadNextPage` on its
    /// own, racing against ours and winning the `isLoadingAssets`
    /// guard. Doing the fetch inline and atomically swapping `assets`
    /// avoids the race entirely.
    public func refreshCurrent() async {
        guard let client, let libraryId = selectedLibraryId else { return }
        // Cancel any in-flight load so its writeback doesn't race ours.
        currentLoadTask?.cancel()
        currentLoadTask = nil
        isLoadingAssets = true
        defer {
            if selectedLibraryId == libraryId {
                isLoadingAssets = false
            }
        }
        do {
            let items = filters.queryItems(
                libraryId: libraryId,
                pathPrefix: selectedPath,
                searchQuery: mode == .search ? committedSearchQuery : nil,
                after: nil,
                limit: 100
            )
            let response: QueryResponse = try await client.get(
                "/v1/query", queryItems: items
            )
            guard selectedLibraryId == libraryId else { return }
            let pageItems = response.items.map { $0.toPageItem() }
            // Atomic swap — never empty the array, so the grid sentinel
            // doesn't re-fire mid-refresh.
            assets = pageItems
            nextCursor = response.nextCursor
            hasMoreAssets = response.nextCursor != nil
            error = nil
        } catch {
            if (error as NSError).code == NSURLErrorCancelled { return }
            self.error = "Failed to refresh: \(error)"
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

    public func loadNextPage() async {
        guard let client, let libraryId = selectedLibraryId else { return }
        guard !isLoadingAssets, hasMoreAssets else { return }

        // Track whether this is the FIRST page so we know when to clear
        // `isChangingLibrary`. Infinite-scroll pages shouldn't touch it.
        let isFirstPage = nextCursor == nil

        isLoadingAssets = true
        error = nil

        do {
            let items = filters.queryItems(
                libraryId: libraryId,
                pathPrefix: selectedPath,
                searchQuery: mode == .search ? committedSearchQuery : nil,
                after: nextCursor,
                limit: 100
            )

            // First page of a library switch races against a 10s timeout
            // so the overlay can surface a sensible error instead of
            // sitting on URLSession's 60s default. Infinite-scroll pages
            // use the vanilla client call — no need to time those out.
            let response: QueryResponse
            if isFirstPage {
                let queryForRequest = items
                response = try await Self.loadWithTimeout(
                    seconds: firstPageTimeoutSeconds,
                    operation: { try await client.get("/v1/query", queryItems: queryForRequest) }
                )
            } else {
                response = try await client.get("/v1/query", queryItems: items)
            }

            // Between the await and here, the user may have switched
            // libraries. If they did, the response is for the OLD library
            // and must not be written back — otherwise we'd pollute the
            // new library's grid with stale assets and corrupt its cursor.
            // The cancelled-Task path from `resetAndLoad` usually throws
            // before we get here, but URLSession occasionally completes
            // fast enough that cancellation loses the race.
            guard selectedLibraryId == libraryId else { return }

            let pageItems = response.items.map { $0.toPageItem() }
            assets.append(contentsOf: pageItems)
            autoSelectNewAssets(pageItems)
            nextCursor = response.nextCursor
            hasMoreAssets = response.nextCursor != nil

            // Complete the last date group: if we have more pages and the
            // last loaded asset's date matches the tail, keep loading so
            // every visible date header covers 100% of its assets. This
            // guarantees "select date" selects everything for that day.
            if hasMoreAssets {
                await completeLastDateGroup(client: client, libraryId: libraryId, isFirstPage: isFirstPage)
            }
        } catch is CancellationError {
            // Library change cancelled this load — nothing to report.
            return
        } catch {
            // Swallow errors that surface from cancelled URLSession calls
            // (`NSURLErrorCancelled`) the same way as CancellationError.
            if (error as NSError).code == NSURLErrorCancelled { return }
            // Also guard error writeback if the library changed mid-flight.
            if selectedLibraryId != libraryId { return }

            // First-page failures populate the overlay's error state.
            // Subsequent-page failures go to the regular inline error.
            if isFirstPage {
                libraryChangeError = Self.describeLibraryChangeError(error)
            } else {
                self.error = "Failed to load assets: \(error)"
            }
        }

        // `isLoadingAssets` is reset here for the still-current library.
        // If the library changed mid-flight, `resetAndLoad` already reset
        // it before spawning the new task — don't clobber that.
        if selectedLibraryId == libraryId {
            isLoadingAssets = false
            // Clear the "changing library" overlay only once the first
            // page's outcome is known, AND only when there's no error —
            // the overlay stays up in error mode until the user retries
            // or backs out. Infinite-scroll pages don't touch the flag.
            if isFirstPage && libraryChangeError == nil {
                isChangingLibrary = false
            }
        }
    }

    // MARK: - Library-change helpers

    /// Race an async operation against a timeout. Used for the first-page
    /// load so the overlay can surface a "server unreachable" error in
    /// seconds instead of waiting on URLSession's 60s default. Captured
    /// as a `@Sendable` closure so the task-group children are isolated
    /// from the @MainActor caller.
    private static func loadWithTimeout<T: Sendable>(
        seconds: TimeInterval,
        operation: @escaping @Sendable () async throws -> T
    ) async throws -> T {
        try await withThrowingTaskGroup(of: T.self) { group in
            group.addTask { try await operation() }
            group.addTask {
                try await Task.sleep(for: .seconds(seconds))
                throw LibraryChangeTimeoutError()
            }
            // `group.next()` returns whichever task finishes first. If
            // that's the operation, we cancel the timer and return. If
            // it's the timer, it throws LibraryChangeTimeoutError and
            // the remaining operation task is cancelled by `cancelAll`.
            guard let first = try await group.next() else {
                throw LibraryChangeTimeoutError()
            }
            group.cancelAll()
            return first
        }
    }

    /// User-facing message for a first-page failure. Timeouts and
    /// network-down errors get distinct, actionable copy instead of a
    /// raw stacktrace-ish string.
    private static func describeLibraryChangeError(_ error: Error) -> String {
        if error is LibraryChangeTimeoutError {
            return "Server didn't respond in time. It may be offline or very slow."
        }
        if let api = error as? APIError {
            switch api {
            case .networkError(let message):
                return "Couldn't reach the server: \(message)"
            case .unauthorized:
                return "Your session has expired. Please sign in again."
            case .serverError(let code, let message):
                return "Server error (\(code)): \(message)"
            case .decodingError(let message):
                return "Server response wasn't valid: \(message)"
            case .noToken:
                return "Not signed in."
            }
        }
        if (error as NSError).code == NSURLErrorCancelled {
            return "Load cancelled."
        }
        return "Couldn't load library: \(error.localizedDescription)"
    }

    // MARK: - Date group completion

    /// Extract the calendar date (YYYY-MM-DD) from an asset's takenAt or
    /// createdAt, matching the grouping logic in `groupAssetsByDate`.
    private static func dateKey(for asset: AssetPageItem) -> String? {
        guard let dateStr = asset.takenAt ?? asset.createdAt else { return nil }
        // Quick parse: ISO8601 dates start with "YYYY-MM-DD"
        guard dateStr.count >= 10 else { return nil }
        return String(dateStr.prefix(10))
    }

    /// After a page load, keep fetching if the last asset's date matches
    /// what would be a partial group at the tail. Stops as soon as the
    /// next page starts a new date (or there are no more pages).
    private func completeLastDateGroup(
        client: APIClient,
        libraryId: String,
        isFirstPage: Bool
    ) async {
        guard let lastAsset = assets.last,
              let tailDate = Self.dateKey(for: lastAsset) else { return }

        while hasMoreAssets {
            guard let cursor = nextCursor else { break }
            guard selectedLibraryId == libraryId else { break }

            let items = filters.queryItems(
                libraryId: libraryId,
                pathPrefix: selectedPath,
                searchQuery: mode == .search ? searchQuery : nil,
                after: cursor,
                limit: 100
            )

            do {
                let response: QueryResponse = try await client.get(
                    "/v1/query", queryItems: items
                )
                guard selectedLibraryId == libraryId else { return }

                let pageItems = response.items.map { $0.toPageItem() }

                // Check if the first item of this page is still on the same date
                guard let firstItem = pageItems.first,
                      Self.dateKey(for: firstItem) == tailDate else {
                    // Different date — the previous group is complete.
                    nextCursor = response.nextCursor
                    hasMoreAssets = response.nextCursor != nil
                    let matching = pageItems.prefix(while: { Self.dateKey(for: $0) == tailDate })
                    assets.append(contentsOf: matching)
                    autoSelectNewAssets(Array(matching))
                    if matching.count < pageItems.count {
                        let rest = Array(pageItems.dropFirst(matching.count))
                        assets.append(contentsOf: rest)
                        autoSelectNewAssets(rest)
                    }
                    break
                }

                // Entire page is still the same date — append and continue
                assets.append(contentsOf: pageItems)
                autoSelectNewAssets(pageItems)
                nextCursor = response.nextCursor
                hasMoreAssets = response.nextCursor != nil
            } catch {
                break
            }
        }
    }

    // MARK: - Asset detail

    public func loadAssetDetail(assetId: String) async {
        guard let client else { return }

        selectedAssetId = assetId
        isLoadingDetail = true
        assetDetail = nil
        currentRating = .empty

        do {
            let detail: AssetDetail = try await client.get("/v1/assets/\(assetId)")
            assetDetail = detail
            // Fetch rating in parallel with the detail load would be ideal,
            // but the detail call is fast and we need the assetId confirmed.
            let ratings = try await client.lookupRatings(assetIds: [assetId])
            currentRating = ratings[assetId] ?? .empty
        } catch {
            self.error = "Failed to load asset: \(error)"
        }

        isLoadingDetail = false
    }

    /// Optimistically update the current rating and persist to the server.
    /// Retries once on failure; reverts local state if both attempts fail.
    public func updateCurrentRating(_ body: RatingUpdateBody) {
        guard let client, let assetId = selectedAssetId else { return }

        // Optimistic local update is already done by the RatingEditorView
        // binding — `currentRating` was mutated before this callback fires.
        let snapshot = currentRating

        Task {
            do {
                let result = try await client.updateRating(assetId: assetId, body: body)
                currentRating = result
            } catch {
                // Retry once
                do {
                    let result = try await client.updateRating(assetId: assetId, body: body)
                    currentRating = result
                } catch {
                    // Revert to pre-optimistic state
                    currentRating = snapshot
                    self.error = "Failed to save rating"
                }
            }
        }
    }

    /// Apply a filter from the lightbox metadata sidebar. Closes the
    /// lightbox and sets the filter, which triggers a reload via the
    /// `filters` didSet.
    public func applyFilterFromLightbox(_ filter: BrowseFilter) {
        closeLightbox()
        filters = filter
    }

    /// Apply a filter by merging specific fields into the current filter.
    /// Closes the lightbox. Preserves sort and library selection.
    /// Merge a new filter constraint into the current filters, close the
    /// lightbox, and reload. Stacks with existing filters rather than
    /// replacing them.
    public func applyMetadataFilter(_ build: (inout BrowseFilter) -> Void) {
        var merged = filters
        build(&merged)
        applyFilterFromLightbox(merged)
    }

    public func closeLightbox() {
        selectedAssetId = nil
        assetDetail = nil
        currentRating = .empty
        // Clear any People-view installed prev/next override so the next
        // lightbox open from the library grid uses the normal mode list.
        displayedAssetIdsOverride = nil
        displayedFaceIdsOverride = nil
        // Clear any cluster-review face highlight so the next lightbox
        // open doesn't pop a stale popover.
        pendingHighlightFaceId = nil
    }

    // MARK: - Search

    /// User-initiated search (from search bar). Filters are NOT cleared
    /// — they stack. The user clears filters explicitly via the chiclet
    /// bar's "Clear all" button.
    public func performSearch() async {
        // Commit the search text and clear the input field
        committedSearchQuery = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        searchQuery = "" // Clear field — chiclet represents the search
        await executeSearch()
    }

    /// Internal search that preserves existing filters. Used when
    /// code (not the user) triggers a search — e.g., tag click from
    /// the lightbox, which is a refinement, not a new intent.
    public func executeSearch() async {
        let query = committedSearchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty, let client else { return }

        mode = .search
        isSearching = true
        searchResults = []
        searchTotal = 0
        error = nil

        do {
            // Use unified /v1/query with all filters + search query.
            // The filter algebra sends ALL structured filters alongside
            // the text search, eliminating the old search/browse gap.
            //
            // Search is intentionally NOT scoped to selectedLibraryId.
            // When a user types "Cards" they want to find their card
            // photos, not "Cards in whichever library happens to be
            // selected right now." Limiting search to the active
            // library was the source of "I searched for Card and got
            // nothing useful" — they were sitting on Empty Besters
            // when the Cards live in Photos. Cross-library search is
            // the only intuitive default. selectedPath is also
            // dropped because path filters only make sense inside one
            // library.
            let items = filters.queryItems(
                libraryId: nil,
                pathPrefix: nil,
                searchQuery: query,
                limit: 500
            )

            let response: QueryResponse = try await client.get(
                "/v1/query", queryItems: items
            )
            // Convert QueryItem to SearchHit for compatibility with existing views
            searchResults = response.items.map { item in
                SearchHit(
                    type: "image",
                    assetId: item.assetId,
                    libraryId: item.libraryId,
                    libraryName: item.libraryName,
                    relPath: item.relPath,
                    thumbnailKey: item.thumbnailKey,
                    proxyKey: item.proxyKey,
                    description: item.searchContext?.snippet ?? "",
                    tags: [],
                    score: item.searchContext?.score ?? 0,
                    source: item.searchContext?.hitType ?? "query",
                    cameraMake: item.cameraMake,
                    cameraModel: item.cameraModel,
                    sceneId: nil,
                    startMs: item.searchContext?.startMs,
                    endMs: item.searchContext?.endMs,
                    mediaType: item.mediaType,
                    fileSize: item.fileSize,
                    durationSec: item.durationSec,
                    width: item.width,
                    height: item.height,
                    takenAt: item.takenAt,
                    snippet: item.searchContext?.snippet,
                    language: nil
                )
            }
            searchTotal = response.totalEstimate ?? response.items.count
        } catch {
            self.error = "Search failed: \(error)"
        }

        isSearching = false
    }

    public func clearSearch() {
        searchQuery = ""
        committedSearchQuery = ""
        searchResults = []
        searchTotal = 0
        mode = .library
        // Reload library assets with current filters (tag, camera, etc.)
        // so stale pre-search data doesn't show.
        reloadAssets()
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
            // Pass the embedding model so the server looks up the right
            // vectors. The model id/version come from the platform
            // BrowseAppContext: macOS reports whichever local provider is
            // available (CLIP if loaded, otherwise FeaturePrint); iOS
            // reports the canonical CLIP id since iOS doesn't enrich
            // and just needs to ask for the most-likely-indexed model.
            let embeddingModelId = appContext.embeddingModelId
            let embeddingModelVersion = appContext.embeddingModelVersion

            var params: [String: String] = [
                "asset_id": assetId,
                "library_id": libraryId,
                "limit": "50",
                "model_id": embeddingModelId,
                "model_version": embeddingModelVersion,
            ]

            // Map the filter UI's media-type picker onto `/v1/similar`'s
            // `asset_types` query (comma-separated, "image"/"video"). The
            // similar endpoint also accepts camera_make/camera_model/
            // from_ts/to_ts but on different param names than /v1/search;
            // we plumb only `asset_types` for now since that's the user-
            // visible filter the picker drives. The other dimensions can
            // be added incrementally as the UI exposes them in similar
            // mode.
            if let mediaType = filters.mediaType {
                params["asset_types"] = mediaType
            }

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

    public func loadRootDirectories() async {
        guard let client, let libraryId = selectedLibraryId else { return }
        do {
            let nodes: [DirectoryNode] = try await client.get(
                "/v1/libraries/\(libraryId)/directories"
            )
            // Reject stale writebacks after a library change mid-flight —
            // otherwise the directory tree for the PREVIOUS library lands
            // in the new library's sidebar.
            guard selectedLibraryId == libraryId else { return }
            directories = nodes
        } catch {
            // Non-fatal — grid still works without tree. Cancellation and
            // library-change races are absorbed silently.
        }
    }

    func loadChildDirectories(parentPath: String) async {
        guard let client, let libraryId = selectedLibraryId else { return }
        do {
            let nodes: [DirectoryNode] = try await client.get(
                "/v1/libraries/\(libraryId)/directories",
                query: ["parent": parentPath]
            )
            guard selectedLibraryId == libraryId else { return }
            childDirectories[parentPath] = nodes
        } catch {
            // Non-fatal
        }
    }

    public func toggleExpanded(path: String) {
        if expandedPaths.contains(path) {
            expandedPaths.remove(path)
        } else {
            expandedPaths.insert(path)
            if childDirectories[path] == nil {
                Task { await loadChildDirectories(parentPath: path) }
            }
        }
    }

    public func selectPath(_ path: String?) {
        selectedPath = path
    }

    // MARK: - Re-enrichment

    @Published public var isReEnriching = false
    @Published public var reEnrichPhase = ""
    @Published public var reEnrichTotal = 0
    @Published public var reEnrichProcessed = 0
    @Published public var reEnrichSkipped: [String] = []

    /// Pluggable re-enrichment runner. macOS sets this from
    /// `BrowseWindow` to a `MacReEnrichInvoker` that wraps the real
    /// `ReEnrichmentRunner` and its CLIP/ArcFace/Whisper providers.
    /// iOS leaves this nil — the lightbox's re-enrich menu is hidden,
    /// and the `reEnrich*` methods below short-circuit on a nil
    /// invoker. Public so the platform-specific entry point can install
    /// it after constructing `BrowseState`.
    public var reEnrichInvoker: (any ReEnrichInvoker)?

    /// Re-enrich assets in the current library, optionally scoped to a path prefix.
    public func reEnrich(operations: Set<EnrichmentOperation>, pathPrefix: String? = nil) {
        guard let invoker = reEnrichInvoker,
              client != nil,
              let libraryId = selectedLibraryId,
              !isReEnriching else { return }

        Task {
            isReEnriching = true
            reEnrichPhase = "fetching assets"
            reEnrichProcessed = 0
            reEnrichTotal = 0

            // Push the media_type filter to the server when the operation
            // set is single-modality. A 16k-image library was previously
            // dragging all 16k rows over the wire when the user only
            // wanted to transcribe 131 videos — the runner filters
            // internally either way, but we'd already paid the network +
            // server cost. Mixed operations (e.g. "All") still fetch
            // unfiltered.
            let mediaType = mediaTypeFilter(forOperations: operations)
            let assets = await fetchAllAssets(
                libraryId: libraryId,
                pathPrefix: pathPrefix,
                mediaType: mediaType,
            )
            guard !assets.isEmpty else {
                isReEnriching = false
                return
            }

            let result = await invoker.reEnrich(
                libraryId: libraryId,
                libraryRootPath: selectedLibraryRootPath,
                assets: assets,
                operations: operations
            ) { [weak self] processed, total, phase in
                self?.reEnrichProcessed = processed
                self?.reEnrichTotal = total
                self?.reEnrichPhase = phase
            }

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
    public func reEnrichAsset(assetId: String, operations: Set<EnrichmentOperation>) {
        guard let invoker = reEnrichInvoker,
              client != nil,
              let libraryId = selectedLibraryId,
              !isReEnriching else { return }

        // Find the asset in current display or fetch it
        let asset = assets.first { $0.assetId == assetId }
        guard let asset else { return }

        Task {
            isReEnriching = true
            reEnrichPhase = ""
            reEnrichProcessed = 0
            reEnrichTotal = 1

            let result = await invoker.reEnrich(
                libraryId: libraryId,
                libraryRootPath: selectedLibraryRootPath,
                assets: [asset],
                operations: operations
            ) { [weak self] processed, total, phase in
                self?.reEnrichProcessed = processed
                self?.reEnrichTotal = total
                self?.reEnrichPhase = phase
            }

            isReEnriching = false
            reEnrichPhase = ""
            reEnrichSkipped = result.skipped

            // Refresh the lightbox detail
            await loadAssetDetail(assetId: assetId)
        }
    }

    public func cancelReEnrich() {
        Task {
            await reEnrichInvoker?.cancel()
        }
    }

    /// Returns the appropriate `media_type` query value for an
    /// enrichment operation set, or nil if the set is mixed-modality.
    /// Used by `reEnrich` to push the media-type filter down to the
    /// server side and avoid pulling all images across the wire when
    /// only video operations are requested.
    private func mediaTypeFilter(forOperations ops: Set<EnrichmentOperation>) -> String? {
        let imageOps: Set<EnrichmentOperation> = [.faces, .embeddings, .ocr, .vision]
        let videoOps: Set<EnrichmentOperation> = [.videoPreview, .transcribe]
        if !ops.isEmpty && ops.isSubset(of: videoOps) { return "video" }
        if !ops.isEmpty && ops.isSubset(of: imageOps) { return "image" }
        return nil
    }

    /// Fetch all assets for a library/path (paginated). When `mediaType`
    /// is set the server filters to only that media type, avoiding the
    /// network + server cost of returning the other side of a mixed
    /// library.
    private func fetchAllAssets(
        libraryId: String,
        pathPrefix: String?,
        mediaType: String? = nil,
    ) async -> [AssetPageItem] {
        guard let client else { return [] }
        var all: [AssetPageItem] = []
        var cursor: String?

        repeat {
            var leafFilters = [LeafFilter(type: "library", value: libraryId)]
            if let pathPrefix { leafFilters.append(LeafFilter(type: "path", value: pathPrefix)) }
            if let mediaType { leafFilters.append(LeafFilter(type: "media", value: mediaType)) }
            let items = filtersToQueryItems(
                leafFilters,
                sort: "asset_id",
                direction: "asc",
                after: cursor,
                limit: 500
            )

            do {
                let response: QueryResponse = try await client.get(
                    "/v1/query", queryItems: items
                )
                all.append(contentsOf: response.items.map { $0.toPageItem() })
                cursor = response.nextCursor
                reEnrichTotal = all.count
            } catch {
                self.error = "Failed to fetch assets: \(error)"
                break
            }
        } while cursor != nil

        return all
    }

    // Note: progress polling moved out of BrowseState. The polling is
    // now the responsibility of `ReEnrichInvoker` implementations,
    // which call BrowseState's progress closure (passed into
    // `reEnrich(...)` above) every ~250 ms while work is in flight.
    // BrowseState only owns the @Published mirror, not the polling
    // mechanics.

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
    public func filterByPerson(_ person: PersonItem) {
        filters.personId = person.personId
        filters.personDisplayName = person.displayName
        personSuggestions = []
        searchQuery = ""
        mode = .library
        // Reload assets with the person filter applied. Can't use
        // resetAndLoad() — it wipes filters. reloadAssets() preserves
        // them and re-fetches from page 1.
        reloadAssets()
    }

    /// Clear the person filter.
    public func clearPersonFilter() {
        filters.personId = nil
        filters.personDisplayName = nil
    }

    /// Debounced person search triggered by search text changes.
    public func debouncedPersonSearch(query: String) {
        personSearchTask?.cancel()
        personSearchTask = Task {
            try? await Task.sleep(for: .milliseconds(400))
            guard !Task.isCancelled else { return }
            await searchPeople(query: query)
        }
    }

    // MARK: - Keyboard navigation

    public func navigateLightbox(direction: Int) {
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

    /// Advance focus by one (left -1 / right +1) through
    /// `displayedAssetIds`. ONLY meaningful while the lightbox is open
    /// — that's how lightbox prev/next works under the hood. The grid
    /// itself no longer has a visible focus concept; PgUp/PgDn etc.
    /// scroll the viewport directly via `scrollPageBy` instead.
    func moveFocus(direction: Int) {
        let ids = displayedAssetIds
        let newIndex = focusedIndex + direction
        if ids.indices.contains(newIndex) {
            focusedIndex = newIndex
            if selectedAssetId != nil {
                Task { await loadAssetDetail(assetId: ids[newIndex]) }
            }
        }
    }

    // (Scrolling is now handled directly by the grid view talking to
    // its underlying NSScrollView via the introspector — see
    // `pendingScrollCommand` / `sendScrollCommand` above. BrowseState
    // doesn't need to know anything about layout rows or visible
    // cells for scroll dispatch.)
}

/// Thrown when the first-page load for a library switch exceeds the
/// configured timeout. Surfaced in the overlay as a human-readable
/// "Server didn't respond in time" message.
struct LibraryChangeTimeoutError: Error, Sendable {}
