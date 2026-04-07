import Foundation

/// A library as returned by `GET /v1/libraries`.
public struct Library: Decodable, Identifiable, Sendable {
    public let libraryId: String
    public let name: String
    public let rootPath: String
    public let createdAt: String

    public var id: String { libraryId }
}

/// Response from `GET /v1/libraries`.
public typealias LibraryListResponse = [Library]
