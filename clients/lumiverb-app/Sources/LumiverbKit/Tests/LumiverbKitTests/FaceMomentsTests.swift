import AVFoundation
import XCTest
@testable import LumiverbKit

/// Faces in videos on the Swift side: when a person is seen (spans), a
/// person's clips (one a clip, a video with its moments), the new fields as
/// the server sends them, and the lightbox's player going to a moment in a
/// real video.
final class FaceMomentsTests: XCTestCase {

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    // MARK: - Moments

    func testTimesSeenCloseTogetherAreOneSpanInOrder() {
        XCTAssertEqual(FaceMoments.spans([40_000, 1_000, 4_000, 9_000, 61_000]), [
            FaceMoment(startMs: 1_000, endMs: 9_000),
            FaceMoment(startMs: 40_000, endMs: 40_000),
            FaceMoment(startMs: 61_000, endMs: 61_000),
        ])
        XCTAssertEqual(FaceMoments.spans(nil), [])
    }

    func testASpanReadsAsClockTimes() {
        XCTAssertEqual(FaceMoments.clock(83_000), "1:23")
        XCTAssertEqual(FaceMoments.clock(3_723_000), "1:02:03")
        XCTAssertEqual(FaceMoment(startMs: 4_000, endMs: 9_500).label, "0:04–0:09")
        XCTAssertEqual(FaceMoment(startMs: 4_000, endMs: 4_000).label, "0:04")
        XCTAssertEqual(FaceMoment(startMs: 95_000, endMs: 95_000).startSeconds, 95)
    }

    // MARK: - A person's clips

    private func face(_ json: String) throws -> PersonFaceItem {
        try decoder.decode(PersonFaceItem.self, from: Data(json.utf8))
    }

    func testOneClipEachAVideoWithAllItsMoments() throws {
        let clips = PersonClip.group([
            try face(#"{"face_id": "p1", "asset_id": "photo", "media_type": "image"}"#),
            try face(#"{"face_id": "v1", "asset_id": "video", "media_type": "video", "frame_ms": 8000, "seen_ms": [4000, 8000]}"#),
            try face(#"{"face_id": "v2", "asset_id": "video", "media_type": "video", "frame_ms": 70000, "seen_ms": [70000]}"#),
        ])
        XCTAssertEqual(clips.map(\.assetId), ["photo", "video"])
        XCTAssertEqual(clips.map { $0.faces.count }, [1, 2])
        XCTAssertFalse(clips[0].isVideo)
        XCTAssertNil(clips[0].startSeconds)
        XCTAssertEqual(clips[1].moments, [FaceMoment(startMs: 4_000, endMs: 8_000), FaceMoment(startMs: 70_000, endMs: 70_000)])
        XCTAssertEqual(clips[1].startSeconds, 8)  // where their face is shown
    }

    func testAnOlderServersFacesAreAPhotosWithNoMoments() throws {
        let old = try face(#"{"face_id": "f", "asset_id": "a", "bounding_box": null, "detection_confidence": 0.9, "rel_path": "a.jpg"}"#)
        XCTAssertFalse(old.isVideo)
        XCTAssertNil(old.frameMs)
        XCTAssertEqual(PersonClip.group([old])[0].moments, [])
    }

    func testAVideosFacesAndDetailSayWhenAndHowMany() throws {
        let item = try decoder.decode(FaceListItem.self, from: Data(#"""
            {"face_id": "f", "bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}, "detection_confidence": 0.9,
             "person": null, "frame_ms": 8000, "seen_ms": [4000, 8000]}
            """#.utf8))
        XCTAssertEqual(item.frameMs, 8_000)
        XCTAssertEqual(item.seenMs, [4_000, 8_000])
        let link = try decoder.decode(PlaybackLink.self, from: Data(#"""
            {"url": "/v1/stream/tok", "expires_at": "2026-10-11T12:00:00Z", "source": "lumiverb.video.analysis_proxy",
             "max_seconds": null, "ready": true}
            """#.utf8))
        XCTAssertEqual(link.url, "/v1/stream/tok")
        XCTAssertTrue(link.ready)
        XCTAssertNil(link.maxSeconds)
        XCTAssertTrue(link.isWhole)  // an older server's: the analysis proxy is the whole clip
    }

    func testAShortClipsPreviewIsTheWholeClip() throws {
        func link(_ json: String) throws -> PlaybackLink {
            try decoder.decode(PlaybackLink.self, from: Data(json.utf8))
        }
        let short = try link(#"{"url": "/v1/stream/t", "source": "lumiverb.video.preview", "max_seconds": null, "ready": true, "whole": true}"#)
        XCTAssertTrue(short.isWhole)  // a 1-second clip: no "10s preview" badge
        let long = try link(#"{"url": "/v1/stream/t", "source": "lumiverb.video.preview", "max_seconds": null, "ready": true, "whole": false}"#)
        XCTAssertFalse(long.isWhole)
        let older = try link(#"{"url": "/v1/stream/t", "source": "lumiverb.video.preview", "max_seconds": null, "ready": true}"#)
        XCTAssertFalse(older.isWhole)
    }

    func testEachAskToPlayAMomentIsItsOwn() {
        let a = VideoStart(assetId: "v", seconds: 4, play: true)
        let b = VideoStart(assetId: "v", seconds: 4, play: true)
        XCTAssertNotEqual(a, b)  // the same moment asked again plays again
    }

    // MARK: - The player at a moment (a real video)

    private func fixture(_ name: String) throws -> URL {
        guard let url = Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Fixtures") else {
            throw XCTSkip("fixture \(name) missing")
        }
        return url
    }

    @MainActor
    func testThePlayerGoesToAMomentAndPlaysWhenAsked() async throws {
        let vm = VideoPlayerViewModel()
        vm.player = AVPlayer(url: try fixture("transcribe-english-brownfox-480.mov"))
        vm.source = .local
        await vm.seek(VideoStart(assetId: "a", seconds: 1.5))
        XCTAssertEqual(vm.player!.currentTime().seconds, 1.5, accuracy: 0.05)
        XCTAssertFalse(vm.hasStartedPlaying)  // opened there; waits for play

        await vm.seek(VideoStart(assetId: "a", seconds: 0.5, play: true))
        XCTAssertTrue(vm.hasStartedPlaying)
        vm.tearDown()
    }

    @MainActor
    func testAMomentPastTheEndLeavesThePlayerWhereItIs() async throws {
        let vm = VideoPlayerViewModel()
        vm.player = AVPlayer(url: try fixture("transcribe-english-brownfox-480.mov"))
        vm.source = .serverPreview
        await vm.seek(VideoStart(assetId: "a", seconds: 3_600))
        XCTAssertEqual(vm.player!.currentTime().seconds, 0, accuracy: 0.01)
        XCTAssertEqual(vm.unreachableMoment, 3_600)  // said where the video is
        XCTAssertTrue(vm.isPreview)
        await vm.seek(VideoStart(assetId: "a", seconds: 1))
        XCTAssertNil(vm.unreachableMoment)  // one it reaches clears it
        vm.tearDown()
    }
}
