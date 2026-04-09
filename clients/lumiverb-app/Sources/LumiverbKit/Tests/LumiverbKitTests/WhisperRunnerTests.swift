#if os(macOS)
import XCTest
import Foundation
@testable import LumiverbKit

/// Tests for `WhisperRunner`.
///
/// The end-to-end transcription test is skipped on systems where
/// `whisper-cpp` (or a GGML model) isn't installed; the parser and
/// binary-discovery tests run unconditionally so the contract is
/// always covered.
final class WhisperRunnerTests: XCTestCase {

    // MARK: - parseLanguageFromJSONFile

    /// whisper-cli's canonical -oj output. The `result.language` field is
    /// the source of truth for auto-detected language; the rest of the
    /// JSON has model metadata, parameters, and a transcription array.
    func testParseLanguageFromCanonicalJSON() throws {
        let json = """
        {
          "model": {"type": "tiny", "multilingual": true},
          "params": {"language": "auto"},
          "result": {"language": "en"},
          "transcription": []
        }
        """
        let path = try writeTempJSON(json)
        defer { try? FileManager.default.removeItem(atPath: path) }
        XCTAssertEqual(WhisperRunner.parseLanguageFromJSONFile(at: path), "en")
    }

    func testParseLanguageHandlesNonEnglishCode() throws {
        let json = """
        {"result": {"language": "es"}}
        """
        let path = try writeTempJSON(json)
        defer { try? FileManager.default.removeItem(atPath: path) }
        XCTAssertEqual(WhisperRunner.parseLanguageFromJSONFile(at: path), "es")
    }

    func testParseLanguageReturnsNilWhenFileMissing() {
        XCTAssertNil(WhisperRunner.parseLanguageFromJSONFile(at: "/tmp/nope-\(UUID()).json"))
    }

    func testParseLanguageReturnsNilOnMalformedJSON() throws {
        let path = try writeTempJSON("{not valid json")
        defer { try? FileManager.default.removeItem(atPath: path) }
        XCTAssertNil(WhisperRunner.parseLanguageFromJSONFile(at: path))
    }

    func testParseLanguageReturnsNilWhenLanguageFieldMissing() throws {
        let path = try writeTempJSON("{\"result\": {}}")
        defer { try? FileManager.default.removeItem(atPath: path) }
        XCTAssertNil(WhisperRunner.parseLanguageFromJSONFile(at: path))
    }

    func testParseLanguageReturnsNilWhenLanguageEmpty() throws {
        let path = try writeTempJSON("{\"result\": {\"language\": \"\"}}")
        defer { try? FileManager.default.removeItem(atPath: path) }
        XCTAssertNil(WhisperRunner.parseLanguageFromJSONFile(at: path))
    }

    private func writeTempJSON(_ content: String) throws -> String {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("lumiverb-whisper-test-\(UUID().uuidString).json")
        try content.write(to: url, atomically: true, encoding: .utf8)
        return url.path
    }

    // MARK: - defaultBinaryURL

    /// `defaultBinaryURL` should return either nil or a path to an
    /// executable file. Anything else (e.g. a non-executable path) is a bug.
    func testDefaultBinaryURLIsExecutableWhenPresent() throws {
        guard let url = WhisperRunner.defaultBinaryURL else {
            throw XCTSkip("whisper-cli not installed; test exercised via XCTSkip")
        }
        XCTAssertTrue(
            FileManager.default.isExecutableFile(atPath: url.path),
            "defaultBinaryURL returned \(url.path) but it is not executable",
        )
    }

    // MARK: - Configuration errors fire before any subprocess work

    func testTranscribeThrowsBinaryNotFoundWhenBinaryURLMissing() async throws {
        let bogus = URL(fileURLWithPath: "/definitely/does/not/exist/whisper-cli")
        let model = URL(fileURLWithPath: "/tmp/no-such-model.bin")
        let wav = URL(fileURLWithPath: "/tmp/no-such.wav")
        do {
            _ = try await WhisperRunner.transcribe(
                wavURL: wav,
                config: .init(modelURL: model, language: nil, binaryURL: bogus),
            )
            XCTFail("expected binaryNotFound")
        } catch let error as WhisperRunner.WhisperError {
            if case .binaryNotFound = error { return }
            XCTFail("expected binaryNotFound, got \(error)")
        }
    }

    func testTranscribeThrowsModelNotFoundWhenModelMissing() async throws {
        // Use /bin/sh as a stand-in "binary that exists" so we get past the
        // binaryNotFound check and reach the model existence check.
        let realExecutable = URL(fileURLWithPath: "/bin/sh")
        guard FileManager.default.isExecutableFile(atPath: realExecutable.path) else {
            throw XCTSkip("/bin/sh not present on this host")
        }
        let bogusModel = URL(fileURLWithPath: "/tmp/no-such-model-\(UUID()).bin")
        let wav = URL(fileURLWithPath: "/tmp/no-such.wav")
        do {
            _ = try await WhisperRunner.transcribe(
                wavURL: wav,
                config: .init(modelURL: bogusModel, binaryURL: realExecutable),
            )
            XCTFail("expected modelNotFound")
        } catch let error as WhisperRunner.WhisperError {
            if case .modelNotFound(let url) = error {
                XCTAssertEqual(url.path, bogusModel.path)
                return
            }
            XCTFail("expected modelNotFound, got \(error)")
        }
    }

    func testTranscribeThrowsWAVNotFoundWhenWAVMissing() async throws {
        let realExecutable = URL(fileURLWithPath: "/bin/sh")
        guard FileManager.default.isExecutableFile(atPath: realExecutable.path) else {
            throw XCTSkip("/bin/sh not present on this host")
        }
        let modelURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("fake-model-\(UUID()).bin")
        try Data([0x00]).write(to: modelURL)
        defer { try? FileManager.default.removeItem(at: modelURL) }

        let bogusWav = URL(fileURLWithPath: "/tmp/no-such-\(UUID()).wav")
        do {
            _ = try await WhisperRunner.transcribe(
                wavURL: bogusWav,
                config: .init(modelURL: modelURL, binaryURL: realExecutable),
            )
            XCTFail("expected wavNotFound")
        } catch let error as WhisperRunner.WhisperError {
            if case .wavNotFound = error { return }
            XCTFail("expected wavNotFound, got \(error)")
        }
    }

    // MARK: - sanitizeSRT

    func testSanitizeStripsBlankAudioPlaceholder() {
        let srt = """
        1
        00:00:00,000 --> 00:00:06,520
         [BLANK_AUDIO]

        """
        XCTAssertEqual(WhisperRunner.sanitizeSRT(srt), "")
    }

    func testSanitizeStripsTokenLoopHallucination() {
        let srt = """
        1
        00:00:00,000 --> 00:00:30,000
         ʕ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ

        """
        XCTAssertEqual(WhisperRunner.sanitizeSRT(srt), "")
    }

    func testSanitizeStripsMusicAndNoisePlaceholders() {
        let srt = """
        1
        00:00:00,000 --> 00:00:05,000
         [MUSIC]

        2
        00:00:05,000 --> 00:00:10,000
         [NOISE]

        """
        XCTAssertEqual(WhisperRunner.sanitizeSRT(srt), "")
    }

    func testSanitizePreservesRealSpeech() {
        let srt = """
        1
        00:00:00,000 --> 00:00:05,000
         The quick brown fox jumped over the lazy dog.

        """
        let result = WhisperRunner.sanitizeSRT(srt)
        XCTAssertTrue(result.contains("brown fox"))
        XCTAssertTrue(result.contains("00:00:00,000"))
    }

    func testSanitizeStripsKnownHallucinationThankYou() {
        let srt = """
        1
        00:00:00,000 --> 00:00:06,520
         Thank you for watching!

        """
        XCTAssertEqual(WhisperRunner.sanitizeSRT(srt), "")
    }

    func testSanitizeStripsKnownHallucinationCaseInsensitive() {
        let srt = """
        1
        00:00:00,000 --> 00:00:05,000
         THANKS FOR WATCHING.

        """
        XCTAssertEqual(WhisperRunner.sanitizeSRT(srt), "")
    }

    func testSanitizePreservesPhraseEmbeddedInLongerSpeech() {
        // The phrase appears mid-transcript, not as the entire body.
        // Real videos can legitimately end with "thanks for watching!" —
        // we should NOT filter it when there's other content alongside.
        let srt = """
        1
        00:00:00,000 --> 00:00:10,000
         Welcome to the show. Today we're going to cover three topics. Thanks for watching.

        """
        let result = WhisperRunner.sanitizeSRT(srt)
        XCTAssertTrue(result.contains("Welcome to the show"))
        XCTAssertTrue(result.contains("Thanks for watching"))
    }

    func testSanitizeMixedSilenceAndSpeechKeepsOnlySpeech() {
        let srt = """
        1
        00:00:00,000 --> 00:00:03,000
         [BLANK_AUDIO]

        2
        00:00:03,000 --> 00:00:08,000
         Hello world this is a real sentence.

        """
        let result = WhisperRunner.sanitizeSRT(srt)
        XCTAssertTrue(result.contains("Hello world"))
        XCTAssertFalse(result.contains("BLANK_AUDIO"))
    }
}
#endif
