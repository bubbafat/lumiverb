import Foundation

// MARK: - Visibility

/// Project visibility levels. Matches server `_VALID_VISIBILITIES`
/// (`src/server/api/routers/projects.py`).
public enum ProjectVisibility: String, Codable, Sendable, Equatable, CaseIterable {
    case `private` = "private"
    case shared = "shared"
    case `public` = "public"
}

// MARK: - Sort order

/// Sort order for assets within a project. Matches server `_VALID_SORT_ORDERS`.
public enum ProjectSortOrder: String, Codable, Sendable, Equatable, CaseIterable {
    case manual
    case addedAt = "added_at"
    case takenAt = "taken_at"
}

// MARK: - Project

/// A single project as returned by `GET /v1/projects` and
/// `GET /v1/projects/{id}`. Matches `ProjectItem` on the server.
public struct Project: Codable, Sendable, Equatable, Identifiable {
    public let projectId: String
    public let name: String
    public let description: String?
    public let coverAssetId: String?
    public let ownerUserId: String?
    public let visibility: String
    public let ownership: String  // "own" | "shared"
    public let sortOrder: String
    public let assetCount: Int
    public let createdAt: String
    public let updatedAt: String

    public var id: String { projectId }

    public var isOwn: Bool { ownership == "own" }

    public var parsedVisibility: ProjectVisibility {
        ProjectVisibility(rawValue: visibility) ?? .private
    }

    public var parsedSortOrder: ProjectSortOrder {
        ProjectSortOrder(rawValue: sortOrder) ?? .manual
    }
}

// MARK: - List response

public struct ProjectListResponse: Codable, Sendable {
    public let items: [Project]
}

// MARK: - Project asset

/// An asset within a project, as returned by
/// `GET /v1/projects/{id}/assets`.
public struct ProjectAsset: Codable, Sendable, Equatable, Identifiable {
    public let assetId: String
    public let relPath: String
    public let fileSize: Int
    public let mediaType: String
    public let width: Int?
    public let height: Int?
    public let takenAt: String?
    public let status: String
    public let durationSec: Double?
    public let cameraMake: String?
    public let cameraModel: String?

    public var id: String { assetId }

    public var isVideo: Bool { mediaType == "video" }

    public var aspectRatio: CGFloat {
        guard let w = width, let h = height, h > 0 else { return 1.0 }
        return CGFloat(w) / CGFloat(h)
    }
}

public struct ProjectAssetsResponse: Codable, Sendable {
    public let items: [ProjectAsset]
    public let nextCursor: String?
}

// MARK: - Request bodies

/// `POST /v1/projects`. A project is an explicit list of clips: `assetIds`,
/// or `fromSearch`, the clips a search matches when it's made (it doesn't
/// follow the search afterwards). Give one or neither, not both.
public struct CreateProjectRequest: Encodable, Sendable {
    public let name: String
    public let description: String?
    public let sortOrder: String
    public let visibility: String
    public let assetIds: [String]?
    public let fromSearch: SavedQueryV2?

    public init(
        name: String,
        description: String? = nil,
        sortOrder: ProjectSortOrder = .manual,
        visibility: ProjectVisibility = .private,
        assetIds: [String]? = nil,
        fromSearch: SavedQueryV2? = nil
    ) {
        self.name = name
        self.description = description
        self.sortOrder = sortOrder.rawValue
        self.visibility = visibility.rawValue
        self.assetIds = assetIds
        self.fromSearch = fromSearch
    }
}

/// `PATCH /v1/projects/{id}`. Only the fields set are sent.
public struct UpdateProjectRequest: Encodable, Sendable {
    public let name: String?
    public let description: String?
    public let visibility: String?
    public let sortOrder: String?
    public let coverAssetId: String?

    public init(
        name: String? = nil,
        description: String? = nil,
        visibility: ProjectVisibility? = nil,
        sortOrder: ProjectSortOrder? = nil,
        coverAssetId: String? = nil
    ) {
        self.name = name
        self.description = description
        self.visibility = visibility?.rawValue
        self.sortOrder = sortOrder?.rawValue
        self.coverAssetId = coverAssetId
    }
}

public struct AssetIdsRequest: Encodable, Sendable {
    public let assetIds: [String]

    public init(assetIds: [String]) {
        self.assetIds = assetIds
    }
}

public struct BatchAddResponse: Codable, Sendable {
    public let added: Int
}

public struct BatchRemoveResponse: Codable, Sendable {
    public let removed: Int
}
