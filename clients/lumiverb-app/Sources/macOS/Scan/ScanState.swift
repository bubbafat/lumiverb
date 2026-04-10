import SwiftUI
import LumiverbKit

/// Per-library health indicator. Drives the colored dot in the
/// sidebar and the "is this library currently being worked on" check
/// for the browse window's progress banner.
enum LibraryStatus: Equatable {
    /// Default healthy state — no pending work, root reachable.
    /// Rendered as green.
    case idle
    /// Currently being scanned or enriched. Rendered as orange.
    case busy
    /// Root path doesn't exist on disk (unmounted volume, moved
    /// folder, bad path). Rendered as red. Scan skips these until
    /// they come back online.
    case offline
}

/// Observable state for scan/processing status shown in the menu bar.
@MainActor
class ScanState: ObservableObject {
    let appState: AppState

    @Published var isScanning = false
    /// Persistent pause state — survives across app launches.
    /// When true, both manual `scanAllLibraries()` calls and watcher-driven
    /// rescans are no-ops, and the menu bar icon switches to its paused
    /// variant. Toggled via `pauseSync()` / `resumeSync()`.
    @Published var isPaused: Bool {
        didSet { UserDefaults.standard.set(isPaused, forKey: "scanPaused") }
    }
    @Published var discoveredFiles = 0   // found on disk
    @Published var serverFiles = 0       // fetched from server
    @Published var totalFiles = 0        // files needing work (new + changed)
    @Published var processedFiles = 0    // files completed
    @Published var skippedFiles = 0      // unchanged files
    @Published var pendingDeletions = 0  // server assets not on disk
    @Published var errorCount = 0         // failed files
    @Published var lastError = ""         // most recent error detail
    @Published var phase = ""            // current scan phase
    @Published var lastScanDate: Date?
    @Published var scanError: String?
    @Published var isWatching = false

    /// Per-library scan results from the last run.
    @Published var lastResults: [String: ScanPipeline.ScanResult] = [:]

    /// Per-library health status. Populated by `scanAllLibraries` as
    /// it iterates. `.offline` is sticky across scans (it's only
    /// cleared by a successful root-existence check on the next
    /// scan). Consumed by `LibrarySidebar` to render the colored
    /// status dot and by `BrowseWindow` to decide when to show its
    /// "background activity" banner.
    @Published var libraryStatus: [String: LibraryStatus] = [:]

    private var watcher: LibraryWatcher?
    private var pipelines: [String: ScanPipeline] = [:]
    private var pollTask: Task<Void, Never>?

    /// Set when the watcher fires while a scan is already running, so we
    /// can rescan once the in-flight scan completes. Without this, file
    /// changes during a long scan would be silently dropped — the watcher
    /// fires once, ScanState ignores it (`isScanning` guard), and there's
    /// no second event to re-trigger.
    private var pendingRescan = false

    init(appState: AppState) {
        self.appState = appState
        self.isPaused = UserDefaults.standard.bool(forKey: "scanPaused")
    }

    /// Status text for the menu bar.
    var statusText: String {
        if isScanning {
            if isPaused {
                return "Paused (\(processedFiles) of \(totalFiles))"
            }
            if phase.hasPrefix("error") {
                return phase
            }
            if phase.hasPrefix("enriching") {
                let step = phase.replacingOccurrences(of: "enriching: ", with: "")
                if totalFiles == 0 {
                    return "Enrichment: \(step) (nothing to do)"
                }
                return "Enrichment: \(step) \(processedFiles) of \(totalFiles)"
            }
            switch phase {
            case "discovering":
                return "Syncing... (\(discoveredFiles) found)"
            case "checking server":
                if serverFiles > 0 {
                    return "Checking server... (\(serverFiles) assets)"
                }
                return "Checking server..."
            case "processing":
                if totalFiles == 0 {
                    return "Up to date (\(skippedFiles) unchanged)"
                }
                return "Syncing \(processedFiles) of \(totalFiles) new files (\(skippedFiles) unchanged)"
            case "deleting":
                return "Removing \(pendingDeletions) deleted files..."
            case "volume unavailable":
                return "Volume unavailable — skipping"
            default:
                return "Syncing..."
            }
        }
        if isPaused {
            return "Sync paused"
        }
        if isWatching {
            let count = appState.libraries.count
            return "Watching \(count) librar\(count == 1 ? "y" : "ies")"
        }
        return "Not syncing"
    }

    /// Start watching all library root paths. The watcher itself is
    /// independent of `isPaused` — it always runs so we don't lose FSEvents
    /// while paused — but its callback no-ops when paused.
    ///
    /// On a fresh `startWatching()` we also kick a one-shot scan so the app
    /// actually syncs at launch instead of waiting for a filesystem event.
    /// This was the "I just opened the app and nothing happens" gap.
    func startWatching() {
        guard appState.isAuthenticated, !appState.libraries.isEmpty else { return }

        let paths = appState.libraries.map(\.rootPath)
        watcher = LibraryWatcher { [weak self] in
            Task { @MainActor in
                guard let self, !self.isPaused else { return }
                self.scanAllLibraries()
            }
        }
        watcher?.watch(paths: paths)
        isWatching = true

        // Kick an initial scan so we sync at launch. Gated on `!isPaused`
        // so the persistent pause survives across launches.
        if !isPaused {
            scanAllLibraries()
        }
    }

    /// Stop watching.
    func stopWatching() {
        watcher?.stop()
        watcher = nil
        isWatching = false
    }

    /// Manually trigger a scan of all libraries. No-op if paused or already
    /// scanning. If already scanning, sets `pendingRescan` so the watcher's
    /// "events arrived during a scan" case still runs another pass after
    /// the current one completes.
    func scanAllLibraries() {
        guard !isPaused, let client = appState.client else { return }
        guard !isScanning else {
            pendingRescan = true
            return
        }

        Task {
            isScanning = true
            processedFiles = 0
            totalFiles = 0
            scanError = nil
            lastResults = [:]

            // Fetch tenant filters once (shared across all libraries)
            let tenantFilters: TenantFilterDefaultsResponse
            do {
                tenantFilters = try await client.get("/v1/tenant/filter-defaults")
            } catch {
                tenantFilters = TenantFilterDefaultsResponse(includes: [], excludes: [])
            }

            // Process each library fully (scan + enrich) before moving
            // on to the next. This used to be two separate loops
            // (scan all, then enrich all), but that made the per-
            // library status indicator flap weirdly — a library would
            // go .busy → .idle → .busy as it moved between phases. The
            // new ordering keeps `.busy` contiguous per library, which
            // is also closer to the natural "one library at a time"
            // mental model.
            for library in appState.libraries {
                guard isScanning else { break }

                // Root-existence check. An unmounted volume or a
                // moved folder shouldn't abort the whole sweep — mark
                // the library offline and continue to the next one.
                guard FileManager.default.fileExists(atPath: library.rootPath) else {
                    libraryStatus[library.libraryId] = .offline
                    continue
                }
                libraryStatus[library.libraryId] = .busy

                // Fetch library-specific filters
                let libraryFilters: LibraryFiltersResponse
                do {
                    libraryFilters = try await client.get(
                        "/v1/libraries/\(library.libraryId)/filters"
                    )
                } catch {
                    libraryFilters = LibraryFiltersResponse(includes: [], excludes: [])
                }

                let pathFilter = PathFilter(tenant: tenantFilters, library: libraryFilters)

                // --- Scan ---
                let pipeline = ScanPipeline(
                    client: client,
                    libraryId: library.libraryId,
                    rootPath: library.rootPath,
                    pathFilter: pathFilter
                )
                pipelines[library.libraryId] = pipeline
                startProgressPolling(pipeline: pipeline)
                let result = await pipeline.run()
                lastResults[library.libraryId] = result
                stopProgressPolling()
                pipelines.removeValue(forKey: library.libraryId)

                // --- Enrich ---
                phase = "enriching"
                let enrichPipeline = EnrichmentPipeline(
                    client: client,
                    libraryId: library.libraryId,
                    visionApiUrl: appState.resolvedVisionApiUrl,
                    visionApiKey: appState.resolvedVisionApiKey,
                    visionModelId: appState.resolvedVisionModelId
                )
                startEnrichProgressPolling(pipeline: enrichPipeline)
                let enrichResult = await enrichPipeline.run()
                stopProgressPolling()

                if enrichResult.errors > 0 {
                    scanError = "\(enrichResult.errors) enrichment errors"
                }

                libraryStatus[library.libraryId] = .idle
            }

            isScanning = false
            lastScanDate = Date()

            // Refresh the library list in browse view
            await appState.refreshLibraries()

            // If the watcher fired while we were running, do one more pass
            // to pick up the changes that arrived mid-scan. Gated on the
            // pause flag so a user pausing mid-scan still gets a clean
            // shutdown.
            if pendingRescan && !isPaused {
                pendingRescan = false
                scanAllLibraries()
            } else {
                pendingRescan = false
            }
        }
    }

    private var cancelled: Bool { !isScanning }

    /// Pause sync. Persistent across app launches. If a scan is in progress
    /// it will pause gracefully (the pipeline's own pause/resume controls
    /// stop accepting new work). The watcher keeps running so we don't lose
    /// FSEvents while paused — its callback just no-ops.
    func pauseSync() {
        isPaused = true
        for pipeline in pipelines.values {
            Task { await pipeline.pause() }
        }
    }

    /// Resume sync. Clears the persistent pause, resumes any in-flight
    /// pipeline, and (if no scan is running) kicks a one-shot scan so the
    /// user immediately sees activity instead of waiting for the next
    /// FSEvents callback.
    func resumeSync() {
        isPaused = false
        for pipeline in pipelines.values {
            Task { await pipeline.resume() }
        }
        if !isScanning {
            scanAllLibraries()
        }
    }

    /// Cancel all active scans.
    func cancelScanning() {
        for pipeline in pipelines.values {
            Task { await pipeline.cancel() }
        }
        pipelines.removeAll()
        isScanning = false
    }

    // MARK: - Progress polling

    private func startProgressPolling(pipeline: ScanPipeline) {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                let discovered = await pipeline.discoveredFiles
                let server = await pipeline.serverFiles
                let total = await pipeline.totalFiles
                let processed = await pipeline.processedFiles
                let skipped = await pipeline.skippedFiles
                let deletions = await pipeline.pendingDeletions
                let errors = await pipeline.errorCount
                let error = await pipeline.lastError
                let currentPhase = await pipeline.phase
                self.discoveredFiles = discovered
                self.serverFiles = server
                self.totalFiles = total
                self.processedFiles = processed
                self.skippedFiles = skipped
                self.pendingDeletions = deletions
                self.errorCount = errors
                self.lastError = error
                self.phase = currentPhase
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
    }

    private func startEnrichProgressPolling(pipeline: EnrichmentPipeline) {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                let total = await pipeline.totalItems
                let processed = await pipeline.processedItems
                let errors = await pipeline.errorCount
                let error = await pipeline.lastError
                let currentPhase = await pipeline.phase
                self.totalFiles = total
                self.processedFiles = processed
                self.errorCount = errors
                self.lastError = error
                self.phase = "enriching: \(currentPhase)"
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
    }

    private func stopProgressPolling() {
        pollTask?.cancel()
        pollTask = nil
    }
}
