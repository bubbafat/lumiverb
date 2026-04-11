import SwiftUI
import LumiverbKit

/// Post-login root view. Owns the shared `BrowseState` and injects the
/// cache bundle + collections state into the SwiftUI environment so all
/// descendant views (shared LumiverbKit grids, lightbox, etc.) can read
/// them via `@Environment`.
struct MainTabView: View {
    @ObservedObject var appState: iOSAppState
    @StateObject private var browseState: BrowseState
    @StateObject private var peopleState: PeopleState
    @StateObject private var collectionsState: CollectionsState

    /// iOS cache bundle: in-memory proxy cache (no disk — iOS proxies are
    /// lightweight and re-fetched on next launch) + disk-backed thumbnail
    /// cache with ~200 MB LRU eviction.
    private let cacheBundle: CacheBundle

    init(appState: iOSAppState) {
        self.appState = appState

        let context = iOSBrowseAppContext(appState: appState)
        let bs = BrowseState(appContext: context)
        // iOS is browse-only — no re-enrich action in the lightbox.
        bs.reEnrichInvoker = nil

        self._browseState = StateObject(wrappedValue: bs)
        self._peopleState = StateObject(wrappedValue: PeopleState(client: appState.client))
        self._collectionsState = StateObject(wrappedValue: CollectionsState(client: appState.client))

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
                Label("Libraries", systemImage: "photo.stack")
            }
        }
        .environment(\.cacheBundle, cacheBundle)
        .environment(\.collectionsState, collectionsState)
    }
}
