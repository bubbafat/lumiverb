import Foundation

/// When a person is seen in a video, as spans to show and jump to. The
/// twin of the web's `src/ui/web/src/lib/faceMoments.ts`.
public struct FaceMoment: Equatable, Hashable, Sendable {
    public let startMs: Int
    public let endMs: Int

    public init(startMs: Int, endMs: Int) {
        self.startMs = startMs
        self.endMs = endMs
    }

    public var startSeconds: Double { Double(startMs) / 1000 }

    /// "0:04" or "0:04–0:08".
    public var label: String {
        endMs > startMs ? "\(FaceMoments.clock(startMs))–\(FaceMoments.clock(endMs))" : FaceMoments.clock(startMs)
    }
}

public enum FaceMoments {
    /// Times seen this close together are one span: the person is on screen between them.
    public static let sameMomentMs = 10_000

    /// The times a person was seen (ms, any order) as spans, in order.
    public static func spans(_ seenMs: [Int]?, within: Int = sameMomentMs) -> [FaceMoment] {
        var out: [FaceMoment] = []
        for t in (seenMs ?? []).sorted() {
            if let last = out.last, t - last.endMs <= within {
                out[out.count - 1] = FaceMoment(startMs: last.startMs, endMs: t)
            } else {
                out.append(FaceMoment(startMs: t, endMs: t))
            }
        }
        return out
    }

    /// 83_000 → "1:23"; an hour or more → "1:02:03".
    public static func clock(_ ms: Int) -> String {
        let total = max(0, ms / 1000)
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, s) : String(format: "%d:%02d", m, s)
    }
}

/// One of a person's clips: their faces grouped by clip, in the order first
/// listed. A video's faces (usually one: the person seen) bring every moment.
public struct PersonClip: Identifiable, Sendable {
    /// The first listed: the one shown.
    public let face: PersonFaceItem
    public let faces: [PersonFaceItem]
    public let moments: [FaceMoment]

    public var id: String { face.assetId }
    public var assetId: String { face.assetId }
    public var isVideo: Bool { face.isVideo }

    /// Where a video opens: the moment the person's face is shown (its crop's), else their first.
    public var startSeconds: Double? {
        guard isVideo else { return nil }
        if let ms = face.frameMs { return Double(ms) / 1000 }
        return moments.first?.startSeconds
    }

    public static func group(_ faces: [PersonFaceItem]) -> [PersonClip] {
        var order: [String] = []
        var byClip: [String: [PersonFaceItem]] = [:]
        for face in faces {
            if byClip[face.assetId] == nil { order.append(face.assetId) }
            byClip[face.assetId, default: []].append(face)
        }
        return order.map { id in
            let group = byClip[id]!
            let moments = group[0].isVideo
                ? FaceMoments.spans(group.flatMap { $0.seenMs ?? ($0.frameMs.map { [$0] } ?? []) })
                : []
            return PersonClip(face: group[0], faces: group, moments: moments)
        }
    }
}

/// A request to play a video from a moment (`BrowseState.videoStart`).
public struct VideoStart: Equatable, Sendable {
    public let assetId: String
    public let seconds: Double
    /// Play from there at once (a moment picked in the lightbox), or open
    /// there and wait for play (a clip opened at a person's moment).
    public let play: Bool
    /// Each request is its own, so asking for the same moment again plays it again.
    public let id: UUID

    public init(assetId: String, seconds: Double, play: Bool = false, id: UUID = UUID()) {
        self.assetId = assetId
        self.seconds = seconds
        self.play = play
        self.id = id
    }
}

/// `GET /v1/assets/{id}/playback`: a signed link a player streams.
public struct PlaybackLink: Decodable, Sendable {
    /// "/v1/stream/<token>", relative to the server.
    public let url: String
    public let expiresAt: String?
    /// "analysis_proxy" (the whole video, up to `maxSeconds`) or "preview" (the scan's 10-second clip).
    public let source: String
    public let maxSeconds: Int?
    /// False while the capped copy is made: ask again shortly.
    public let ready: Bool
}
