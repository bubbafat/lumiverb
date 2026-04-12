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

    /// Aspect ratio (width / height), defaulting to 1.0 when dimensions
    /// are missing. Used by the justified-row grid layout.
    public var aspectRatio: Double {
        guard let w = width, let h = height, h > 0 else { return 1.0 }
        return Double(w) / Double(h)
    }
}

/// Response from `GET /v1/similar`.
public struct SimilarityResponse: Decodable, Sendable {
    public let sourceAssetId: String
    public let hits: [SimilarHit]
    public let total: Int
    public let embeddingAvailable: Bool
}

/// Request body for `POST /v1/similar/search-by-image`. The client
/// pre-resizes the image and sends it as base64-encoded JPEG bytes.
public struct ImageSimilarityRequest: Encodable, Sendable {
    public let libraryId: String
    public let imageB64: String
    public let limit: Int
    public let offset: Int

    public init(
        libraryId: String,
        imageB64: String,
        limit: Int = 20,
        offset: Int = 0
    ) {
        self.libraryId = libraryId
        self.imageB64 = imageB64
        self.limit = limit
        self.offset = offset
    }
}

/// Response from `POST /v1/similar/search-by-image` (and search-by-vector).
/// Has `hits` + `total` but no `source_asset_id` (the source is the
/// uploaded image, not a stored asset).
public struct ImageSimilarityResponse: Decodable, Sendable {
    public let hits: [SimilarHit]
    public let total: Int
}
