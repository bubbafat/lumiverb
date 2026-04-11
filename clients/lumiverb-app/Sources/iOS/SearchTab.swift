import SwiftUI
import LumiverbKit

/// Search tab with face circles, search bar, and results.
/// Matches Google Photos search layout: face row at top, search bar,
/// recent searches, then results grouped by relevance.
struct SearchTab: View {
    @ObservedObject var appState: iOSAppState
    @ObservedObject var browseState: BrowseState
    @ObservedObject var peopleState: PeopleState

    var body: some View {
        VStack(spacing: 0) {
            if browseState.mode == .search || browseState.isSearching {
                searchResultsContent
            } else if browseState.filters.personId != nil {
                personFilterContent
            } else if case .similar(let sourceId) = browseState.mode {
                similarResultsContent(sourceId: sourceId)
            } else {
                searchHomeContent
            }
        }
        .navigationTitle("Search")
        .searchable(
            text: $browseState.searchQuery,
            placement: .navigationBarDrawer(displayMode: .always),
            prompt: "Search photos"
        )
        .onSubmit(of: .search) {
            browseState.personSuggestions = []
            Task { await browseState.performSearch() }
        }
        .onChange(of: browseState.searchQuery) { _, newValue in
            if newValue.isEmpty {
                if browseState.committedSearchQuery.isEmpty {
                    browseState.clearSearch()
                }
                browseState.personSuggestions = []
            } else {
                browseState.debouncedPersonSearch(query: newValue)
            }
        }
        .searchSuggestions {
            personSuggestions
        }
        .fullScreenCover(isPresented: lightboxBinding) {
            iOSLightboxView(browseState: browseState, client: appState.client)
        }
    }

    // MARK: - Search home (no active search)

    @ViewBuilder
    private var searchHomeContent: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                // Face circles row
                if !peopleState.people.isEmpty {
                    faceCirclesRow
                }

                if browseState.isSearching {
                    ProgressView("Searching...")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .padding(.top, 60)
                }
            }
            .padding(.top, 8)
        }
        .task {
            await peopleState.loadIfNeeded()
        }
    }

    // MARK: - Face circles

    private var faceCirclesRow: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("People")
                .font(.headline)
                .padding(.horizontal)

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 16) {
                    ForEach(peopleState.people) { person in
                        Button {
                            browseState.filterByPerson(person)
                        } label: {
                            VStack(spacing: 6) {
                                FaceThumbnailView(
                                    faceId: person.representativeFaceId,
                                    client: appState.client
                                )
                                .frame(width: 64, height: 64)
                                .clipShape(Circle())

                                Text(person.displayName)
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                                    .lineLimit(1)
                                    .frame(width: 64)
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal)
            }
        }
    }

    // MARK: - Search results

    @ViewBuilder
    private var searchResultsContent: some View {
        VStack(spacing: 0) {
            FilterChicletBar(browseState: browseState)
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
    }

    // MARK: - Person filter results

    /// Shows the asset grid filtered by a person (tapped from face circles).
    /// `filterByPerson` sets mode to `.library` and adds a person filter,
    /// so we reuse the standard grid views.
    @ViewBuilder
    private var personFilterContent: some View {
        VStack(spacing: 0) {
            FilterChicletBar(browseState: browseState)
            if browseState.assets.isEmpty && !browseState.isLoadingAssets {
                ContentUnavailableView(
                    "No Photos",
                    systemImage: "person.crop.rectangle",
                    description: Text("No photos found for this person")
                )
            } else {
                MediaGridView(browseState: browseState, client: appState.client) {
                    EmptyView()
                }
            }
        }
    }

    @ViewBuilder
    private func similarResultsContent(sourceId: String) -> some View {
        if browseState.isFindingSimilar {
            ProgressView("Finding similar...")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if browseState.similarResults.isEmpty {
            ContentUnavailableView(
                "No Similar Photos",
                systemImage: "square.stack.3d.up"
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

    // MARK: - Person suggestions

    @ViewBuilder
    private var personSuggestions: some View {
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

    // MARK: - Lightbox

    private var lightboxBinding: Binding<Bool> {
        Binding(
            get: { browseState.selectedAssetId != nil },
            set: { if !$0 { browseState.closeLightbox() } }
        )
    }
}
