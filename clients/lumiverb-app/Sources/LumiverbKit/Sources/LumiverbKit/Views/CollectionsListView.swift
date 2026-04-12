import SwiftUI

/// List of collections grouped into Mine / Shared with a "+" button.
public struct CollectionsListView: View {
    @ObservedObject public var collectionsState: CollectionsState
    public let client: APIClient?

    /// When true, render a "Favorites" virtual collection entry at the
    /// top of the list. iOS sets this; the destination is wired by the
    /// iOS app's `MainTabView` via `.navigationDestination(for:
    /// FavoritesDestination.self)`. macOS doesn't surface this — the
    /// macOS sidebar already has its own paths to favorited photos via
    /// the FilterChicletBar.
    public let showFavoritesLink: Bool

    @State private var showCreateSheet = false

    public init(
        collectionsState: CollectionsState,
        client: APIClient?,
        showFavoritesLink: Bool = false
    ) {
        self.collectionsState = collectionsState
        self.client = client
        self.showFavoritesLink = showFavoritesLink
    }

    public var body: some View {
        List {
            if showFavoritesLink {
                Section {
                    NavigationLink(value: FavoritesDestination.shared) {
                        HStack(spacing: 12) {
                            Image(systemName: "star.fill")
                                .foregroundColor(.yellow)
                                .frame(width: 30, height: 30)
                                .background(Color.yellow.opacity(0.15))
                                .clipShape(RoundedRectangle(cornerRadius: 6))
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Favorites")
                                    .lineLimit(1)
                                Text("Photos you've starred")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }
                    }
                }
            }

            if !collectionsState.ownCollections.isEmpty {
                Section("Mine") {
                    ForEach(collectionsState.ownCollections) { col in
                        collectionRow(col)
                    }
                }
            }

            if !collectionsState.sharedCollections.isEmpty {
                Section("Shared") {
                    ForEach(collectionsState.sharedCollections) { col in
                        collectionRow(col)
                    }
                }
            }

            if collectionsState.collections.isEmpty && !collectionsState.isLoadingList {
                Text("No collections yet")
                    .foregroundColor(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding()
            }
        }
        .overlay {
            if collectionsState.isLoadingList && collectionsState.collections.isEmpty {
                ProgressView()
            }
        }
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button {
                    showCreateSheet = true
                } label: {
                    Label("New Collection", systemImage: "plus")
                }
            }
        }
        .sheet(isPresented: $showCreateSheet) {
            CreateCollectionSheet(collectionsState: collectionsState)
        }
        .task {
            await collectionsState.loadCollections()
        }
    }

    @ViewBuilder
    private func collectionRow(_ col: AssetCollection) -> some View {
        #if os(iOS)
        // iOS uses NavigationLink so the row pushes a destination
        // (CollectionDetailView) wired by the host app via
        // `.navigationDestination(for: CollectionDetailRoute.self)`.
        // The destination view's `.task` calls `openCollectionDetail`
        // to load the data.
        NavigationLink(value: CollectionDetailRoute(collectionId: col.collectionId)) {
            collectionRowLabel(col)
        }
        #else
        // macOS uses the section-switch flow: tapping calls
        // `openCollectionDetail`, and BrowseWindow's `.onChange` of
        // `collectionsState.openCollection` flips the sidebar section
        // to `.collectionDetail`.
        Button {
            Task { await collectionsState.openCollectionDetail(col) }
        } label: {
            collectionRowLabel(col)
        }
        .buttonStyle(.plain)
        #endif
    }

    @ViewBuilder
    private func collectionRowLabel(_ col: AssetCollection) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(col.name)
                    .lineLimit(1)
                Text("\(col.assetCount) item\(col.assetCount == 1 ? "" : "s")")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            Spacer()
            if col.isSmart {
                Image(systemName: "wand.and.stars")
                    .font(.caption)
                    .foregroundColor(.purple)
            }
            if col.parsedVisibility == .shared {
                Image(systemName: "person.2")
                    .font(.caption)
                    .foregroundColor(.secondary)
            } else if col.parsedVisibility == .public {
                Image(systemName: "globe")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
    }
}

/// Marker type for a single collection's detail navigation destination.
/// Wraps just the collection_id so the value is Hashable + Sendable —
/// `AssetCollection` itself contains a `SavedQuery` with type-erased
/// values that can't be made Hashable cheaply.
public struct CollectionDetailRoute: Hashable, Sendable {
    public let collectionId: String
    public init(collectionId: String) {
        self.collectionId = collectionId
    }
}

/// Marker type for the Favorites virtual collection navigation
/// destination. The host app provides the actual destination view via
/// `.navigationDestination(for: FavoritesDestination.self)` — keeps
/// the iOS-specific FavoritesView out of LumiverbKit.
public struct FavoritesDestination: Hashable, Sendable {
    public static let shared = FavoritesDestination()
    private init() {}
}

// MARK: - Create collection sheet

struct CreateCollectionSheet: View {
    @ObservedObject var collectionsState: CollectionsState
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""

    var body: some View {
        VStack(spacing: 16) {
            Text("New Collection")
                .font(.headline)

            TextField("Name", text: $name)
                .textFieldStyle(.roundedBorder)

            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Create") {
                    Task {
                        _ = await collectionsState.createCollection(name: name)
                        dismiss()
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding()
        .frame(minWidth: 300)
    }
}
