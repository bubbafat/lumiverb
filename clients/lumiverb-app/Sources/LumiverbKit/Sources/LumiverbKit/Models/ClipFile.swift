import Foundation

/// A clip's files by their names (ADR-024, `docs/data-names.md`), as the
/// apps ask for them: `GET /v1/assets/{id}/artifacts/{name}`. Every server
/// from v0.1.105 serves them by name; before ADR-024's part 4 they were
/// asked for as `proxy`, `thumbnail`, `video_preview` and `analysis_proxy`.
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

    /// What a server says a file is (playback's `source`), in either
    /// spelling: its name, or the word a server used before ADR-024's
    /// part 4 (`analysis_proxy`, `preview` for the video preview).
    public init?(said: String) {
        switch said {
        case "analysis_proxy": self = .analysisProxy
        case "preview", "video_preview": self = .videoPreview
        default: self.init(rawValue: said)
        }
    }
}
