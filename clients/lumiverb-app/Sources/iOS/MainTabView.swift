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
                    client: appState.client
                )
                .navigationTitle("Collections")
            }
            .tabItem {
                Label("Collections", systemImage: "rectangle.stack.fill")
            }

            NavigationStack {
                PeopleView(
                    peopleState: peopleState,
                    browseState: browseState,
                    client: appState.client
                )
                .navigationTitle("People")
            }
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
    }
}
