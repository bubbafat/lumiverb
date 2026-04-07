import Foundation

/// A directory entry from `GET /v1/libraries/{library_id}/directories`.
public struct DirectoryNode: Decodable, Identifiable, Sendable {
    public let name: String
    public let path: String
    public let assetCount: Int

    public var id: String { path }
}
