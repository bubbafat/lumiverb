import SwiftUI
import LumiverbKit

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
            prompt: "Search assets..."
        )
        .onSubmit(of: .search) {
            Task { await browseState.performSearch() }
        }
        .onChange(of: browseState.searchQuery) { _, newValue in
            if newValue.isEmpty {
                browseState.clearSearch()
            }
        }
        .toolbar {
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
