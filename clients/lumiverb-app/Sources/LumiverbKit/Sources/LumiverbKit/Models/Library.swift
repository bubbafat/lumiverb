import Foundation

/// A library as returned by `GET /v1/libraries`.
public struct Library: Decodable, Identifiable, Sendable {
    public let libraryId: String
    public let name: String
    public let rootPath: String
    public let lastScanAt: String?
    public let status: String?
    public let isPublic: Bool?

    public var id: String { libraryId }
}

/// Response from `GET /v1/libraries` is a JSON array.
public typealias LibraryListResponse = [Library]
