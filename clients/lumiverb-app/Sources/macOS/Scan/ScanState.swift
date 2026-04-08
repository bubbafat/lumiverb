import SwiftUI
import LumiverbKit

/// Observable state for scan/processing status shown in the menu bar.
@MainActor
class ScanState: ObservableObject {
    let appState: AppState

    @Published var isScanning = false
    @Published var isPaused = false
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

    private var watcher: LibraryWatcher?
    private var pipelines: [String: ScanPipeline] = [:]
    private var pollTask: Task<Void, Never>?

    init(appState: AppState) {
        self.appState = appState
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
                return "Scanning files... (\(discoveredFiles) found)"
            case "checking server":
                if serverFiles > 0 {
                    return "Checking server... (\(serverFiles) assets)"
                }
                return "Checking server..."
            case "processing":
                if totalFiles == 0 {
                    return "Up to date (\(skippedFiles) unchanged)"
                }
                return "Processing \(processedFiles) of \(totalFiles) new files (\(skippedFiles) unchanged)"
            case "deleting":
                return "Removing \(pendingDeletions) deleted files..."
            case "volume unavailable":
                return "Volume unavailable — skipping"
            default:
                return "Scanning..."
            }
        }
        if isWatching {
            let count = appState.libraries.count
            return "Watching \(count) librar\(count == 1 ? "y" : "ies")"
        }
        return "Not scanning"
    }

    /// Start watching all library root paths.
    func startWatching() {
        guard appState.isAuthenticated, !appState.libraries.isEmpty else { return }

        let paths = appState.libraries.map(\.rootPath)
        watcher = LibraryWatcher { [weak self] in
            Task { @MainActor in
                self?.scanAllLibraries()
            }
        }
        watcher?.watch(paths: paths)
        isWatching = true
    }

    /// Stop watching.
    func stopWatching() {
        watcher?.stop()
        watcher = nil
        isWatching = false
    }

    /// Manually trigger a scan of all libraries.
    func scanAllLibraries() {
        guard !isScanning, let client = appState.client else { return }

        Task {
            isScanning = true
            isPaused = false
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

            for library in appState.libraries {
                guard isScanning else { break }

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

                let pipeline = ScanPipeline(
                    client: client,
                    libraryId: library.libraryId,
                    rootPath: library.rootPath,
                    pathFilter: pathFilter
                )
                pipelines[library.libraryId] = pipeline

                // Poll progress
                startProgressPolling(pipeline: pipeline)

                let result = await pipeline.run()
                lastResults[library.libraryId] = result

                stopProgressPolling()
                pipelines.removeValue(forKey: library.libraryId)
            }

            // Run enrichment after scan for each library
            for library in appState.libraries {
                if cancelled { break }

                phase = "enriching"
                let enrichPipeline = EnrichmentPipeline(
                    client: client,
                    libraryId: library.libraryId
                )
                startEnrichProgressPolling(pipeline: enrichPipeline)
                let enrichResult = await enrichPipeline.run()
                stopProgressPolling()

                if enrichResult.errors > 0 {
                    scanError = "\(enrichResult.errors) enrichment errors"
                }
            }

            isScanning = false
            lastScanDate = Date()

            // Refresh the library list in browse view
            await appState.refreshLibraries()
        }
    }

    private var cancelled: Bool { !isScanning }

    /// Pause all active scans.
    func pauseScanning() {
        isPaused = true
        for pipeline in pipelines.values {
            Task { await pipeline.pause() }
        }
    }

    /// Resume all paused scans.
    func resumeScanning() {
        isPaused = false
        for pipeline in pipelines.values {
            Task { await pipeline.resume() }
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
