import SwiftUI
import LumiverbKit

/// Post-login root view. Tab bar mirrors Google Photos layout:
/// Libraries (photo grid), Collections, People, Search, Settings.
///
/// Owns the shared `BrowseState` and injects the cache bundle +
/// collections state into the SwiftUI environment so all descendant
/// views can read them via `@Environment`.
struct MainTabView: View {
    @ObservedObject var appState: iOSAppState
    @StateObject private var browseState: BrowseState
    @StateObject private var peopleState: PeopleState
    @StateObject private var collectionsState: CollectionsState
    @StateObject private var clusterReviewState: ClusterReviewState
    @StateObject private var networkMonitor = NetworkMonitor()

    private let cacheBundle: CacheBundle

    init(appState: iOSAppState) {
        self.appState = appState

        let context = iOSBrowseAppContext(appState: appState)
        let bs = BrowseState(appContext: context)
        bs.reEnrichInvoker = nil

        self._browseState = StateObject(wrappedValue: bs)
        self._peopleState = StateObject(wrappedValue: PeopleState(client: appState.client))
        self._collectionsState = StateObject(wrappedValue: CollectionsState(client: appState.client))
        self._clusterReviewState = StateObject(wrappedValue: ClusterReviewState(client: appState.client))

        self.cacheBundle = CacheBundle(
            proxies: MemoryImageCache(name: "ios.proxies"),
            thumbnails: IOSThumbnailDiskCache()
        )
    }

    var body: some View {
        TabView {
            NavigationStack {
                LibraryBrowseView(
                    appState: appState,
                    browseState: browseState
                )
                .navigationTitle("Libraries")
            }
            .tabItem {
                Label("Photos", systemImage: "photo.fill")
            }

            NavigationStack {
                CollectionsListView(
                    collectionsState: collectionsState,
                    client: appState.client,
                    showFavoritesLink: true
                )
                .navigationTitle("Collections")
                .navigationDestination(for: FavoritesDestination.self) { _ in
                    FavoritesView(appState: appState, browseState: browseState)
                }
                .navigationDestination(for: CollectionDetailRoute.self) { route in
                    CollectionDetailDestinationView(
                        route: route,
                        collectionsState: collectionsState,
                        browseState: browseState,
                        client: appState.client
                    )
                }
            }
            .tabItem {
                Label("Collections", systemImage: "rectangle.stack.fill")
            }

            // PeopleView wraps itself in a NavigationStack — don't
            // double-wrap.
            PeopleView(
                peopleState: peopleState,
                browseState: browseState,
                client: appState.client,
                clusterReviewState: clusterReviewState
            )
            .tabItem {
                Label("People", systemImage: "person.2.fill")
            }

            NavigationStack {
                SearchTab(
                    appState: appState,
                    browseState: browseState,
                    peopleState: peopleState
                )
            }
            .tabItem {
                Label("Search", systemImage: "magnifyingglass")
            }

            NavigationStack {
                iOSSettingsView(appState: appState)
                    .navigationTitle("Settings")
            }
            .tabItem {
                Label("Settings", systemImage: "gearshape.fill")
            }
        }
        .preferredColorScheme(.dark)
        .environment(\.cacheBundle, cacheBundle)
        .environment(\.collectionsState, collectionsState)
        .environmentObject(networkMonitor)
        // Top-level lightbox so any tab can open it (People tab's
        // cluster review opens the lightbox to highlight a face for
        // tagging — without this it'd silently set selectedAssetId
        // and nothing would happen).
        .fullScreenCover(isPresented: lightboxBinding) {
            iOSLightboxView(browseState: browseState, client: appState.client)
        }
    }

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
}

/// Wrapper for the iOS collection-detail navigation destination.
/// Loads the collection's assets via `openCollectionDetail` on appear,
/// and pops the navigation stack when `collectionsState.openCollection`
/// becomes nil — that's how `CollectionsState.deleteCollection` signals
/// "the detail you were looking at is gone, get out". Without this,
/// deleting a collection from CollectionDetailView left a blank detail
/// pane on the navigation stack.
private struct CollectionDetailDestinationView: View {
    let route: CollectionDetailRoute
    @ObservedObject var collectionsState: CollectionsState
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    @Environment(\.dismiss) private var dismiss

    var body: some View {
        CollectionDetailView(
            collectionsState: collectionsState,
            browseState: browseState,
            client: client
        )
        .task {
            // Mirror what `openCollectionDetail` does on macOS via the
            // sidebar tap — load the collection's assets when the
            // detail screen first appears.
            if let col = collectionsState.collections.first(
                where: { $0.collectionId == route.collectionId }
            ) {
                await collectionsState.openCollectionDetail(col)
            }
        }
        .onChange(of: collectionsState.openCollection?.collectionId) { _, newId in
            // Delete cleared the open collection — pop back to the list.
            if newId == nil {
                dismiss()
            }
        }
    }
}
