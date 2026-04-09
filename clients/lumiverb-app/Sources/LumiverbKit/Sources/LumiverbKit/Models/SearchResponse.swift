import Foundation

/// A single search result from `GET /v1/search`.
public struct SearchHit: Decodable, Identifiable, Sendable {
    public let type: String
    public let assetId: String
    public let libraryId: String?
    public let libraryName: String?
    public let relPath: String
    public let thumbnailKey: String?
    public let proxyKey: String?
    public let description: String
    public let tags: [String]
    public let score: Double
    public let source: String
    public let cameraMake: String?
    public let cameraModel: String?
    public let sceneId: String?
    public let startMs: Int?
    public let endMs: Int?
    public let mediaType: String?
    public let fileSize: Int?
    public let durationSec: Double?
    public let width: Int?
    public let height: Int?
    public let takenAt: String?
    public let snippet: String?
    public let language: String?

    public var id: String {
        // Scenes and transcripts can share asset_id, so include type + scene_id
        if let sceneId {
            return "\(assetId)-\(type)-\(sceneId)"
        }
        return assetId
    }

    /// Aspect ratio (width / height), defaulting to 1.0 when dimensions
    /// are missing. Used by the justified-row grid layout.
    public var aspectRatio: Double {
        guard let w = width, let h = height, h > 0 else { return 1.0 }
        return Double(w) / Double(h)
    }
}

/// Response from `GET /v1/search`.
public struct SearchResponse: Decodable, Sendable {
    public let query: String
    public let hits: [SearchHit]
    public let total: Int
    public let source: String
}
