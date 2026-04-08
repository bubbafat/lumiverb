import Foundation

/// A person from `GET /v1/people`.
public struct PersonItem: Decodable, Identifiable, Sendable {
    public let personId: String
    public let displayName: String
    public let faceCount: Int
    public let representativeFaceId: String?
    public let representativeAssetId: String?
    public let confirmationCount: Int

    public var id: String { personId }
}

/// Response from `GET /v1/people`.
public struct PersonListResponse: Decodable, Sendable {
    public let items: [PersonItem]
    public let nextCursor: String?
}
