import Foundation
import LumiverbKit

/// Orchestrates enrichment of assets that are missing OCR, faces, or embeddings.
///
/// Runs after scan completes. Queries server for assets needing enrichment,
/// loads proxy from disk cache, runs Apple Vision / CoreML providers, and
/// submits results in batches.
///
/// Order: CLIP embeddings → Face detection → OCR
/// (matches Python CLI order — CPU-bound first, then Vision framework)
actor EnrichmentPipeline {
    private let client: APIClient
    private let libraryId: String
    private let concurrency: Int

    private(set) var phase = ""
    private(set) var totalItems = 0
    private(set) var processedItems = 0
    private(set) var errorCount = 0
    private(set) var lastError = ""
    private(set) var isRunning = false
    private var cancelled = false

    struct EnrichResult: Sendable {
        let embeddingsProcessed: Int
        let facesProcessed: Int
        let ocrProcessed: Int
        let errors: Int
    }

    init(client: APIClient, libraryId: String, concurrency: Int = 4) {
        self.client = client
        self.libraryId = libraryId
        self.concurrency = concurrency
    }

    func cancel() { cancelled = true }

    func run() async -> EnrichResult {
        guard !isRunning else {
            return EnrichResult(embeddingsProcessed: 0, facesProcessed: 0, ocrProcessed: 0, errors: 0)
        }
        isRunning = true
        cancelled = false
        processedItems = 0
        errorCount = 0

        var embedCount = 0
        var faceCount = 0
        var ocrCount = 0

        // Step 1: CLIP embeddings (if model available)
        if CLIPProvider.isAvailable {
            phase = "embeddings"
            embedCount = await runEmbeddings()
        }

        // Step 2: Face detection
        if !cancelled {
            phase = "faces"
            faceCount = await runFaceDetection()
        }

        // Step 3: OCR
        if !cancelled {
            phase = "ocr"
            ocrCount = await runOCR()
        }

        phase = "done"
        isRunning = false

        return EnrichResult(
            embeddingsProcessed: embedCount,
            facesProcessed: faceCount,
            ocrProcessed: ocrCount,
            errors: errorCount
        )
    }

    // MARK: - CLIP Embeddings

    private func runEmbeddings() async -> Int {
        let assets = await fetchAssets(missing: "missing_embeddings")
        totalItems = assets.count
        processedItems = 0
        guard !assets.isEmpty else { return 0 }

        var batch: [BatchEmbeddingsRequest.Item] = []
        var count = 0

        for asset in assets {
            if cancelled { break }
            guard let proxyData = await loadProxy(assetId: asset.assetId) else { continue }

            do {
                let vector = try CLIPProvider.embed(imageData: proxyData)
                batch.append(BatchEmbeddingsRequest.Item(
                    assetId: asset.assetId,
                    modelId: CLIPProvider.modelId,
                    modelVersion: CLIPProvider.modelVersion,
                    vector: vector
                ))
            } catch {
                lastError = "\(asset.relPath): \(error)"
                errorCount += 1
            }

            processedItems += 1

            // Submit in batches of 50
            if batch.count >= 50 {
                count += await submitEmbeddingBatch(batch)
                batch.removeAll()
            }
        }

        if !batch.isEmpty {
            count += await submitEmbeddingBatch(batch)
        }

        return count
    }

    private func submitEmbeddingBatch(_ items: [BatchEmbeddingsRequest.Item]) async -> Int {
        do {
            let response: BatchEmbeddingsResponse = try await client.post(
                "/v1/assets/batch-embeddings",
                body: BatchEmbeddingsRequest(items: items)
            )
            return response.updated
        } catch {
            lastError = "Embedding batch submit: \(error)"
            errorCount += 1
            return 0
        }
    }

    // MARK: - Face Detection

    private func runFaceDetection() async -> Int {
        let assets = await fetchAssets(missing: "missing_faces")
        totalItems = assets.count
        processedItems = 0
        guard !assets.isEmpty else { return 0 }

        var count = 0

        for asset in assets {
            if cancelled { break }
            guard let proxyData = await loadProxy(assetId: asset.assetId) else {
                processedItems += 1
                continue
            }

            do {
                let faces = try FaceDetectionProvider.detectFaces(from: proxyData)

                let faceItems = faces.map { face in
                    FacesSubmitRequest.FaceItem(
                        boundingBox: face.boundingBox,
                        detectionConfidence: face.confidence,
                        embedding: nil // ArcFace embeddings require model — submitted separately when available
                    )
                }

                let request = FacesSubmitRequest(
                    detectionModel: FaceDetectionProvider.detectionModel,
                    detectionModelVersion: FaceDetectionProvider.detectionModelVersion,
                    faces: faceItems
                )

                let _: FacesSubmitResponse = try await client.post(
                    "/v1/assets/\(asset.assetId)/faces",
                    body: request
                )
                count += 1
            } catch {
                lastError = "\(asset.relPath): \(error)"
                errorCount += 1
            }

            processedItems += 1
        }

        return count
    }

    // MARK: - OCR

    private func runOCR() async -> Int {
        let assets = await fetchAssets(missing: "missing_ocr")
        totalItems = assets.count
        processedItems = 0
        guard !assets.isEmpty else { return 0 }

        var batch: [BatchOCRRequest.Item] = []
        var count = 0

        for asset in assets {
            if cancelled { break }
            guard let proxyData = await loadProxy(assetId: asset.assetId) else {
                processedItems += 1
                continue
            }

            do {
                let text = try OCRProvider.extractText(from: proxyData)
                batch.append(BatchOCRRequest.Item(
                    assetId: asset.assetId,
                    ocrText: text // Empty string = no text found (server sets has_text=false)
                ))
            } catch {
                lastError = "\(asset.relPath): \(error)"
                errorCount += 1
            }

            processedItems += 1

            if batch.count >= 50 {
                count += await submitOCRBatch(batch)
                batch.removeAll()
            }
        }

        if !batch.isEmpty {
            count += await submitOCRBatch(batch)
        }

        return count
    }

    private func submitOCRBatch(_ items: [BatchOCRRequest.Item]) async -> Int {
        do {
            let response: BatchOCRResponse = try await client.post(
                "/v1/assets/batch-ocr",
                body: BatchOCRRequest(items: items)
            )
            return response.updated
        } catch {
            lastError = "OCR batch submit: \(error)"
            errorCount += 1
            return 0
        }
    }

    // MARK: - Helpers

    /// Fetch assets needing a specific type of enrichment.
    private func fetchAssets(missing filter: String) async -> [AssetPageItem] {
        var all: [AssetPageItem] = []
        var cursor: String?

        repeat {
            var query: [String: String] = [
                "library_id": libraryId,
                "limit": "500",
                filter: "true",
                "sort": "asset_id",
                "dir": "asc",
            ]
            if let cursor { query["after"] = cursor }

            do {
                let response: AssetPageResponse = try await client.get(
                    "/v1/assets/page", query: query
                )
                all.append(contentsOf: response.items)
                cursor = response.nextCursor
            } catch {
                lastError = "Fetch \(filter) assets: \(error)"
                errorCount += 1
                break
            }
        } while cursor != nil

        return all
    }

    /// Load proxy image from disk cache, falling back to server download.
    private func loadProxy(assetId: String) async -> Data? {
        // 1. Check disk cache (shared with Python CLI)
        if let cached = ProxyCacheOnDisk.shared.get(assetId: assetId) {
            return cached
        }

        // 2. Download from server and cache
        do {
            if let data = try await client.getData("/v1/assets/\(assetId)/proxy") {
                ProxyCacheOnDisk.shared.put(assetId: assetId, data: data)
                return data
            }
        } catch {
            // Non-fatal — skip this asset
        }

        return nil
    }
}
