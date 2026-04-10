import Foundation

// MARK: - Visibility

/// Collection visibility levels. Matches server `_VALID_VISIBILITIES`.
public enum CollectionVisibility: String, Codable, Sendable, Equatable, CaseIterable {
    case `private` = "private"
    case shared = "shared"
    case `public` = "public"
}

// MARK: - Sort order

/// Sort order for assets within a collection. Matches server `_VALID_SORT_ORDERS`.
public enum CollectionSortOrder: String, Codable, Sendable, Equatable, CaseIterable {
    case manual
    case addedAt = "added_at"
    case takenAt = "taken_at"
}

// MARK: - Collection

/// A single collection as returned by `GET /v1/collections` and
/// `GET /v1/collections/{id}`. Matches `CollectionItem` on the server.
public struct AssetCollection: Codable, Sendable, Equatable, Identifiable {
    public let collectionId: String
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

    public var id: String { collectionId }

    public var isOwn: Bool { ownership == "own" }

    public var parsedVisibility: CollectionVisibility {
        CollectionVisibility(rawValue: visibility) ?? .private
    }

    public var parsedSortOrder: CollectionSortOrder {
        CollectionSortOrder(rawValue: sortOrder) ?? .manual
    }
}

// MARK: - List response

public struct CollectionListResponse: Codable, Sendable {
    public let items: [AssetCollection]
}

// MARK: - Collection asset

/// An asset within a collection, as returned by
/// `GET /v1/collections/{id}/assets`.
public struct CollectionAsset: Codable, Sendable, Equatable, Identifiable {
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

public struct CollectionAssetsResponse: Codable, Sendable {
    public let items: [CollectionAsset]
    public let nextCursor: String?
}

// MARK: - Request bodies

public struct CreateCollectionRequest: Encodable, Sendable {
    public let name: String
    public let description: String?
    public let sortOrder: String
    public let visibility: String
    public let assetIds: [String]?

    public init(
        name: String,
        description: String? = nil,
        sortOrder: CollectionSortOrder = .manual,
        visibility: CollectionVisibility = .private,
        assetIds: [String]? = nil
    ) {
        self.name = name
        self.description = description
        self.sortOrder = sortOrder.rawValue
        self.visibility = visibility.rawValue
        self.assetIds = assetIds
    }
}

public struct UpdateCollectionRequest: Encodable, Sendable {
    public let name: String?
    public let description: String?
    public let visibility: String?
    public let sortOrder: String?
    public let coverAssetId: String?

    public init(
        name: String? = nil,
        description: String? = nil,
        visibility: CollectionVisibility? = nil,
        sortOrder: CollectionSortOrder? = nil,
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
