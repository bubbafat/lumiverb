#if os(macOS)
import XCTest
import Foundation
@testable import LumiverbKit

/// The Mac app's ffmpeg and ffprobe (its scan's video poster) open a video
/// only as a local file in a container Lumiverb takes in (`FFmpegInput`),
/// as the server's do (`src/processing/video/ffmpeg_run.py`). Run against
/// the real ffprobe when it's installed.
final class FFmpegInputTests: XCTestCase {

    private var tempDir: URL!

    override func setUp() {
        super.setUp()
        tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("lumiverb-ffmpeg-input-tests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempDir)
        super.tearDown()
    }

    func testEveryVideoExtensionHasItsDemuxer() {
        for ext in FileExtensions.video {
            XCTAssertNotNil(FFmpegInput.demuxers[ext], "\(ext) has no demuxer in FFmpegInput.demuxers")
        }
    }

    func testTheArgumentsAreTheServers() {
        // The same as ffmpeg_run.input_args() (tests/test_video_input_whitelists.py says so too).
        XCTAssertEqual(FFmpegInput.arguments, ["-protocol_whitelist", "file", "-format_whitelist", "matroska,mov"])
    }

    func testFFprobeOpensAClipAndRefusesAListNamingIt() throws {
        let ffprobe = try requireFFprobe()
        let real = tempDir.appendingPathComponent("real.mov")
        try FileManager.default.copyItem(at: try requireFixture("transcribe-no-words-480.mov"), to: real)
        // ffmpeg's concat format, named like a clip, naming the clip beside it.
        let listed = tempDir.appendingPathComponent("listed.mov")
        try "ffconcat version 1.0\nfile real.mov\n".write(to: listed, atomically: true, encoding: .utf8)

        XCTAssertEqual(try probe(ffprobe, FFmpegInput.arguments, real), 0)
        XCTAssertEqual(try probe(ffprobe, [], listed), 0, "without the whitelists ffprobe reads the list")
        XCTAssertNotEqual(try probe(ffprobe, FFmpegInput.arguments, listed), 0)
    }

    // MARK: - Helpers

    /// ffprobe's exit on `file`, with `arguments` before it.
    private func probe(_ ffprobe: String, _ arguments: [String], _ file: URL) throws -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: ffprobe)
        process.arguments = ["-v", "quiet"] + arguments + ["-show_entries", "format=format_name", file.path]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run()
        process.waitUntilExit()
        return process.terminationStatus
    }

    private func requireFFprobe() throws -> String {
        for path in ["/opt/homebrew/bin/ffprobe", "/usr/local/bin/ffprobe"]
        where FileManager.default.isExecutableFile(atPath: path) {
            return path
        }
        throw XCTSkip("ffprobe not installed")
    }

    private func requireFixture(_ name: String) throws -> URL {
        guard let url = Bundle.module.url(
            forResource: name, withExtension: nil, subdirectory: "Fixtures",
        ) ?? Bundle.module.url(forResource: name, withExtension: nil) else {
            throw XCTSkip("fixture \(name) not present in test bundle")
        }
        return url
    }
}
#endif
