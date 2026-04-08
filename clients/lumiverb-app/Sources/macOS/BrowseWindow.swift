import SwiftUI
import LumiverbKit

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
    @StateObject private var browseState: BrowseState

    init(appState: AppState) {
        self.appState = appState
        self._browseState = StateObject(wrappedValue: BrowseState(appState: appState))
    }

    var body: some View {
        NavigationSplitView {
            LibrarySidebar(
                libraries: appState.libraries,
                browseState: browseState
            )
            .navigationSplitViewColumnWidth(min: 180, ideal: 220, max: 300)
        } detail: {
            ZStack {
                VStack(spacing: 0) {
                    if browseState.isReEnriching || !browseState.reEnrichSkipped.isEmpty {
                        reEnrichBanner
                    }
                    activeFiltersBar
                    contentArea
                }
                if browseState.selectedAssetId != nil {
                    lightboxOverlay
                }
            }
        }
        .searchable(
            text: $browseState.searchQuery,
            placement: .toolbar,
            prompt: "Search assets or people..."
        )
        .searchSuggestions {
            if !browseState.personSuggestions.isEmpty {
                Section("People") {
                    ForEach(browseState.personSuggestions) { person in
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
                }
            }
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
        .onKeyPress(.return) {
            browseState.openFocusedAsset()
            return .handled
        }
        .onKeyPress(.leftArrow) {
            if browseState.selectedAssetId != nil {
                browseState.navigateLightbox(direction: -1)
            } else {
                browseState.moveFocus(direction: -1)
            }
            return .handled
        }
        .onKeyPress(.rightArrow) {
            if browseState.selectedAssetId != nil {
                browseState.navigateLightbox(direction: 1)
            } else {
                browseState.moveFocus(direction: 1)
            }
            return .handled
        }
        .onKeyPress(.upArrow) {
            browseState.moveFocus(direction: -1, columns: 4)
            return .handled
        }
        .onKeyPress(.downArrow) {
            browseState.moveFocus(direction: 1, columns: 4)
            return .handled
        }
        .onChange(of: appState.libraries.count) { _, _ in
            // Restore last opened library once libraries are loaded
            if browseState.selectedLibraryId == nil,
               let lastId = UserDefaults.standard.string(forKey: "lastLibraryId"),
               appState.libraries.contains(where: { $0.libraryId == lastId }) {
                browseState.selectedLibraryId = lastId
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
                MediaGridView(browseState: browseState, client: appState.client)
            }

        case .search:
            if browseState.isSearching {
                ProgressView("Searching...")
            } else if browseState.searchResults.isEmpty {
                emptyState("No results for \"\(browseState.searchQuery)\"", icon: "magnifyingglass")
            } else {
                SearchResultsGrid(browseState: browseState, client: appState.client)
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
                )
            }
        }

        if let error = browseState.error {
            Text(error)
                .foregroundColor(.red)
                .font(.caption)
                .padding()
        }
    }

    // MARK: - Lightbox overlay

    @ViewBuilder
    private var lightboxOverlay: some View {
        LightboxView(
            browseState: browseState,
            client: appState.client
        )
        .transition(.opacity.animation(.easeInOut(duration: 0.15)))
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
    private var activeFiltersBar: some View {
        if browseState.filters.hasActiveFilters {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    if browseState.filters.mediaType != nil {
                        filterChiclet(
                            label: browseState.filters.mediaType == "image" ? "Photos" : "Videos"
                        ) {
                            browseState.filters.mediaType = nil
                        }
                    }
                    if let name = browseState.filters.personDisplayName {
                        filterChiclet(label: name) {
                            browseState.clearPersonFilter()
                        }
                    }
                    if browseState.filters.sortField != "taken_at" || browseState.filters.sortDirection != "desc" {
                        let option = SortOption(rawValue: browseState.filters.sortField)
                        let dir = browseState.filters.sortDirection == "asc" ? "Asc" : "Desc"
                        filterChiclet(label: "\(option?.label ?? browseState.filters.sortField) \(dir)") {
                            browseState.filters.sortField = "taken_at"
                            browseState.filters.sortDirection = "desc"
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 4)
            }
            .background(.bar)
        }
    }

    private func filterChiclet(label: String, onRemove: @escaping () -> Void) -> some View {
        HStack(spacing: 4) {
            Text(label)
                .font(.caption)
                .lineLimit(1)
            Button {
                onRemove()
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(.quaternary)
        .cornerRadius(12)
    }

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
