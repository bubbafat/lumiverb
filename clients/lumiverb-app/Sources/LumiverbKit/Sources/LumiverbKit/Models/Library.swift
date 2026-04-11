import Foundation

/// A library as returned by `GET /v1/libraries`.
public struct Library: Decodable, Identifiable, Sendable {
    public let libraryId: String
    public let name: String
    public let rootPath: String
    public let lastScanAt: String?
    public let status: String?
    public let isPublic: Bool?
    public let coverAssetId: String?

    public var id: String { libraryId }
}

/// Response from `GET /v1/libraries` is a JSON array.
public typealias LibraryListResponse = [Library]

/// Request body for `POST /v1/libraries`. APIClient's encoder converts the
/// camelCase field names to the snake_case the server expects.
public struct CreateLibraryRequest: Encodable, Sendable {
    public let name: String
    public let rootPath: String

    public init(name: String, rootPath: String) {
        self.name = name
        self.rootPath = rootPath
    }
}

/// Request body for `PATCH /v1/libraries/{id}`. All fields optional — the
/// server only updates fields that are explicitly present. Swift's default
/// `Encodable` skips `nil` fields entirely, which matches "don't touch this
/// attribute" semantics.
public struct LibraryUpdateRequest: Encodable, Sendable {
    public let name: String?
    public let rootPath: String?
    public let isPublic: Bool?

    public init(
        name: String? = nil,
        rootPath: String? = nil,
        isPublic: Bool? = nil
    ) {
        self.name = name
        self.rootPath = rootPath
        self.isPublic = isPublic
    }
}

// MARK: - Path filters (library-scoped)

/// Request body for `POST /v1/libraries/{id}/filters`.
///
/// `trashMatching` is only meaningful for `type = "exclude"` — when true,
/// any assets already indexed under the new pattern are soft-trashed at
/// filter-create time. The UI previews the count first so the user can
/// opt in explicitly.
public struct CreateLibraryFilterRequest: Encodable, Sendable {
    public let type: String   // "include" | "exclude"
    public let pattern: String
    public let trashMatching: Bool

    public init(type: String, pattern: String, trashMatching: Bool = false) {
        self.type = type
        self.pattern = pattern
        self.trashMatching = trashMatching
    }
}

/// Response from `POST /v1/libraries/{id}/filters`. Mirrors the server's
/// `LibraryFilterItemWithType` — includes the `type` discriminator (which
/// is absent in `FilterItem` because list responses split by type) and the
/// count of assets trashed by an exclude-with-trash_matching request.
public struct LibraryFilterItemWithType: Decodable, Sendable {
    public let filterId: String
    public let type: String
    public let pattern: String
    public let createdAt: String
    public let trashedCount: Int
}

/// Request body for `POST /v1/libraries/{id}/filters/preview`.
public struct PreviewFilterRequest: Encodable, Sendable {
    public let type: String
    public let pattern: String

    public init(type: String, pattern: String) {
        self.type = type
        self.pattern = pattern
    }
}

/// Response from `POST /v1/libraries/{id}/filters/preview`.
public struct PreviewFilterResponse: Decodable, Sendable {
    public let matchingAssetCount: Int
}
