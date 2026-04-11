import SwiftUI
import LumiverbKit

/// Top-level sidebar section. Switches the detail panel between the
/// existing library/search/similar browse experience, the People view
/// (Phase 6 M3 of ADR-014), and the cluster review view (Phase 6 M5).
enum SidebarSection: Equatable {
    case library
    case people
    case review
    case collections
    case collectionDetail
}

/// Sort options for the asset grid.
enum SortOption: String, CaseIterable, Identifiable {
    case takenAt = "taken_at"
    case createdAt = "created_at"
    case fileSize = "file_size"
    case iso = "iso"
    case aperture = "aperture"
    case focalLength = "focal_length"
    case relPath = "rel_path"

    var id: String { rawValue }

    var label: String {
        switch self {
        case .takenAt: return "Date Taken"
        case .createdAt: return "Date Added"
        case .fileSize: return "File Size"
        case .iso: return "ISO"
        case .aperture: return "Aperture"
        case .focalLength: return "Focal Length"
        case .relPath: return "Filename"
        }
    }
}

/// Main browse window with library sidebar, media grid, and lightbox.
struct BrowseWindow: View {
    @ObservedObject var appState: AppState
    @ObservedObject var scanState: ScanState
    @StateObject private var browseState: BrowseState
    @StateObject private var peopleState: PeopleState
    @StateObject private var clusterReviewState: ClusterReviewState
    @StateObject private var collectionsState: CollectionsState

    /// Which top-level sidebar section is active. Drives whether the
    /// detail panel shows the existing library browse UI, the People
    /// view (M3), or the cluster review view (M5).
    @State private var section: SidebarSection = .library

    init(appState: AppState, scanState: ScanState) {
        self.appState = appState
        self.scanState = scanState
        // BrowseState consumes the cross-platform `BrowseAppContext`
        // protocol that AppState now conforms to (see AppState.swift).
        // The re-enrich invoker is installed after construction so the
        // closure can hold the same AppState reference for vision /
        // whisper config lookup at run time.
        let browseState = BrowseState(appContext: appState)
        browseState.reEnrichInvoker = MacReEnrichInvoker(appState: appState)
        self._browseState = StateObject(wrappedValue: browseState)
        self._peopleState = StateObject(wrappedValue: PeopleState(client: appState.client))
        self._clusterReviewState = StateObject(
            wrappedValue: ClusterReviewState(client: appState.client)
        )
        self._collectionsState = StateObject(
            wrappedValue: CollectionsState(client: appState.client)
        )
    }

    var body: some View {
        NavigationSplitView {
            LibrarySidebar(
                libraries: appState.libraries,
                browseState: browseState,
                appState: appState,
                scanState: scanState,
                section: $section
            )
            .navigationSplitViewColumnWidth(min: 180, ideal: 220, max: 300)
        } detail: {
            detailContent
                .environment(\.collectionsState, collectionsState)
        }
        .searchable(
            text: $browseState.searchQuery,
            placement: .toolbar,
            prompt: "Search assets or people..."
        )
        .searchSuggestions {
            personSuggestions
        }
        .onSubmit(of: .search) {
            browseState.personSuggestions = []
            Task { await browseState.performSearch() }
        }
        .onChange(of: browseState.searchQuery) { _, newValue in
            if newValue.isEmpty {
                browseState.clearSearch()
                browseState.personSuggestions = []
            } else {
                browseState.debouncedPersonSearch(query: newValue)
            }
        }
        .toolbar {
            ToolbarItem(placement: .automatic) {
                mediaTypePicker
            }
            ToolbarItem(placement: .automatic) {
                sortMenu
            }
            ToolbarItem(placement: .automatic) {
                modeIndicator
            }
        }
        .focusedSceneValue(\.browseState, browseState)
        .onKeyPress(.escape) {
            if browseState.selectedAssetId != nil {
                browseState.closeLightbox()
                return .handled
            }
            if browseState.mode != .library {
                browseState.clearSearch()
                return .handled
            }
            return .ignored
        }
        // Arrow keys: lightbox navigates prev/next; grid scrolls one
        // row up/down. Left/right are no-ops in grid mode (the grid
        // has no concept of "selected cell" — click opens the lightbox).
        .onKeyPress(.leftArrow) {
            if browseState.selectedAssetId != nil {
                browseState.navigateLightbox(direction: -1)
                return .handled
            }
            return .ignored
        }
        .onKeyPress(.rightArrow) {
            if browseState.selectedAssetId != nil {
                browseState.navigateLightbox(direction: 1)
                return .handled
            }
            return .ignored
        }
        .onKeyPress(.upArrow) {
            if browseState.selectedAssetId != nil {
                browseState.navigateLightbox(direction: -1)
            } else {
                browseState.sendScrollCommand(.lineUp)
            }
            return .handled
        }
        .onKeyPress(.downArrow) {
            if browseState.selectedAssetId != nil {
                browseState.navigateLightbox(direction: 1)
            } else {
                browseState.sendScrollCommand(.lineDown)
            }
            return .handled
        }
        // Page / Home / End: always scroll the viewport, regardless of
        // lightbox state. With the lightbox open these are unusual
        // gestures but harmless — the next time the user closes the
        // lightbox they'll see the new scroll position.
        .onKeyPress(.pageDown) {
            browseState.sendScrollCommand(.pageDown)
            return .handled
        }
        .onKeyPress(.pageUp) {
            browseState.sendScrollCommand(.pageUp)
            return .handled
        }
        .onKeyPress(.home) {
            browseState.sendScrollCommand(.home)
            return .handled
        }
        .onKeyPress(.end) {
            browseState.sendScrollCommand(.end)
            return .handled
        }
        .onChange(of: appState.libraries.count) { _, _ in
            restoreLastOpenedLibraryIfNeeded()
        }
        .onAppear {
            // Honor any pending menu-bar request to open with a specific
            // library selected. Cleared after consuming so a subsequent
            // window-open without a request doesn't re-trigger.
            consumePendingLibraryId()
        }
        .onChange(of: appState.pendingSelectedLibraryId) { _, _ in
            consumePendingLibraryId()
        }
        .onChange(of: browseState.selectedLibraryId) { oldValue, newValue in
            // Reset + reload on library change. This used to live in a
            // `didSet` on `selectedLibraryId`, but because SwiftUI's
            // `List(selection:)` invokes the setter synchronously from
            // inside its click-dispatch path, the 17-@Published-mutation
            // cascade blocked the next click for as long as it took to
            // run. Moving the trigger here means the binding setter
            // returns immediately and SwiftUI processes the next click
            // on the next run loop tick — one frame of old-asset flash
            // in exchange for a responsive sidebar.
            //
            // We fire for nil transitions too (e.g. logout) so the old
            // `didSet`-based behavior of clearing grid state on
            // deselection is preserved.
            guard newValue != oldValue else { return }
            browseState.handleSelectedLibraryChange()
        }
        .onChange(of: collectionsState.openCollection?.collectionId) { _, newValue in
            if newValue != nil {
                section = .collectionDetail
            } else if section == .collectionDetail {
                section = .collections
            }
        }
    }

    /// Restore the last-opened library on first load. Extracted from the
    /// inline `.onChange` closure because the triple if-let-and over
    /// cross-module `browseState.selectedLibraryId` and `appState.libraries`
    /// pushed the type checker over its timeout after the BrowseState
    /// move into LumiverbKit.
    private func restoreLastOpenedLibraryIfNeeded() {
        guard browseState.selectedLibraryId == nil else { return }
        guard let lastId = UserDefaults.standard.string(forKey: "lastLibraryId") else { return }
        let exists = appState.libraries.contains(where: { $0.libraryId == lastId })
        guard exists else { return }
        browseState.selectedLibraryId = lastId
    }

    // MARK: - Detail pane

    /// Right-hand pane of the NavigationSplitView. Extracted from the
    /// inline `detail:` closure because the inline body — switch over the
    /// section, ZStack with two overlays, and a chain of cross-module
    /// `browseState.*` accesses — pushed Swift's type checker over its
    /// inference timeout after the BrowseState move into LumiverbKit.
    @ViewBuilder
    private var detailContent: some View {
        if browseState.selectedAssetId != nil {
            lightboxOverlay
        } else {
            ZStack {
                sectionContent
                if browseState.isChangingLibrary && section == .library {
                    changingLibraryOverlay
                }
            }
        }
    }

    @ViewBuilder
    private var sectionContent: some View {
        switch section {
        case .library:
            libraryColumn
        case .people:
            PeopleView(
                peopleState: peopleState,
                browseState: browseState,
                client: appState.client
            )
        case .review:
            ClusterReviewView(
                state: clusterReviewState,
                browseState: browseState,
                client: appState.client
            )
        case .collections:
            CollectionsListView(
                collectionsState: collectionsState,
                client: appState.client
            )
        case .collectionDetail:
            CollectionDetailView(
                collectionsState: collectionsState,
                browseState: browseState,
                client: appState.client
            )
        }
    }

    @ViewBuilder
    private var libraryColumn: some View {
        VStack(spacing: 0) {
            if browseState.isReEnriching || !browseState.reEnrichSkipped.isEmpty {
                reEnrichBanner
            } else if let libId = browseState.selectedLibraryId,
                      scanState.libraryStatus[libId] == .busy {
                // Background sync/enrich activity banner. Shows the same
                // status text as the menu bar so users in the browse
                // window don't miss that work is in flight on the
                // library they're looking at. The user-triggered
                // re-enrich banner takes priority when both are active —
                // they convey similar info and stacking both would be
                // noisy.
                scanActivityBanner
            }
            FilterChicletBar(browseState: browseState)
            contentArea
        }
    }

    // MARK: - Scroll introspector

    /// AppKit scroll introspector view that the LumiverbKit grids embed
    /// inside their `LazyVStack`'s `.background` so the introspector's
    /// superview walk can find the `NSScrollView`. The introspector
    /// callback wires the discovered scroll view into
    /// `appState.scrollAccessor.box` so `BrowseState.pendingScrollCommand`
    /// can dispatch through it via `MacScrollAccessor.apply(_:)`. The
    /// callback also tunes `verticalLineScroll` to the grid's row
    /// height so one arrow-key press advances ~one row.
    private var macScrollIntrospector: some View {
        NSScrollViewIntrospector { sv in
            appState.scrollAccessor.box.scrollView = sv
            sv.verticalLineScroll = MediaGridLayoutConstants.verticalLineScrollHeight
        }
    }

    // MARK: - Person search suggestions

    /// Person search suggestions surface inside `.searchSuggestions { }`.
    /// Extracted into its own ViewBuilder property because the inline form
    /// pushed SwiftUI's type checker over the timeout limit after the
    /// browse state moved into LumiverbKit (the protocol-existential
    /// `appContext` and the cross-module `PersonItem` together inflated
    /// the inference cost — splitting the body lets each subexpression
    /// resolve independently).
    @ViewBuilder
    private var personSuggestions: some View {
        if !browseState.personSuggestions.isEmpty {
            Section("People") {
                ForEach(browseState.personSuggestions) { person in
                    personSuggestionRow(person: person)
                }
            }
        }
    }

    @ViewBuilder
    private func personSuggestionRow(person: PersonItem) -> some View {
        Button {
            browseState.filterByPerson(person)
        } label: {
            HStack(spacing: 8) {
                FaceThumbnailView(
                    faceId: person.representativeFaceId,
                    client: appState.client
                )
                .frame(width: 28, height: 28)
                .clipShape(Circle())
                VStack(alignment: .leading, spacing: 1) {
                    Text(person.displayName)
                    Text("\(person.faceCount) photos")
                        .font(.caption2)
                        .foregroundColor(.secondary)
                }
            }
        }
    }

    // MARK: - Content area

    @ViewBuilder
    private var contentArea: some View {
        switch browseState.mode {
        case .library:
            if browseState.selectedLibraryId == nil {
                emptyState("Select a library", icon: "sidebar.left")
            } else if browseState.assets.isEmpty && !browseState.isLoadingAssets {
                emptyState("No assets in this library", icon: "photo.on.rectangle.angled")
            } else {
                VStack(spacing: 0) {
                    SelectionToolbarView(browseState: browseState, client: appState.client)
                    MediaGridView(browseState: browseState, client: appState.client) {
                        macScrollIntrospector
                    }
                }
            }

        case .search:
            if browseState.isSearching {
                ProgressView("Searching...")
            } else if browseState.searchResults.isEmpty {
                emptyState("No results for \"\(browseState.searchQuery)\"", icon: "magnifyingglass")
            } else {
                SearchResultsGrid(browseState: browseState, client: appState.client) {
                    macScrollIntrospector
                }
            }

        case .similar(let sourceId):
            if browseState.isFindingSimilar {
                ProgressView("Finding similar...")
            } else if browseState.similarResults.isEmpty {
                emptyState("No similar assets found", icon: "square.stack.3d.up")
            } else {
                SimilarResultsGrid(
                    browseState: browseState,
                    sourceAssetId: sourceId,
                    client: appState.client
                ) {
                    macScrollIntrospector
                }
            }
        }

        if let error = browseState.error {
            Text(error)
                .foregroundColor(.red)
                .font(.caption)
                .padding()
        }
    }

    // MARK: - Pending library selection (menu bar → window)

    /// Apply any pending menu-bar request to switch to a specific library
    /// and clear the request. Called from `.onAppear` (when the window
    /// opens fresh from the menu bar) and `.onChange` (when the window
    /// was already open and the user clicks another favorite). The
    /// section is forced to `.library` because that's where the user
    /// expects to land — favorites bypass People/Review.
    private func consumePendingLibraryId() {
        guard let id = appState.pendingSelectedLibraryId else { return }
        // Only switch if the requested library actually exists in the
        // current list. Otherwise leave the pending value alone in case
        // libraries are still loading from the server.
        guard appState.libraries.contains(where: { $0.libraryId == id }) else { return }
        if browseState.selectedLibraryId != id {
            browseState.selectedLibraryId = id
        }
        section = .library
        appState.pendingSelectedLibraryId = nil
    }

    // MARK: - Lightbox overlay

    @ViewBuilder
    private var lightboxOverlay: some View {
        LightboxView(
            browseState: browseState,
            client: appState.client
        )
    }

    /// Overlay shown while a library-switch is in progress. Appears
    /// immediately on click (tied to `browseState.isChangingLibrary`,
    /// which is set synchronously in `handleSelectedLibraryChange()`)
    /// and clears when the first page of assets comes back. If the
    /// load times out (10s) or the server returns an error, the
    /// overlay flips into an error state with Retry / Back actions
    /// instead of dismissing — the user isn't stranded on a blank grid.
    ///
    /// The backdrop doesn't block hit-testing on the sidebar (it's a
    /// child of the `detail` pane in `NavigationSplitView`), so the
    /// user can still click a different library mid-load or mid-error.
    @ViewBuilder
    private var changingLibraryOverlay: some View {
        let name = appState.libraries.first { $0.libraryId == browseState.selectedLibraryId }?.name ?? "library"
        ZStack {
            Color.black.opacity(0.25)
                .ignoresSafeArea()
            if let errorMessage = browseState.libraryChangeError {
                libraryChangeErrorCard(name: name, message: errorMessage)
            } else {
                libraryChangeLoadingCard(name: name)
            }
        }
        .transition(.opacity.animation(.easeInOut(duration: 0.12)))
    }

    private func libraryChangeLoadingCard(name: String) -> some View {
        VStack(spacing: 14) {
            ProgressView()
                .controlSize(.large)
            VStack(spacing: 4) {
                Text("Loading library…")
                    .font(.headline)
                Text(name)
                    .font(.subheadline)
                    .foregroundColor(.secondary)
            }
        }
        .padding(28)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(.regularMaterial)
        )
    }

    private func libraryChangeErrorCard(name: String, message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 32))
                .foregroundColor(.orange)
            Text("Couldn't load \(name)")
                .font(.headline)
            Text(message)
                .font(.callout)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 10) {
                Button("Back") { browseState.revertLibraryChange() }
                    .keyboardShortcut(.cancelAction)
                Button("Retry") { browseState.retryLibraryChange() }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
            .padding(.top, 4)
        }
        .padding(24)
        .frame(maxWidth: 360)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(.regularMaterial)
        )
    }

    // MARK: - Mode indicator

    @ViewBuilder
    private var modeIndicator: some View {
        switch browseState.mode {
        case .library:
            EmptyView()
        case .search:
            HStack(spacing: 4) {
                Text("\(browseState.searchTotal) results")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Button {
                    browseState.clearSearch()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
            }
        case .similar:
            HStack(spacing: 4) {
                Text("\(browseState.similarTotal) similar")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Button {
                    browseState.mode = .library
                    browseState.similarResults = []
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
            }
        }
    }

    // MARK: - Re-enrichment banner

    private var reEnrichBanner: some View {
        VStack(alignment: .leading, spacing: 4) {
            if browseState.isReEnriching {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                    Text("Re-enriching: \(browseState.reEnrichPhase)")
                        .font(.caption)
                    if browseState.reEnrichTotal > 0 {
                        Text("\(browseState.reEnrichProcessed) of \(browseState.reEnrichTotal)")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    Spacer()
                    Button {
                        browseState.cancelReEnrich()
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundColor(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
            if !browseState.reEnrichSkipped.isEmpty {
                HStack(spacing: 4) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundColor(.yellow)
                        .font(.caption)
                    Text("Skipped: \(browseState.reEnrichSkipped.joined(separator: ", "))")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    Spacer()
                    Button {
                        browseState.reEnrichSkipped = []
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundColor(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(.bar)
    }

    // MARK: - Scan activity banner

    /// Thin progress banner shown when the currently-selected library
    /// is being scanned or enriched by the background scan. Gives the
    /// browse-window user the same visibility into pipeline state
    /// that the menu bar dropdown already provides.
    private var scanActivityBanner: some View {
        HStack(spacing: 8) {
            ProgressView()
                .controlSize(.small)
            Text(scanState.statusText)
                .font(.caption)
            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(.bar)
    }

    // MARK: - Sort menu

    @ViewBuilder
    private var sortMenu: some View {
        Menu {
            ForEach(SortOption.allCases) { option in
                Button {
                    browseState.filters.sortField = option.rawValue
                } label: {
                    HStack {
                        Text(option.label)
                        if browseState.filters.sortField == option.rawValue {
                            Image(systemName: "checkmark")
                        }
                    }
                }
            }
            Divider()
            Button {
                browseState.filters.sortDirection = browseState.filters.sortDirection == "asc" ? "desc" : "asc"
            } label: {
                Label(
                    browseState.filters.sortDirection == "asc" ? "Ascending" : "Descending",
                    systemImage: browseState.filters.sortDirection == "asc" ? "arrow.up" : "arrow.down"
                )
            }
        } label: {
            Label("Sort", systemImage: "arrow.up.arrow.down")
        }
    }

    // MARK: - Media type picker

    @ViewBuilder
    private var mediaTypePicker: some View {
        Picker("Media", selection: Binding(
            get: { browseState.filters.mediaType ?? "all" },
            set: { browseState.filters.mediaType = $0 == "all" ? nil : $0 }
        )) {
            Text("All").tag("all")
            Label("Photos", systemImage: "photo").tag("image")
            Label("Videos", systemImage: "video").tag("video")
        }
        .pickerStyle(.segmented)
        .frame(width: 180)
    }

    // MARK: - Active filters bar

    @ViewBuilder

    private func emptyState(_ message: String, icon: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 40))
                .foregroundColor(.secondary)
            Text(message)
                .font(.title3)
                .foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Focused scene value for keyboard handling

struct BrowseStateFocusedKey: FocusedValueKey {
    typealias Value = BrowseState
}

extension FocusedValues {
    var browseState: BrowseState? {
        get { self[BrowseStateFocusedKey.self] }
        set { self[BrowseStateFocusedKey.self] = newValue }
    }
}
