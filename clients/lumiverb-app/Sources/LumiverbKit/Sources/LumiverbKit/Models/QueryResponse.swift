import Foundation

/// Search context annotation on a query result.
public struct SearchContext: Decodable, Sendable, Equatable {
    public let score: Double
    public let hitType: String
    public let snippet: String?
    public let startMs: Int?
    public let endMs: Int?
}

/// A single item from GET /v1/query.
public struct QueryItem: Decodable, Identifiable, Sendable, Equatable {
    public let assetId: String
    public let libraryId: String
    public let libraryName: String
    public let relPath: String
    public let fileSize: Int
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
    public let thumbnailKey: String?
    public let proxyKey: String?
    public let createdAt: String?
    public let searchContext: SearchContext?

    public var id: String { assetId }
    public var isVideo: Bool { mediaType == "video" }
    public var aspectRatio: Double {
        guard let w = width, let h = height, h > 0 else { return 1.0 }
        return Double(w) / Double(h)
    }

    /// Convert to AssetPageItem for compatibility with existing views.
    public func toPageItem() -> AssetPageItem {
        AssetPageItem(
            assetId: assetId,
            relPath: relPath,
            fileSize: fileSize,
            fileMtime: nil,
            sha256: nil,
            mediaType: mediaType,
            width: width,
            height: height,
            takenAt: takenAt,
            status: status,
            durationSec: durationSec,
            cameraMake: cameraMake,
            cameraModel: cameraModel,
            iso: iso,
            aperture: aperture,
            focalLength: focalLength,
            focalLength35mm: focalLength35mm,
            lensModel: lensModel,
            flashFired: flashFired,
            gpsLat: gpsLat,
            gpsLon: gpsLon,
            faceCount: faceCount,
            createdAt: createdAt
        )
    }
}

/// Response from GET /v1/query.
public struct QueryResponse: Decodable, Sendable {
    public let items: [QueryItem]
    public let nextCursor: String?
    public let totalEstimate: Int?
}
