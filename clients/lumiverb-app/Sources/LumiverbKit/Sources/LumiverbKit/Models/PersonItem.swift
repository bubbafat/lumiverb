import Foundation

// MARK: - Person

/// A person from `GET /v1/people`.
public struct PersonItem: Decodable, Identifiable, Hashable, Sendable {
    public let personId: String
    public let displayName: String
    public let faceCount: Int
    public let representativeFaceId: String?
    public let representativeAssetId: String?
    public let confirmationCount: Int

    public var id: String { personId }
}

/// Response from `GET /v1/people` and `GET /v1/people/dismissed`.
public struct PersonListResponse: Decodable, Sendable {
    public let items: [PersonItem]
    public let nextCursor: String?
}

// MARK: - Person mutation requests

/// Body for `POST /v1/people`.
///
/// Optionally pre-assigns the new person to a set of faces (used by the
/// "name this cluster" flow that creates a new person from a whole cluster
/// at once). Pass an empty / nil array to create an empty person.
public struct PersonCreateRequest: Encodable, Sendable {
    public let displayName: String
    public let faceIds: [String]?

    public init(displayName: String, faceIds: [String]? = nil) {
        self.displayName = displayName
        self.faceIds = faceIds
    }
}

/// Body for `PATCH /v1/people/{person_id}`.
public struct PersonUpdateRequest: Encodable, Sendable {
    public let displayName: String

    public init(displayName: String) {
        self.displayName = displayName
    }
}

/// Body for `POST /v1/people/{person_id}/merge` — merge `sourcePersonId`
/// *into* the URL person, then delete the source.
public struct MergeRequest: Encodable, Sendable {
    public let sourcePersonId: String

    public init(sourcePersonId: String) {
        self.sourcePersonId = sourcePersonId
    }
}

/// Body for `POST /v1/people/{person_id}/undismiss`. Restoring a dismissed
/// person requires giving them a real display name.
public struct UndismissRequest: Encodable, Sendable {
    public let displayName: String

    public init(displayName: String) {
        self.displayName = displayName
    }
}

// MARK: - Person faces

/// One face attached to a person — used by both
/// `GET /v1/people/{id}/faces` and `GET /v1/faces/clusters/{i}/faces`.
/// Server-side this is the `PersonFaceItem` Pydantic model and the
/// `ClusterFacesResponse.items` list reuses it directly.
public struct PersonFaceItem: Decodable, Identifiable, Sendable {
    public let faceId: String
    public let assetId: String
    public let boundingBox: FaceBoundingBox?
    public let detectionConfidence: Float?
    public let relPath: String?

    public var id: String { faceId }
}

/// Response from `GET /v1/people/{person_id}/faces`. Cursor-paginated.
public struct PersonFacesResponse: Decodable, Sendable {
    public let items: [PersonFaceItem]
    public let nextCursor: String?
}

// MARK: - Nearest people

/// One row from `GET /v1/people/{id}/nearest` and
/// `GET /v1/faces/clusters/{i}/nearest-people`. Both endpoints sort by
/// `distance` ascending (closest = best match), so the natural order is
/// already the most-likely-match-first list to display.
public struct NearestPersonItem: Decodable, Identifiable, Sendable {
    public let personId: String
    public let displayName: String
    public let faceCount: Int
    public let distance: Float

    public var id: String { personId }
}
