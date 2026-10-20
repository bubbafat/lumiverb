import Foundation

/// A clip's files by their names (ADR-024, `docs/data-names.md`), as the
/// apps ask for them: `GET /v1/assets/{id}/artifacts/{name}`, and as a
/// server says them (playback's `source`). Every server from v0.1.105
/// serves them by name.
public enum ClipFile: String, Sendable, CaseIterable {
    /// The clip's preview (a photo's, or a video's poster frame).
    case preview = "lumiverb.preview"
    /// The preview's thumbnail, for grids.
    case thumbnail = "lumiverb.preview.thumbnail"
    /// A video's 10-second preview.
    case videoPreview = "lumiverb.video.preview"
    /// A video's analysis proxy, the whole video.
    case analysisProxy = "lumiverb.video.analysis_proxy"

    /// Where the clip's file is, relative to the server.
    public func path(_ assetId: String) -> String {
        "/v1/assets/\(assetId)/artifacts/\(rawValue)"
    }
}
