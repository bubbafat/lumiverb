import Foundation
import AVFoundation

/// Generates 10-second H.264/AAC MP4 video previews using AVAssetExportSession.
///
/// Pure Apple implementation — no FFmpeg dependency. Output is compatible with
/// the CLI-generated previews (H.264, 720p max, faststart).
enum VideoPreviewGenerator {

    /// Generate a preview clip from a local video file.
    ///
    /// - Parameters:
    ///   - sourceURL: Local file URL of the source video
    ///   - duration: Maximum preview duration in seconds (default 10)
    /// - Returns: MP4 data suitable for upload to the server
    static func generatePreview(sourceURL: URL, duration: TimeInterval = 10) async throws -> Data {
        let asset = AVURLAsset(url: sourceURL)

        // Get actual duration to cap the preview range
        let assetDuration = try await asset.load(.duration)
        let previewDuration = min(duration, assetDuration.seconds)

        guard let session = AVAssetExportSession(
            asset: asset,
            presetName: AVAssetExportPreset1280x720
        ) else {
            throw VideoPreviewError.exportSessionCreationFailed
        }

        let tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("lumiverb-preview-\(UUID().uuidString).mp4")

        session.outputURL = tempURL
        session.outputFileType = .mp4
        session.timeRange = CMTimeRange(
            start: .zero,
            duration: CMTime(seconds: previewDuration, preferredTimescale: 600)
        )
        session.shouldOptimizeForNetworkUse = true  // faststart equivalent

        // Use the modern async throwing API (macOS 15+) with fallback
        if #available(macOS 15, *) {
            do {
                try await session.export(to: tempURL, as: .mp4)
            } catch {
                try? FileManager.default.removeItem(at: tempURL)
                throw VideoPreviewError.exportFailed(error.localizedDescription)
            }
        } else {
            await session.export()
            guard session.status == .completed else {
                let message = session.error?.localizedDescription ?? "Unknown export error"
                try? FileManager.default.removeItem(at: tempURL)
                throw VideoPreviewError.exportFailed(message)
            }
        }

        let data = try Data(contentsOf: tempURL)
        try? FileManager.default.removeItem(at: tempURL)
        return data
    }
}

enum VideoPreviewError: Error, CustomStringConvertible {
    case exportSessionCreationFailed
    case exportFailed(String)

    var description: String {
        switch self {
        case .exportSessionCreationFailed:
            return "VideoPreview: failed to create export session"
        case .exportFailed(let msg):
            return "VideoPreview: export failed — \(msg)"
        }
    }
}
