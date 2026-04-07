import Foundation

/// Full asset detail from `GET /v1/assets/{asset_id}`.
public struct AssetDetail: Decodable, Identifiable, Sendable {
    public let assetId: String
    public let libraryId: String
    public let relPath: String
    public let mediaType: String
    public let status: String
    public let proxyKey: String?
    public let thumbnailKey: String?
    public let videoPreviewKey: String?
    public let durationSec: Double?
    public let width: Int?
    public let height: Int?
    public let sha256: String?
    public let exifExtractedAt: String?
    public let cameraMake: String?
    public let cameraModel: String?
    public let takenAt: String?
    public let gpsLat: Double?
    public let gpsLon: Double?
    public let iso: Int?
    public let exposureTimeUs: Int?
    public let aperture: Double?
    public let focalLength: Double?
    public let focalLength35mm: Double?
    public let lensModel: String?
    public let flashFired: Bool?
    public let orientation: Int?
    public let aiDescription: String?
    public let aiTags: [String]?
    public let ocrText: String?
    public let transcriptSrt: String?
    public let transcriptLanguage: String?
    public let transcribedAt: String?
    public let note: String?
    public let noteAuthor: String?
    public let noteUpdatedAt: String?

    public var id: String { assetId }

    /// Whether this asset is a video.
    public var isVideo: Bool { mediaType == "video" }

    /// Camera description combining make and model.
    public var cameraDescription: String? {
        switch (cameraMake, cameraModel) {
        case let (make?, model?):
            // Avoid "Canon Canon EOS R5" — if model contains make, just use model
            if model.lowercased().hasPrefix(make.lowercased()) {
                return model
            }
            return "\(make) \(model)"
        case let (_, model?):
            return model
        case let (make?, _):
            return make
        default:
            return nil
        }
    }

    /// Human-readable exposure time (e.g., "1/250s").
    public var exposureDescription: String? {
        guard let us = exposureTimeUs, us > 0 else { return nil }
        let seconds = Double(us) / 1_000_000.0
        if seconds >= 1 {
            return String(format: "%.1fs", seconds)
        } else {
            let denom = Int(round(1.0 / seconds))
            return "1/\(denom)s"
        }
    }

    /// Formatted dimensions string (e.g., "6000 x 4000").
    public var dimensionsDescription: String? {
        guard let w = width, let h = height else { return nil }
        return "\(w) x \(h)"
    }

    /// Human-readable file size from the rel_path filename.
    public var filename: String {
        (relPath as NSString).lastPathComponent
    }
}
