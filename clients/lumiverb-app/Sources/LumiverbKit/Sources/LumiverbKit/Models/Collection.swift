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
/// Saved query for a smart collection — filters + optional search text.
public struct SavedQuery: Codable, Sendable, Equatable {
    public let q: String?
    public let filters: [String: AnyCodable]
    public let libraryId: String?

    public init(q: String?, filters: [String: Any], libraryId: String?) {
        self.q = q
        self.filters = filters.mapValues { AnyCodable($0) }
        self.libraryId = libraryId
    }
}

/// Type-erased Codable wrapper for saved query filter values.
public struct AnyCodable: Codable, Sendable, Equatable {
    public nonisolated(unsafe) let value: Any

    public init(_ value: Any) { self.value = value }

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let b = try? container.decode(Bool.self) { value = b }
        else if let i = try? container.decode(Int.self) { value = i }
        else if let d = try? container.decode(Double.self) { value = d }
        else if let s = try? container.decode(String.self) { value = s }
        else if let a = try? container.decode([AnyCodable].self) { value = a.map(\.value) }
        else if let dict = try? container.decode([String: AnyCodable].self) { value = dict.mapValues(\.value) }
        else if container.decodeNil() { value = NSNull() }
        else { value = NSNull() }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch value {
        case let b as Bool: try container.encode(b)
        case let i as Int: try container.encode(i)
        case let d as Double: try container.encode(d)
        case let s as String: try container.encode(s)
        case let a as [Any]: try container.encode(a.map { AnyCodable($0) })
        case let dict as [String: Any]: try container.encode(dict.mapValues { AnyCodable($0) })
        default: try container.encodeNil()
        }
    }

    public static func == (lhs: AnyCodable, rhs: AnyCodable) -> Bool {
        String(describing: lhs.value) == String(describing: rhs.value)
    }
}

/// Collection type: static (manual) or smart (saved query).
public enum CollectionType: String, Codable, Sendable, Equatable {
    case `static` = "static"
    case smart = "smart"
}

public struct AssetCollection: Codable, Sendable, Equatable, Identifiable {
    public let collectionId: String
    public let name: String
    public let description: String?
    public let coverAssetId: String?
    public let ownerUserId: String?
    public let visibility: String
    public let ownership: String  // "own" | "shared"
    public let sortOrder: String
    public let type: String
    public let savedQuery: SavedQuery?
    public let assetCount: Int
    public let createdAt: String
    public let updatedAt: String

    public var id: String { collectionId }

    public var isOwn: Bool { ownership == "own" }

    public var isSmart: Bool { type == "smart" }

    public var parsedVisibility: CollectionVisibility {
        CollectionVisibility(rawValue: visibility) ?? .private
    }

    public var parsedSortOrder: CollectionSortOrder {
        CollectionSortOrder(rawValue: sortOrder) ?? .manual
    }

    // Default type to "static" for backwards compat with older servers
    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        collectionId = try container.decode(String.self, forKey: .collectionId)
        name = try container.decode(String.self, forKey: .name)
        description = try container.decodeIfPresent(String.self, forKey: .description)
        coverAssetId = try container.decodeIfPresent(String.self, forKey: .coverAssetId)
        ownerUserId = try container.decodeIfPresent(String.self, forKey: .ownerUserId)
        visibility = try container.decode(String.self, forKey: .visibility)
        ownership = try container.decode(String.self, forKey: .ownership)
        sortOrder = try container.decode(String.self, forKey: .sortOrder)
        type = try container.decodeIfPresent(String.self, forKey: .type) ?? "static"
        savedQuery = try container.decodeIfPresent(SavedQuery.self, forKey: .savedQuery)
        assetCount = try container.decode(Int.self, forKey: .assetCount)
        createdAt = try container.decode(String.self, forKey: .createdAt)
        updatedAt = try container.decode(String.self, forKey: .updatedAt)
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
    public let type: String
    public let savedQuery: SavedQueryV2?
    public let assetIds: [String]?

    public init(
        name: String,
        description: String? = nil,
        sortOrder: CollectionSortOrder = .manual,
        visibility: CollectionVisibility = .private,
        type: CollectionType = .static,
        savedQuery: SavedQueryV2? = nil,
        assetIds: [String]? = nil
    ) {
        self.name = name
        self.description = description
        self.sortOrder = sortOrder.rawValue
        self.visibility = visibility.rawValue
        self.type = type.rawValue
        self.savedQuery = savedQuery
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
