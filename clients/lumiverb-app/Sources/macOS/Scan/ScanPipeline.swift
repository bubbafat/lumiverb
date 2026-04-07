import Foundation
import ImageIO
import LumiverbKit

/// Orchestrates the full scan cycle for a single library:
/// discover → classify → generate proxy → upload → handle deletes/moves.
actor ScanPipeline {
    private let client: APIClient
    private let libraryId: String
    private let rootPath: String
    private let pathFilter: PathFilter
    private let concurrency: Int

    /// Progress tracking (read from main actor via ScanState).
    private(set) var discoveredFiles = 0 // files found on disk
    private(set) var serverFiles = 0     // assets fetched from server
    private(set) var totalFiles = 0      // total needing work (new + changed)
    private(set) var processedFiles = 0  // completed so far
    private(set) var skippedFiles = 0    // unchanged
    private(set) var errorCount = 0
    private(set) var lastError: String = "" // most recent error for debugging
    private(set) var isRunning = false
    private(set) var isPaused = false
    private(set) var phase: String = ""  // "discovering", "checking server", "processing", "done"

    private var cancelled = false

    init(
        client: APIClient,
        libraryId: String,
        rootPath: String,
        pathFilter: PathFilter = PathFilter(),
        concurrency: Int = 4
    ) {
        self.client = client
        self.libraryId = libraryId
        self.rootPath = rootPath
        self.pathFilter = pathFilter
        self.concurrency = concurrency
    }

    // MARK: - Public interface

    func cancel() {
        cancelled = true
    }

    func pause() {
        isPaused = true
    }

    func resume() {
        isPaused = false
    }

    struct ScanResult: Sendable {
        let newFiles: Int
        let changedFiles: Int
        let unchangedFiles: Int
        let deletedFiles: Int
        let errors: Int
    }

    /// Run a full scan cycle. Returns summary stats.
    func run() async -> ScanResult {
        guard !isRunning else { return ScanResult(newFiles: 0, changedFiles: 0, unchangedFiles: 0, deletedFiles: 0, errors: 0) }

        isRunning = true
        cancelled = false
        processedFiles = 0
        skippedFiles = 0
        errorCount = 0

        // Phase 1: Discover local files
        phase = "discovering"
        let localFiles = discoverFiles()
        discoveredFiles = localFiles.count

        // Phase 2: Fetch existing assets from server
        phase = "checking server"
        let serverAssets = await fetchServerAssets()
        serverFiles = serverAssets.count

        // Phase 3: Classify files
        let (newFiles, changedFiles, unchangedFiles, deletedAssetIds) = classifyFiles(
            local: localFiles,
            server: serverAssets
        )

        // Only count files that actually need work
        let allWork = newFiles + changedFiles
        totalFiles = allWork.count
        skippedFiles = unchangedFiles.count

        // Phase 4: Process new + changed files concurrently
        var newCount = 0
        var changedCount = 0

        if !allWork.isEmpty {
            phase = "processing"
            await withTaskGroup(of: Bool.self) { group in
                var inflight = 0
                var index = 0

                while index < allWork.count || inflight > 0 {
                    // Wait if paused
                    while isPaused && !cancelled {
                        try? await Task.sleep(for: .milliseconds(500))
                    }
                    if cancelled { break }

                    // Launch tasks up to concurrency limit
                    while inflight < concurrency && index < allWork.count {
                        let file = allWork[index]
                        index += 1
                        inflight += 1

                        group.addTask { [weak self] in
                            guard let self else { return false }
                            let success = await self.processFile(file)
                            return success
                        }
                    }

                    // Wait for one to complete
                    if let success = await group.next() {
                        inflight -= 1
                        processedFiles += 1
                        if !success { errorCount += 1 }
                    }
                }
            }
        }

        newCount = newFiles.count
        changedCount = changedFiles.count

        // Phase 5: Handle deletions
        var deleteCount = 0
        if !deletedAssetIds.isEmpty && !cancelled {
            deleteCount = await handleDeletions(assetIds: deletedAssetIds)
        }

        phase = "done"
        isRunning = false

        return ScanResult(
            newFiles: newCount,
            changedFiles: changedCount,
            unchangedFiles: unchangedFiles.count,
            deletedFiles: deleteCount,
            errors: errorCount
        )
    }

    // MARK: - Discovery

    /// Walk the library root and find all supported files.
    private func discoverFiles() -> [DiscoveredFile] {
        var files: [DiscoveredFile] = []
        let rootURL = URL(fileURLWithPath: rootPath)
        let fm = FileManager.default

        guard let enumerator = fm.enumerator(
            at: rootURL,
            includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey, .isRegularFileKey],
            options: [.skipsHiddenFiles, .skipsPackageDescendants]
        ) else { return [] }

        for case let fileURL as URL in enumerator {
            guard let resourceValues = try? fileURL.resourceValues(forKeys: [
                .isRegularFileKey, .fileSizeKey, .contentModificationDateKey,
            ]) else { continue }

            guard resourceValues.isRegularFile == true else { continue }

            // Check extension
            guard FileExtensions.isSupported(fileURL.path) else { continue }

            // Compute relative path, normalize to NFC (macOS returns NFD)
            let relPath = String(fileURL.path.dropFirst(rootURL.path.count + 1))
                .precomposedStringWithCanonicalMapping  // NFD → NFC

            // Check path filter
            guard pathFilter.isAllowed(relPath) else { continue }

            // Check file size > 0
            let fileSize = resourceValues.fileSize ?? 0
            guard fileSize > 0 else { continue }

            files.append(DiscoveredFile(
                url: fileURL,
                relPath: relPath,
                fileSize: fileSize,
                mtime: resourceValues.contentModificationDate
            ))
        }

        return files
    }

    // MARK: - Server assets

    /// Fetch all existing assets for this library from the server (paginated).
    private func fetchServerAssets() async -> [String: ServerAsset] {
        var assets: [String: ServerAsset] = [:]
        var cursor: String?
        var pageCount = 0

        repeat {
            var query: [String: String] = [
                "library_id": libraryId,
                "limit": "500",
                "sort": "asset_id",
                "dir": "asc",
            ]
            if let cursor { query["after"] = cursor }

            do {
                let response: AssetPageResponse = try await client.get(
                    "/v1/assets/page", query: query
                )
                pageCount += 1
                for item in response.items {
                    assets[item.relPath] = ServerAsset(
                        assetId: item.assetId,
                        relPath: item.relPath,
                        fileSize: item.fileSize,
                        fileMtime: item.fileMtime,
                        sha256: item.sha256
                    )
                }
                cursor = response.nextCursor
                serverFiles = assets.count
            } catch {
                // Surface the error so it's visible
                phase = "error fetching server assets (page \(pageCount + 1)): \(error)"
                errorCount += 1
                break
            }
        } while cursor != nil

        return assets
    }

    // MARK: - Classification

    private func classifyFiles(
        local: [DiscoveredFile],
        server: [String: ServerAsset]
    ) -> (new: [DiscoveredFile], changed: [DiscoveredFile], unchanged: [DiscoveredFile], deleted: [String]) {
        var newFiles: [DiscoveredFile] = []
        var changedFiles: [DiscoveredFile] = []
        var unchangedFiles: [DiscoveredFile] = []
        var seenPaths = Set<String>()

        for file in local {
            seenPaths.insert(file.relPath)

            if let serverAsset = server[file.relPath] {
                // Exists on server — check if changed (fast: mtime + size)
                let sizeMatch = serverAsset.fileSize == file.fileSize
                let mtimeMatch: Bool = {
                    guard let localMtime = file.mtime,
                          let serverMtime = serverAsset.fileMtime else {
                        return true // No mtime to compare — trust size
                    }
                    // Parse server ISO8601 mtime and compare within 2 seconds
                    let formatter = ISO8601DateFormatter()
                    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
                    guard let serverDate = formatter.date(from: serverMtime) else {
                        return true
                    }
                    return abs(localMtime.timeIntervalSince(serverDate)) < 2.0
                }()

                if sizeMatch && mtimeMatch {
                    unchangedFiles.append(file)
                } else {
                    changedFiles.append(file)
                }
            } else {
                newFiles.append(file)
            }
        }

        // Find deleted: on server but not on disk
        let deletedAssetIds = server.values
            .filter { !seenPaths.contains($0.relPath) }
            .map(\.assetId)

        return (newFiles, changedFiles, unchangedFiles, deletedAssetIds)
    }

    // MARK: - Process a single file

    private func processFile(_ file: DiscoveredFile) async -> Bool {
        guard let mediaType = FileExtensions.mediaType(for: file.relPath) else { return false }

        do {
            // Compute source SHA first — needed for cache check and EXIF payload
            let sourceSHA = try ProxyGenerator.computeSHA256(of: file.url)

            // Check if we already have a valid cached proxy for this source
            // (shared cache with Python CLI at ~/.cache/lumiverb/proxies/)
            let cache = ProxyCacheOnDisk.shared
            // We need the asset_id from the server to use the cache, but for new files
            // we don't have one yet. For changed files we do. We'll cache after ingest.

            let proxyResult: ProxyGenerator.ProxyResult
            if mediaType == "video" {
                proxyResult = try ProxyGenerator.generateVideoPoster(at: file.url)
            } else {
                proxyResult = try ProxyGenerator.generateProxy(at: file.url)
            }

            // Build EXIF payload
            var exifPayload: [String: Any] = ["sha256": sourceSHA]
            if let exif = proxyResult.exifProperties {
                exifPayload["exif"] = exif
            }
            extractEXIFFields(from: file.url, into: &exifPayload)

            let exifJSON: String
            if let data = try? JSONSerialization.data(withJSONObject: exifPayload),
               let str = String(data: data, encoding: .utf8) {
                exifJSON = str
            } else {
                exifJSON = "{\"sha256\": \"\(sourceSHA)\"}"
            }

            // Build multipart fields
            var fields: [String: String] = [
                "library_id": libraryId,
                "rel_path": file.relPath,
                "file_size": "\(file.fileSize)",
                "media_type": mediaType,
                "width": "\(proxyResult.originalWidth)",
                "height": "\(proxyResult.originalHeight)",
                "exif": exifJSON,
            ]

            if let mtime = file.mtime {
                let formatter = ISO8601DateFormatter()
                formatter.formatOptions = [.withInternetDateTime]
                fields["file_mtime"] = formatter.string(from: mtime)
            }

            // Upload
            let response: IngestResponse = try await client.postMultipart(
                "/v1/ingest",
                fields: fields,
                fileField: "proxy",
                fileData: proxyResult.proxyData,
                fileName: "proxy.jpg",
                mimeType: "image/jpeg"
            )

            // Cache the proxy + SHA sidecar for reuse by both clients
            cache.putScan(
                assetId: response.assetId,
                jpegData: proxyResult.proxyData,
                sourceSHA256: sourceSHA
            )

            return true
        } catch {
            lastError = "\(file.relPath): \(error)"
            return false
        }
    }

    /// Extract EXIF fields from ImageIO properties for the ingest payload.
    private func extractEXIFFields(from url: URL, into payload: inout [String: Any]) {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [String: Any] else {
            return
        }

        let exif = properties["{Exif}"] as? [String: Any]
        let tiff = properties["{TIFF}"] as? [String: Any]
        let gps = properties["{GPS}"] as? [String: Any]

        // Camera
        if let make = tiff?["Make"] as? String {
            payload["camera_make"] = make
        }
        if let model = tiff?["Model"] as? String {
            payload["camera_model"] = model
        }

        // Date
        if let dateStr = exif?["DateTimeOriginal"] as? String {
            // Convert EXIF date (2024:06:15 10:30:00) to ISO8601
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyy:MM:dd HH:mm:ss"
            formatter.timeZone = TimeZone.current
            if let date = formatter.date(from: dateStr) {
                let iso = ISO8601DateFormatter()
                iso.formatOptions = [.withInternetDateTime]
                payload["taken_at"] = iso.string(from: date)
            }
        }

        // GPS
        if let lat = gps?["Latitude"] as? Double,
           let latRef = gps?["LatitudeRef"] as? String {
            payload["gps_lat"] = latRef == "S" ? -lat : lat
        }
        if let lon = gps?["Longitude"] as? Double,
           let lonRef = gps?["LongitudeRef"] as? String {
            payload["gps_lon"] = lonRef == "W" ? -lon : lon
        }

        // Exposure
        if let isoRatings = exif?["ISOSpeedRatings"] as? [Int],
           let isoValue = isoRatings.first {
            payload["iso"] = isoValue
        }
        if let exposure = exif?["ExposureTime"] as? Double {
            payload["exposure_time_us"] = Int(exposure * 1_000_000)
        }
        if let aperture = exif?["FNumber"] as? Double {
            payload["aperture"] = aperture
        }
        if let fl = exif?["FocalLength"] as? Double {
            payload["focal_length"] = fl
        }
        if let fl35 = exif?["FocalLenIn35mmFilm"] as? Int {
            payload["focal_length_35mm"] = Double(fl35)
        }
        if let lensModel = exif?["LensModel"] as? String {
            payload["lens_model"] = lensModel
        }
        if let flash = exif?["Flash"] as? Int {
            payload["flash_fired"] = (flash & 1) == 1
        }
        if let orientation = properties["Orientation"] as? Int {
            payload["orientation"] = orientation
        }
    }

    // MARK: - Deletions

    private func handleDeletions(assetIds: [String]) async -> Int {
        var totalDeleted = 0

        // Batch in groups of 500
        for start in stride(from: 0, to: assetIds.count, by: 500) {
            let end = min(start + 500, assetIds.count)
            let batch = Array(assetIds[start..<end])
            let request = BatchDeleteRequest(assetIds: batch)

            do {
                let response: BatchDeleteResponse = try await client.deleteWithBody(
                    "/v1/assets", body: request
                )
                totalDeleted += response.trashed.count
            } catch {
                errorCount += 1
            }
        }

        return totalDeleted
    }
}

// MARK: - Internal types

struct DiscoveredFile: Sendable {
    let url: URL
    let relPath: String
    let fileSize: Int
    let mtime: Date?
}

struct ServerAsset: Sendable {
    let assetId: String
    let relPath: String
    let fileSize: Int
    let fileMtime: String?
    let sha256: String?
}
