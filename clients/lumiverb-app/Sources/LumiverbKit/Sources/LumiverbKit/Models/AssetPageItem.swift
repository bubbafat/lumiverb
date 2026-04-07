import Foundation

/// A single asset in a paginated listing from `GET /v1/assets/page`.
public struct AssetPageItem: Decodable, Identifiable, Sendable {
    public let assetId: String
    public let relPath: String
    public let fileSize: Int
    public let fileMtime: String?
    public let sha256: String?
    public let mediaType: String
    public let width: Int?
    public let height: Int?
    public let takenAt: String?
    public let status: String
    public let durationSec: Double?
    public let cameraMake: String?
    public let cameraModel: String?
    public let iso: Int?
    public let aperture: Double?
    public let focalLength: Double?
    public let focalLength35mm: Double?
    public let lensModel: String?
    public let flashFired: Bool?
    public let gpsLat: Double?
    public let gpsLon: Double?
    public let faceCount: Int?
    public let createdAt: String?

    public var id: String { assetId }

    /// Whether this asset is a video.
    public var isVideo: Bool { mediaType == "video" }

    /// Aspect ratio (width / height), defaulting to 1.0 if dimensions unknown.
    public var aspectRatio: Double {
        guard let w = width, let h = height, h > 0 else { return 1.0 }
        return Double(w) / Double(h)
    }
}

/// Response from `GET /v1/assets/page`.
public struct AssetPageResponse: Decodable, Sendable {
    public let items: [AssetPageItem]
    public let nextCursor: String?
}
