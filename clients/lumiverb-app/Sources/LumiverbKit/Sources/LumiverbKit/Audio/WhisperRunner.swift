#if os(macOS)
@preconcurrency import Foundation

/// Thin wrapper around the `whisper-cli` binary from the Homebrew
/// `whisper-cpp` package. Takes a 16 kHz mono PCM_S16LE WAV (the canonical
/// whisper.cpp input — produced by `AudioExtraction.extractToWAV`) and a GGML
/// model file, runs the binary, and returns the resulting SRT bytes plus the
/// detected language.
///
/// macOS-only because `Process` is not available on iOS. The macOS native
/// client is the only target that runs enrichment; iOS is browse-only per
/// ADR-014.
///
/// Mirrors the Python production reference at
/// `src/client/cli/repair.py:148-216`. The SRT format produced by whisper-cli
/// is byte-identical to what `faster-whisper` produces in the Python path.
public enum WhisperRunner {

    /// Configuration for one transcription run.
    public struct Config: Sendable {
        /// Absolute path to a GGML model file (e.g. `ggml-small.bin`).
        public var modelURL: URL
        /// Optional language code (e.g. "en", "es"). nil means auto-detect.
        public var language: String?
        /// Override the binary path. nil = auto-discover via `defaultBinaryURL`.
        public var binaryURL: URL?
        /// Hard wall-clock cap on whisper invocation. Matches the 1-hour
        /// ceiling in the Python production reference.
        public var timeoutSeconds: TimeInterval

        public init(
            modelURL: URL,
            language: String? = nil,
            binaryURL: URL? = nil,
            timeoutSeconds: TimeInterval = 3600,
        ) {
            self.modelURL = modelURL
            self.language = language
            self.binaryURL = binaryURL
            self.timeoutSeconds = timeoutSeconds
        }
    }

    public struct TranscriptionResult: Sendable {
        public let srt: String
        public let language: String

        public init(srt: String, language: String) {
            self.srt = srt
            self.language = language
        }
    }

    public enum WhisperError: Error, CustomStringConvertible {
        case binaryNotFound
        case modelNotFound(URL)
        case wavNotFound(URL)
        case launchFailed(String)
        case nonZeroExit(code: Int32, stderr: String)
        case missingOutput(String)
        case timeout(TimeInterval)

        public var description: String {
            switch self {
            case .binaryNotFound:
                return "whisper-cli not found. Install with `brew install whisper-cpp` or set the binary path in Settings."
            case .modelNotFound(let url):
                return "Whisper model not found at \(url.path). On the macOS app, enable whisper transcription in Settings to auto-download. For CLI / scripted use, download a GGML model from https://huggingface.co/ggerganov/whisper.cpp/tree/main and place it there."
            case .wavNotFound(let url):
                return "Audio WAV missing at \(url.path) — extraction step failed silently."
            case .launchFailed(let m):
                return "whisper-cli failed to launch: \(m)"
            case .nonZeroExit(let code, let stderr):
                let preview = stderr.prefix(500)
                return "whisper-cli exited \(code): \(preview)"
            case .missingOutput(let path):
                return "whisper-cli completed but produced no SRT at \(path)"
            case .timeout(let seconds):
                return "whisper-cli timed out after \(Int(seconds))s"
            }
        }
    }

    /// Common install paths for `whisper-cli`. Some older builds of whisper.cpp
    /// shipped the binary as `main`; check for both names. Homebrew installs
    /// to `/opt/homebrew/bin` on Apple Silicon and `/usr/local/bin` on Intel.
    private static let candidateBinaryPaths: [String] = [
        "/opt/homebrew/bin/whisper-cli",
        "/opt/homebrew/bin/main",
        "/usr/local/bin/whisper-cli",
        "/usr/local/bin/main",
    ]

    /// Auto-discover the whisper-cli binary from common Homebrew locations.
    /// Returns nil if no candidate exists.
    public static var defaultBinaryURL: URL? {
        for path in candidateBinaryPaths {
            if FileManager.default.isExecutableFile(atPath: path) {
                return URL(fileURLWithPath: path)
            }
        }
        return nil
    }

    /// Run whisper-cli against `wavURL`, returning the SRT bytes and the
    /// detected (or configured) language code.
    ///
    /// - Important: pass a WAV produced by `AudioExtraction.extractToWAV`.
    ///   Other sample rates / channel counts will be silently rejected by
    ///   whisper-cli, producing a non-zero-but-cryptic exit code.
    public static func transcribe(
        wavURL: URL,
        config: Config,
    ) async throws -> TranscriptionResult {
        let binary = config.binaryURL ?? defaultBinaryURL
        guard let binary, FileManager.default.isExecutableFile(atPath: binary.path) else {
            throw WhisperError.binaryNotFound
        }
        guard FileManager.default.fileExists(atPath: config.modelURL.path) else {
            throw WhisperError.modelNotFound(config.modelURL)
        }
        guard FileManager.default.fileExists(atPath: wavURL.path) else {
            throw WhisperError.wavNotFound(wavURL)
        }

        let outputDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("lumiverb-whisper-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: outputDir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: outputDir) }

        let outputPrefix = outputDir.appendingPathComponent("out").path
        let srtPath = outputPrefix + ".srt"
        let jsonPath = outputPrefix + ".json"

        // Always request both SRT (the user-facing artifact) and JSON (the
        // metadata sidecar that contains the auto-detected language).
        // whisper-cli ≥1.7 stopped printing the language line to stderr, so
        // the JSON is the only reliable source.
        var args: [String] = [
            "-m", config.modelURL.path,
            "-f", wavURL.path,
            "-osrt",
            "-oj",
            "-of", outputPrefix,
            "--no-prints",
        ]
        if let language = config.language, !language.isEmpty {
            args += ["-l", language]
        } else {
            args += ["-l", "auto"]
        }

        let result: ProcessResult
        do {
            result = try await runProcess(
                executableURL: binary,
                arguments: args,
                timeoutSeconds: config.timeoutSeconds,
            )
        } catch let error as WhisperError {
            throw error
        } catch {
            throw WhisperError.launchFailed(error.localizedDescription)
        }

        if result.exitCode != 0 {
            throw WhisperError.nonZeroExit(code: result.exitCode, stderr: result.stderr)
        }

        guard FileManager.default.fileExists(atPath: srtPath) else {
            throw WhisperError.missingOutput(srtPath)
        }
        let rawSrt = (try? String(contentsOfFile: srtPath, encoding: .utf8)) ?? ""
        let srt = sanitizeSRT(rawSrt)

        let detectedLanguage = parseLanguageFromJSONFile(at: jsonPath)
            ?? config.language
            ?? ""

        return TranscriptionResult(srt: srt, language: detectedLanguage)
    }

    // MARK: - SRT sanitization

    /// Strip degenerate whisper output. Returns either the cleaned SRT or
    /// an empty string when nothing meaningful remains — the empty string
    /// is the canonical "checked, no speech" signal that the server's
    /// `/v1/assets/{id}/transcript` endpoint understands.
    ///
    /// Three categories of degenerate output get dropped:
    ///
    /// 1. **Bracketed non-speech placeholders** (`[BLANK_AUDIO]`, `[MUSIC]`,
    ///    `[NOISE]`) — whisper-cli emits these as "I processed this segment
    ///    but there was no transcribable speech" markers.
    ///
    /// 2. **IPA token-loop hallucinations** (e.g. `ʕ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ ʔ`) —
    ///    macOS's AAC decoder is non-deterministic on near-silent input,
    ///    and the small whisper model occasionally enters a
    ///    self-referential prediction loop on the resulting noise floor.
    ///    Detected by the absence of any 2+ consecutive ASCII letter pair.
    ///
    /// 3. **Known training-data hallucinations** (`"Thank you for
    ///    watching!"`, `"Subscribe to the channel"`, etc.) — the small
    ///    model has these phrases baked in from YouTube training data
    ///    and emits them when fed silent or music-only audio. They're
    ///    well-formed English so the IPA filter doesn't catch them.
    ///    To minimize false positives we only drop them when they make
    ///    up the *entire* segment body — a real video where someone says
    ///    "Thanks for watching!" inside a longer sentence will not be
    ///    filtered.
    ///
    /// Public for testing.
    public static func sanitizeSRT(_ srt: String) -> String {
        let placeholders = [
            "[BLANK_AUDIO]", "[MUSIC]", "[NOISE]", "[SOUND]",
            "[silence]", "[music]", "[noise]", "[no audio]", "[blank audio]",
        ]
        // Known whisper hallucination phrases. Lowercased for
        // case-insensitive comparison. Only matched against the *entire*
        // segment body (after trimming and punctuation strip) so a real
        // longer transcript that happens to contain "thanks for watching"
        // is not filtered.
        let hallucinationPhrases: Set<String> = [
            "thank you for watching",
            "thank you for watching!",
            "thanks for watching",
            "thanks for watching!",
            "please subscribe to the channel",
            "subscribe to the channel",
            "subscribe to my channel",
            "like and subscribe",
            "don't forget to like and subscribe",
            "thanks for listening",
            "thank you so much for watching",
            "thanks for watching, see you next time",
            "see you next time",
            "bye",
            "bye bye",
            "goodbye",
        ]

        // Parse out the segment text bodies. SRT segments look like:
        //   N
        //   HH:MM:SS,mmm --> HH:MM:SS,mmm
        //   text body
        //   <blank line>
        // We split on blank lines and inspect each block.
        let blocks = srt.components(separatedBy: "\n\n")
        var keptBlocks: [String] = []
        for block in blocks {
            let lines = block.split(separator: "\n", omittingEmptySubsequences: false)
            guard lines.count >= 3 else { continue }
            // Lines 2..N are the text body (some segments span multiple lines).
            let bodyLines = lines.dropFirst(2)
            let body = bodyLines.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
            if body.isEmpty { continue }

            // Skip if the entire body is one of the bracketed placeholders.
            var stripped = body
            for placeholder in placeholders {
                stripped = stripped.replacingOccurrences(of: placeholder, with: "")
            }
            stripped = stripped.trimmingCharacters(in: .whitespacesAndNewlines)
            if stripped.isEmpty { continue }

            // Skip if the entire body matches a known training-data
            // hallucination phrase. Compared after lowercasing and
            // stripping trailing punctuation so "Thank you for watching!"
            // and "thank you for watching." both match the canonical
            // entry. Note: only the FULL body must match — a longer
            // transcript that contains the phrase as a clause is left
            // alone.
            let lowered = stripped
                .lowercased()
                .trimmingCharacters(in: CharacterSet(charactersIn: " .!?,;:"))
            if hallucinationPhrases.contains(lowered) {
                continue
            }

            // Skip whisper hallucinations on near-silent input. Real
            // English / Spanish / French speech always contains at least
            // one ASCII letter pair; whisper's silence-loop hallucinations
            // consist of IPA glyphs (`ʕ`, `ʔ`) and similar non-Latin
            // characters that never form ASCII letter sequences. If we
            // can't find any 2+ ASCII-letter sequence, the segment is
            // gibberish.
            //
            // **Known limitation:** this filter is ASCII-only by design.
            // Audio in fully non-Latin scripts (Cyrillic / CJK / Arabic /
            // Hebrew / Thai / etc.) would not produce any ASCII letter
            // pairs and so would be incorrectly filtered. The user's
            // photo/video library is overwhelmingly Latin-script in
            // practice, and the cost of letting IPA gibberish leak into
            // user-facing transcripts (the alternative) is worse. The
            // intended Unicode-letter category mitigation does NOT work
            // because the IPA glyphs are in "letter, modifier" / "letter,
            // other" categories which ARE in CharacterSet.letters — we'd
            // accept the hallucinations as legitimate.
            if !containsTwoConsecutiveLetters(stripped) {
                continue
            }

            keptBlocks.append(block)
        }

        return keptBlocks.joined(separator: "\n\n")
    }

    // MARK: - JSON sidecar language parsing

    /// Extract `result.language` from a whisper-cli `-oj` JSON file.
    /// Returns nil if the file is missing, malformed, or doesn't contain a
    /// language code. Public for testing.
    public static func parseLanguageFromJSONFile(at path: String) -> String? {
        guard FileManager.default.fileExists(atPath: path),
              let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
              let raw = try? JSONSerialization.jsonObject(with: data),
              let root = raw as? [String: Any],
              let result = root["result"] as? [String: Any],
              let lang = result["language"] as? String,
              !lang.isEmpty else {
            return nil
        }
        return lang
    }

    /// True if the input contains any two consecutive ASCII letters [a-zA-Z].
    /// Used by `sanitizeSRT` to filter whisper's IPA-glyph silence
    /// hallucinations: real English / Spanish / French speech always
    /// produces ASCII letter pairs in the SRT, while the loop output
    /// (`ʕ ʔ ʔ ʔ`) consists entirely of non-ASCII modifier glyphs.
    ///
    /// Audio in fully non-Latin scripts (Cyrillic / CJK / Arabic) would
    /// not match this and would be filtered. That's an acceptable cost for
    /// the user's mostly-Latin-script library; the alternative is letting
    /// IPA gibberish leak into the user-facing transcripts.
    private static func containsTwoConsecutiveLetters(_ s: String) -> Bool {
        var prevWasLetter = false
        for scalar in s.unicodeScalars {
            let v = scalar.value
            let isAscii = (v >= 0x41 && v <= 0x5A) || (v >= 0x61 && v <= 0x7A)
            if isAscii {
                if prevWasLetter { return true }
                prevWasLetter = true
            } else {
                prevWasLetter = false
            }
        }
        return false
    }

    // MARK: - Subprocess plumbing

    private struct ProcessResult {
        let exitCode: Int32
        let stdout: String
        let stderr: String
    }

    private static func runProcess(
        executableURL: URL,
        arguments: [String],
        timeoutSeconds: TimeInterval,
    ) async throws -> ProcessResult {
        // The timeout flag has to be reference-typed so the DispatchWorkItem
        // closure and the terminationHandler closure can both observe it.
        // Using `terminationReason == .uncaughtSignal` to detect timeout
        // is fragile because that reason also fires for any signal-driven
        // termination (segfault, OOM kill, user-sent SIGTERM via Activity
        // Monitor, etc.) — we'd report any of those as "timeout".
        let timeoutFlag = TimeoutFlag()

        return try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            process.executableURL = executableURL
            process.arguments = arguments

            let stdoutPipe = Pipe()
            let stderrPipe = Pipe()
            process.standardOutput = stdoutPipe
            process.standardError = stderrPipe

            let timeoutItem = DispatchWorkItem {
                if process.isRunning {
                    timeoutFlag.set()
                    process.terminate()
                }
            }
            DispatchQueue.global(qos: .utility).asyncAfter(
                deadline: .now() + timeoutSeconds,
                execute: timeoutItem,
            )

            process.terminationHandler = { proc in
                timeoutItem.cancel()
                let stdoutData = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
                let stderrData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
                let stdout = String(data: stdoutData, encoding: .utf8) ?? ""
                let stderr = String(data: stderrData, encoding: .utf8) ?? ""
                if timeoutFlag.isSet {
                    continuation.resume(throwing: WhisperError.timeout(timeoutSeconds))
                } else {
                    continuation.resume(returning: ProcessResult(
                        exitCode: proc.terminationStatus,
                        stdout: stdout,
                        stderr: stderr,
                    ))
                }
            }

            do {
                try process.run()
            } catch {
                timeoutItem.cancel()
                continuation.resume(throwing: WhisperError.launchFailed(error.localizedDescription))
            }
        }
    }
}

/// Mutable thread-safe boolean used by the WhisperRunner timeout path.
/// Reference-typed so it can be shared between the timeout DispatchWorkItem
/// and the Process terminationHandler closures, both of which run on
/// background queues.
private final class TimeoutFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var _value = false
    var isSet: Bool { lock.lock(); defer { lock.unlock() }; return _value }
    func set() { lock.lock(); _value = true; lock.unlock() }
}
#endif
