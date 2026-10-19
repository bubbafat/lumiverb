import XCTest
@testable import LumiverbKit

/// A clip's files by their names (ADR-024 phase 1 part 2): asked for by
/// name, and playback's source read in either spelling.
final class ClipFileTests: XCTestCase {
    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    func testEachFileIsAskedForByItsName() {
        XCTAssertEqual(ClipFile.preview.path("ast_1"), "/v1/assets/ast_1/artifacts/lumiverb.preview")
        XCTAssertEqual(ClipFile.thumbnail.path("ast_1"), "/v1/assets/ast_1/artifacts/lumiverb.preview.thumbnail")
        XCTAssertEqual(ClipFile.videoPreview.path("ast_1"), "/v1/assets/ast_1/artifacts/lumiverb.video.preview")
        XCTAssertEqual(ClipFile.analysisProxy.path("ast_1"),
                       "/v1/assets/ast_1/artifacts/lumiverb.video.analysis_proxy")
        // The grid's and the lightbox's images.
        XCTAssertEqual(AuthenticatedImageView.ImageType.thumbnail.file, .thumbnail)
        XCTAssertEqual(AuthenticatedImageView.ImageType.proxy.file, .preview)
    }

    func testWhatAServerSaysAFileIsInEitherSpelling() {
        XCTAssertEqual(ClipFile(said: "analysis_proxy"), .analysisProxy)  // before ADR-024's part 4
        XCTAssertEqual(ClipFile(said: "preview"), .videoPreview)
        XCTAssertEqual(ClipFile(said: "lumiverb.video.analysis_proxy"), .analysisProxy)  // from part 4
        XCTAssertEqual(ClipFile(said: "lumiverb.video.preview"), .videoPreview)
        XCTAssertNil(ClipFile(said: "something else"))
    }

    func testPlaybackIsTheWholeClipInEitherSpelling() throws {
        func link(_ source: String) throws -> PlaybackLink {
            try decoder.decode(PlaybackLink.self, from: Data(
                #"{"url": "/v1/stream/t", "source": "\#(source)", "max_seconds": null, "ready": true}"#.utf8))
        }
        XCTAssertTrue(try link("analysis_proxy").isWhole)
        XCTAssertTrue(try link("lumiverb.video.analysis_proxy").isWhole)
        XCTAssertFalse(try link("preview").isWhole)
        XCTAssertFalse(try link("lumiverb.video.preview").isWhole)
    }
}
