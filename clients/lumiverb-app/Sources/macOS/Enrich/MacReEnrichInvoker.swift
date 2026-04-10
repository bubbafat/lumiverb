import Foundation
import LumiverbKit

/// macOS-side `ReEnrichInvoker`. Wraps `ReEnrichmentRunner` (which
/// pulls in CLIP / ArcFace / Whisper / Vision providers — all
/// macOS-only). Constructed by `AppState` and handed to `BrowseState`
/// at scene wiring time so the LumiverbKit-resident `BrowseState` can
/// trigger re-enrichment without importing the providers itself.
///
/// Holds a weak reference to the current `ReEnrichmentRunner` so
/// `cancel()` can reach in. The runner is recreated on every
/// `reEnrich(...)` call (matching the prior in-line construction in
/// BrowseState), so there is at most one outstanding runner at a time.
@MainActor
final class MacReEnrichInvoker: ReEnrichInvoker {
    let appState: AppState
    private var currentRunner: ReEnrichmentRunner?

    init(appState: AppState) {
        self.appState = appState
    }

    func reEnrich(
        libraryId: String,
        libraryRootPath: String?,
        assets: [AssetPageItem],
        operations: Set<EnrichmentOperation>,
        progress: @escaping @MainActor (_ processed: Int, _ total: Int, _ phase: String) -> Void
    ) async -> ReEnrichmentResult {
        guard let client = appState.client else {
            return ReEnrichmentResult(processed: 0, errors: 0, skipped: [])
        }

        let runner = ReEnrichmentRunner(
            client: client,
            libraryId: libraryId,
            libraryRootPath: libraryRootPath,
            visionApiUrl: appState.resolvedVisionApiUrl,
            visionApiKey: appState.resolvedVisionApiKey,
            visionModelId: appState.resolvedVisionModelId,
            whisperModelSize: appState.whisperModelSize,
            whisperLanguage: appState.whisperLanguage,
            whisperBinaryPath: appState.whisperBinaryPath
        )
        currentRunner = runner

        // Polling task to forward progress. Mirrors what BrowseState
        // used to do directly before the protocol abstraction landed
        // (250 ms cadence is a UX choice — fast enough to feel live,
        // slow enough not to thrash main with @Published updates).
        let pollTask = Task { @MainActor in
            while !Task.isCancelled {
                let total = await runner.totalItems
                let processed = await runner.processedItems
                let phase = await runner.phase
                progress(processed, total, phase)
                try? await Task.sleep(for: .milliseconds(250))
            }
        }

        let result = await runner.run(assets: assets, operations: operations)
        pollTask.cancel()
        currentRunner = nil

        return ReEnrichmentResult(
            processed: result.processed,
            errors: result.errors,
            skipped: result.skipped
        )
    }

    func cancel() async {
        await currentRunner?.cancel()
    }
}
