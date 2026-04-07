import Foundation

/// A single similarity result from `GET /v1/similar`.
public struct SimilarHit: Decodable, Identifiable, Sendable {
    public let assetId: String
    public let relPath: String
    public let thumbnailKey: String?
    public let proxyKey: String?
    public let distance: Double
    public let mediaType: String?
    public let fileSize: Int?
    public let width: Int?
    public let height: Int?

    public var id: String { assetId }
}

/// Response from `GET /v1/similar`.
public struct SimilarityResponse: Decodable, Sendable {
    public let sourceAssetId: String
    public let hits: [SimilarHit]
    public let total: Int
    public let embeddingAvailable: Bool
}
