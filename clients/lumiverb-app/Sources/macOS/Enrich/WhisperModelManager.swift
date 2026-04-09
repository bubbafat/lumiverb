import Foundation
import LumiverbKit

/// Observable manager for whisper.cpp GGML model files.
///
/// Owns:
///   - the model-size catalog (display name, approximate file size, download URL),
///   - the on-disk model directory `~/.lumiverb/models/whisper/`,
///   - one-shot download with byte-level progress reporting via `@Published`,
///   - cleanup of unused models (single-active-model invariant),
///   - idempotent attach-to-in-progress-download semantics.
///
/// Models are downloaded from the canonical whisper.cpp HuggingFace repo
/// (`https://huggingface.co/ggerganov/whisper.cpp`). The user is enrolled in
/// auto-download by enabling the Settings toggle; the actual download is
/// kicked off explicitly when they hit Save with a model size that isn't
/// on disk yet.
@MainActor
final class WhisperModelManager: ObservableObject {

    static let shared = WhisperModelManager()

    // MARK: - Model catalog

    /// Available whisper model sizes, ordered fastest → highest quality.
    enum ModelSize: String, CaseIterable, Identifiable {
        case tiny
        case base
        case small
        case medium
        case largeV3 = "large-v3"

        var id: String { rawValue }
        var displayName: String { rawValue }

        /// Approximate on-disk size, in megabytes. Rounded to friendly
        /// values — these are surfaced in the (i) tooltip, the slider
        /// caption, and the initial expected-bytes value of the download
        /// sheet (which gets corrected by the actual server-reported size
        /// once HTTP headers come in).
        var approximateSizeMB: Int {
            switch self {
            case .tiny:    return 75
            case .base:    return 150
            case .small:   return 500
            case .medium:  return 1_500
            case .largeV3: return 3_000
            }
        }

        /// Human-readable quality / speed hint shown in the tooltip.
        var qualityHint: String {
            switch self {
            case .tiny:    return "fastest, roughest"
            case .base:    return "fast"
            case .small:   return "balanced (recommended)"
            case .medium:  return "more accurate, slower"
            case .largeV3: return "best quality, slowest"
            }
        }

        var filename: String { "ggml-\(rawValue).bin" }

        var downloadURL: URL {
            URL(string: "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/\(filename)")!
        }
    }

    // MARK: - Published state

    enum DownloadState: Equatable {
        case idle
        case downloading(modelSize: ModelSize, bytesReceived: Int64, bytesExpected: Int64)
        case completed(modelSize: ModelSize)
        case failed(modelSize: ModelSize, message: String)
        case cancelled(modelSize: ModelSize)
    }

    @Published private(set) var downloadState: DownloadState = .idle

    private var activeTask: URLSessionDownloadTask?
    private var activeDelegate: DownloadDelegate?

    // MARK: - Filesystem
    //
    // The static helpers below touch only FileManager and never the actor
    // state, so they're safe to call from any context (including the
    // URLSessionDownloadDelegate callbacks that fire on the delegate
    // queue). They're marked `nonisolated` so Swift 6 strict concurrency
    // doesn't require an actor hop just to look up a path.

    nonisolated static var modelDirectory: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".lumiverb/models/whisper", isDirectory: true)
    }

    nonisolated static func modelURL(for size: ModelSize) -> URL {
        modelDirectory.appendingPathComponent(size.filename)
    }

    nonisolated static func isModelInstalled(_ size: ModelSize) -> Bool {
        FileManager.default.fileExists(atPath: modelURL(for: size).path)
    }

    /// Total bytes used by every installed whisper model. Used in the
    /// disable-confirmation dialog so the user knows how much disk space
    /// they would reclaim.
    nonisolated static func totalInstalledBytes() -> Int64 {
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: modelDirectory,
            includingPropertiesForKeys: [.fileSizeKey],
        ) else { return 0 }
        var total: Int64 = 0
        for url in entries
            where url.lastPathComponent.hasPrefix("ggml-")
                && url.lastPathComponent.hasSuffix(".bin") {
            if let size = (try? url.resourceValues(forKeys: [.fileSizeKey]))?.fileSize {
                total += Int64(size)
            }
        }
        return total
    }

    /// Delete every installed model EXCEPT the one passed in. When `keep`
    /// is nil, every whisper model on disk is removed (the disable-cleanup
    /// flow). Otherwise this enforces the single-active-model invariant
    /// after a successful download to a different size.
    nonisolated static func cleanup(keep: ModelSize?) {
        let dir = modelDirectory
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: nil,
        ) else { return }
        let keepFilename = keep?.filename
        for url in entries
            where url.lastPathComponent.hasPrefix("ggml-")
                && url.lastPathComponent.hasSuffix(".bin") {
            if url.lastPathComponent != keepFilename {
                try? FileManager.default.removeItem(at: url)
            }
        }
    }

    // MARK: - Download orchestration

    /// Start a download for the given model size. Idempotent in two ways:
    ///   1. If the model is already on disk, immediately transitions to
    ///      `.completed` and returns without doing network work.
    ///   2. If the same download is already in progress, returns without
    ///      starting a second one.
    /// If a different size is currently downloading, that download is
    /// cancelled and replaced.
    func startDownload(_ size: ModelSize) {
        if Self.isModelInstalled(size) {
            downloadState = .completed(modelSize: size)
            // Make sure the single-active-model invariant holds even if a
            // previous run left old files on disk.
            Self.cleanup(keep: size)
            return
        }

        if case .downloading(let active, _, _) = downloadState, active == size {
            return
        }

        // Cancel any in-progress download for a different size.
        activeTask?.cancel()
        activeTask = nil
        activeDelegate = nil

        do {
            try FileManager.default.createDirectory(
                at: Self.modelDirectory, withIntermediateDirectories: true,
            )
        } catch {
            downloadState = .failed(
                modelSize: size,
                message: "could not create \(Self.modelDirectory.path): \(error.localizedDescription)",
            )
            return
        }

        downloadState = .downloading(
            modelSize: size,
            bytesReceived: 0,
            bytesExpected: Int64(size.approximateSizeMB) * 1_000_000,
        )

        let delegate = DownloadDelegate(modelSize: size, manager: self)
        let session = URLSession(configuration: .default, delegate: delegate, delegateQueue: .main)
        let task = session.downloadTask(with: size.downloadURL)
        activeTask = task
        activeDelegate = delegate
        task.resume()
    }

    /// Cancel the in-progress download (if any). Transitions state to
    /// `.cancelled` so the UI sheet can react.
    func cancelDownload() {
        activeTask?.cancel()
        activeTask = nil
        activeDelegate = nil
        if case .downloading(let size, _, _) = downloadState {
            downloadState = .cancelled(modelSize: size)
        }
    }

    /// Reset state back to idle (used when the UI dismisses the
    /// completion / failure / cancellation banner).
    func acknowledgeTerminalState() {
        downloadState = .idle
    }

    // MARK: - Delegate callbacks (called on the main queue)

    fileprivate func progress(_ size: ModelSize, received: Int64, expected: Int64) {
        downloadState = .downloading(
            modelSize: size,
            bytesReceived: received,
            bytesExpected: expected > 0
                ? expected
                : Int64(size.approximateSizeMB) * 1_000_000,
        )
    }

    fileprivate func finished(_ size: ModelSize, success: Bool, message: String?) {
        if success {
            Self.cleanup(keep: size)
            downloadState = .completed(modelSize: size)
        } else {
            downloadState = .failed(modelSize: size, message: message ?? "unknown error")
        }
        activeTask = nil
        activeDelegate = nil
    }

    fileprivate func failed(_ size: ModelSize, error: Error) {
        // Ignore "cancelled" errors — the cancel flow already updated state.
        if (error as NSError).code == NSURLErrorCancelled { return }
        downloadState = .failed(modelSize: size, message: error.localizedDescription)
        activeTask = nil
        activeDelegate = nil
    }
}

// MARK: - URLSessionDownloadDelegate

private final class DownloadDelegate: NSObject, URLSessionDownloadDelegate {
    let modelSize: WhisperModelManager.ModelSize
    weak var manager: WhisperModelManager?

    init(modelSize: WhisperModelManager.ModelSize, manager: WhisperModelManager) {
        self.modelSize = modelSize
        self.manager = manager
    }

    func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didWriteData bytesWritten: Int64,
        totalBytesWritten: Int64,
        totalBytesExpectedToWrite: Int64,
    ) {
        let size = self.modelSize
        let received = totalBytesWritten
        let expected = totalBytesExpectedToWrite
        Task { @MainActor [weak manager] in
            manager?.progress(size, received: received, expected: expected)
        }
    }

    func urlSession(
        _ session: URLSession,
        downloadTask: URLSessionDownloadTask,
        didFinishDownloadingTo location: URL,
    ) {
        // The temp file at `location` is invalidated as soon as this
        // method returns, so we have to validate AND move synchronously
        // before the closure dispatches to the main actor.
        let dest = WhisperModelManager.modelURL(for: modelSize)
        var success = false
        var message: String?

        // Validation gate: HuggingFace can serve 302/HTML on transient
        // errors (CDN edge cases, rate limiting, model retraction), and
        // the URLSession download task happily writes the redirect HTML
        // to the temp file. Without these checks we'd move a 1 KB HTML
        // body into ~/.lumiverb/models/whisper/ggml-small.bin and report
        // "configured" — only to fail at run time with a confusing
        // whisper-cli "model load" error.
        if let response = downloadTask.response as? HTTPURLResponse,
           response.statusCode != 200 {
            message = "download failed: HTTP \(response.statusCode)"
        } else {
            // Size sanity check: real GGML files are 70 MB → 3 GB.
            // Anything under 80% of the catalog estimate is almost
            // certainly an error response, not a model.
            let attrs = try? FileManager.default.attributesOfItem(atPath: location.path)
            let actualSize = (attrs?[.size] as? NSNumber)?.int64Value ?? 0
            let expectedMB = Int64(modelSize.approximateSizeMB)
            let minBytes = (expectedMB * 1_000_000 * 8) / 10  // 80% of expected
            if actualSize < minBytes {
                let actualMB = actualSize / 1_000_000
                message = "download too small: got \(actualMB) MB, expected ≈\(expectedMB) MB — likely an error response"
            } else {
                do {
                    try FileManager.default.createDirectory(
                        at: WhisperModelManager.modelDirectory,
                        withIntermediateDirectories: true,
                    )
                    try? FileManager.default.removeItem(at: dest)
                    try FileManager.default.moveItem(at: location, to: dest)
                    success = true
                } catch {
                    message = error.localizedDescription
                }
            }
        }

        // If we rejected the download, make sure the bogus temp file is
        // gone so the next attempt doesn't see stale state.
        if !success {
            try? FileManager.default.removeItem(at: location)
            try? FileManager.default.removeItem(at: dest)
        }

        let size = self.modelSize
        let cap = success
        let msg = message
        Task { @MainActor [weak manager] in
            manager?.finished(size, success: cap, message: msg)
        }
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didCompleteWithError error: Error?,
    ) {
        guard let error else { return }
        let size = self.modelSize
        Task { @MainActor [weak manager] in
            manager?.failed(size, error: error)
        }
    }
}
