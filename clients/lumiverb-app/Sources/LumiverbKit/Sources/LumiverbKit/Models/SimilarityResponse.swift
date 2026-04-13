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

/// Request body for `POST /v1/similar/search-by-vector`. For clients
/// that embed locally — iOS uses Apple Vision feature prints, macOS
/// uses CLIP via CoreML or Vision feature prints. The model_id /
/// model_version must match how the library was indexed, otherwise
/// the server's vector lookup returns no results (different vector
/// spaces).
///
/// **Hybrid mode**: when `imageB64` is also supplied, the server runs
/// face detection on the image, embeds each face with ArcFace, and
/// fuses the per-face identity matches with the scene-vector cosine
/// results via Reciprocal Rank Fusion. The bytes are additive — the
/// scene `vector` is still the primary signal — so legacy callers
/// that only set `vector` keep working unchanged. Pass the same image
/// you embedded, downscaled to a face-detection-friendly size (~768px
/// max edge) to keep upload bandwidth bounded.
public struct VectorSimilarityRequest: Encodable, Sendable {
    public let libraryId: String
    public let vector: [Float]
    public let modelId: String
    public let modelVersion: String
    public let limit: Int
    public let offset: Int
    public let imageB64: String?

    public init(
        libraryId: String,
        vector: [Float],
        modelId: String,
        modelVersion: String,
        limit: Int = 20,
        offset: Int = 0,
        imageB64: String? = nil
    ) {
        self.libraryId = libraryId
        self.vector = vector
        self.modelId = modelId
        self.modelVersion = modelVersion
        self.limit = limit
        self.offset = offset
        self.imageB64 = imageB64
    }
}

/// Response from `POST /v1/similar/search-by-image` (and search-by-vector).
/// Has `hits` + `total` but no `source_asset_id` (the source is the
/// uploaded image, not a stored asset).
public struct ImageSimilarityResponse: Decodable, Sendable {
    public let hits: [SimilarHit]
    public let total: Int
}
