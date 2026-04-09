import Foundation
import LumiverbKit

/// macOS-target orchestrator around `LumiverbKit.WhisperRunner`.
///
/// Owns:
///   - GGML model file resolution (`~/.lumiverb/models/whisper/ggml-<size>.bin`)
///   - Audio extraction → temp WAV via `LumiverbKit.AudioExtraction`
///   - Whisper invocation via `LumiverbKit.WhisperRunner`
///   - Temp file cleanup
///
/// The actual subprocess plumbing and audio decoding live in LumiverbKit so
/// they can be exercised by the LumiverbKit test target — this file is the
/// thin per-user-config wrapper that pulls model size / language / binary
/// path off `AppState` and feeds them through.
///
/// Mirrors the Python production reference at
/// `src/client/cli/repair.py:107-216`.
enum WhisperProvider {

    static let providerId = "whisper.cpp"

    /// Default place to look for / download GGML model files. Matches the
    /// `~/.lumiverb/models/...` convention used by ArcFace and CLIP.
    static var defaultModelDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".lumiverb/models/whisper", isDirectory: true)
    }

    /// Resolve the GGML model file URL for a given size code (e.g. "tiny",
    /// "base", "small", "medium", "large-v3"). Files are expected to live at
    /// `~/.lumiverb/models/whisper/ggml-<size>.bin`.
    static func defaultModelURL(forSize size: String) -> URL {
        defaultModelDirectory.appendingPathComponent("ggml-\(size).bin")
    }

    /// Whether the provider is fully configured: the binary is discoverable
    /// AND a model file exists for the requested size.
    static func isConfigured(modelSize: String, binaryPath: String? = nil) -> Bool {
        let binary: URL?
        if let binaryPath, !binaryPath.isEmpty {
            binary = URL(fileURLWithPath: binaryPath)
        } else {
            binary = WhisperRunner.defaultBinaryURL
        }
        guard let binary, FileManager.default.isExecutableFile(atPath: binary.path) else {
            return false
        }
        let model = defaultModelURL(forSize: modelSize)
        return FileManager.default.fileExists(atPath: model.path)
    }

    /// Run the whole pipeline for one source video and return its SRT +
    /// detected language.
    ///
    /// - Returns: `(srt, language)`. `srt` is empty when the source has no
    ///   audio track or contains only silence — this matches the Python
    ///   production code and the server's "no_speech" status path.
    static func transcribe(
        sourceURL: URL,
        modelSize: String,
        language: String?,
        binaryPath: String?,
    ) async throws -> (srt: String, language: String) {
        let modelURL = defaultModelURL(forSize: modelSize)
        let binaryURL: URL?
        if let binaryPath, !binaryPath.isEmpty {
            binaryURL = URL(fileURLWithPath: binaryPath)
        } else {
            binaryURL = WhisperRunner.defaultBinaryURL
        }

        let wavURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("lumiverb-whisper-input-\(UUID().uuidString).wav")
        defer { try? FileManager.default.removeItem(at: wavURL) }

        let extracted = try await AudioExtraction.extractToWAV(
            sourceURL: sourceURL, destinationURL: wavURL,
        )
        if !extracted {
            // Deterministic empty: no audio track. The /v1/assets/{id}/transcript
            // endpoint accepts an empty SRT to mark "checked, no speech" so the
            // asset is not retried on every enrichment run.
            return (srt: "", language: language ?? "")
        }

        let result = try await WhisperRunner.transcribe(
            wavURL: wavURL,
            config: .init(
                modelURL: modelURL,
                language: language,
                binaryURL: binaryURL,
            ),
        )
        return (srt: result.srt, language: result.language)
    }
}
