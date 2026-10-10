import Foundation

enum CollectionsError: Error {
    case noClient
}

/// Observable state for the collections feature. Holds the list of
/// collections, the currently-open collection's assets, and loading flags.
@MainActor
public final class CollectionsState: ObservableObject {
    private let client: APIClient?

    // MARK: - List

    @Published public var collections: [Project] = []
    @Published public var isLoadingList = false

    // MARK: - Detail

    @Published public var openCollection: Project?
    @Published public var collectionAssets: [ProjectAsset] = []
    @Published public var isLoadingAssets = false
    @Published public var nextCursor: String?

    // MARK: - Mutations

    @Published public var error: String?

    public init(client: APIClient?) {
        self.client = client
    }

    // MARK: - List operations

    public func loadCollections() async {
        guard let client else { return }
        isLoadingList = true
        error = nil
        do {
            collections = try await client.listProjects()
        } catch {
            self.error = "Failed to load collections: \(error)"
        }
        isLoadingList = false
    }

    // MARK: - Detail operations

    public func openCollectionDetail(_ collection: Project) async {
        guard let client else { return }
        openCollection = collection
        collectionAssets = []
        nextCursor = nil
        isLoadingAssets = true
        do {
            let response = try await client.listProjectAssets(id: collection.projectId)
            collectionAssets = response.items
            nextCursor = response.nextCursor
        } catch {
            self.error = "Failed to load collection assets: \(error)"
        }
        isLoadingAssets = false
    }

    public func loadNextPage() async {
        guard let client,
              let collection = openCollection,
              let cursor = nextCursor,
              !isLoadingAssets else { return }
        isLoadingAssets = true
        do {
            let response = try await client.listProjectAssets(
                id: collection.projectId, after: cursor
            )
            collectionAssets.append(contentsOf: response.items)
            nextCursor = response.nextCursor
        } catch {
            self.error = "Failed to load more assets: \(error)"
        }
        isLoadingAssets = false
    }

    public func closeDetail() {
        openCollection = nil
        collectionAssets = []
        nextCursor = nil
    }

    // MARK: - CRUD

    public func createCollection(
        name: String,
        description: String? = nil,
        visibility: ProjectVisibility = .private,
        assetIds: [String]? = nil
    ) async -> Project? {
        guard let client else { return nil }
        do {
            let body = CreateProjectRequest(
                name: name,
                description: description,
                visibility: visibility,
                assetIds: assetIds
            )
            let created = try await client.createProject(body: body)
            collections.insert(created, at: 0)
            return created
        } catch {
            self.error = "Failed to create collection: \(error)"
            return nil
        }
    }

    /// Save a search as a project: it holds the clips the search matches
    /// now and doesn't follow the search afterwards.
    public func createFromSearch(name: String, search: SavedQueryV2) async throws -> Project {
        guard let client else { throw CollectionsError.noClient }
        let created = try await client.createProject(
            body: CreateProjectRequest(name: name, fromSearch: search)
        )
        collections.insert(created, at: 0)
        return created
    }

    public func renameCollection(id: String, name: String) async {
        guard let client else { return }
        do {
            let updated = try await client.updateProject(
                id: id, body: UpdateProjectRequest(name: name)
            )
            if let idx = collections.firstIndex(where: { $0.projectId == id }) {
                collections[idx] = updated
            }
            if openCollection?.projectId == id {
                openCollection = updated
            }
        } catch {
            self.error = "Failed to rename collection: \(error)"
        }
    }

    public func deleteCollection(id: String) async {
        guard let client else { return }
        do {
            try await client.deleteProject(id: id)
            collections.removeAll { $0.projectId == id }
            if openCollection?.projectId == id {
                closeDetail()
            }
        } catch {
            self.error = "Failed to delete collection: \(error)"
        }
    }

    public func addAssets(collectionId: String, assetIds: [String]) async -> Int {
        guard let client else { return 0 }
        do {
            let added = try await client.addAssetsToProject(id: collectionId, assetIds: assetIds)
            // Refresh the collection metadata (asset count changed)
            if let idx = collections.firstIndex(where: { $0.projectId == collectionId }) {
                let refreshed = try await client.getProject(id: collectionId)
                collections[idx] = refreshed
            }
            return added
        } catch {
            self.error = "Failed to add assets: \(error)"
            return 0
        }
    }

    public func removeAssets(collectionId: String, assetIds: [String]) async -> Int {
        guard let client else { return 0 }
        do {
            let removed = try await client.removeAssetsFromProject(id: collectionId, assetIds: assetIds)
            collectionAssets.removeAll { assetIds.contains($0.assetId) }
            // Refresh collection metadata
            if let idx = collections.firstIndex(where: { $0.projectId == collectionId }) {
                let refreshed = try await client.getProject(id: collectionId)
                collections[idx] = refreshed
            }
            return removed
        } catch {
            self.error = "Failed to remove assets: \(error)"
            return 0
        }
    }

    // MARK: - Computed

    public var ownCollections: [Project] {
        collections.filter { $0.isOwn }
    }

    public var sharedCollections: [Project] {
        collections.filter { !$0.isOwn }
    }
}
