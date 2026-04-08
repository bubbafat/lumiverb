import Foundation
import LumiverbKit

/// Runs enrichment on a specific set of assets, regardless of their current
/// enrichment state. Used for re-enrichment triggered by user action (context
/// menu, lightbox actions).
///
/// Unlike `EnrichmentPipeline`, this does NOT query for "missing" assets —
/// it processes exactly the assets passed to `run()`. Server endpoints use
/// upsert/replace semantics, so re-submitting is safe.
actor ReEnrichmentRunner {
    private let client: APIClient
    private let libraryId: String
    private let visionApiUrl: String
    private let visionApiKey: String
    private let visionModelId: String

    private(set) var totalItems = 0
    private(set) var processedItems = 0
    private(set) var errorCount = 0
    private(set) var lastError = ""
    private(set) var phase = ""
    private(set) var skippedOperations: [String] = []
    private(set) var isRunning = false
    private var cancelled = false

    struct Result: Sendable {
        let processed: Int
        let errors: Int
        let skipped: [String]
    }

    init(
        client: APIClient,
        libraryId: String,
        visionApiUrl: String = "",
        visionApiKey: String = "",
        visionModelId: String = ""
    ) {
        self.client = client
        self.libraryId = libraryId
        self.visionApiUrl = visionApiUrl
        self.visionApiKey = visionApiKey
        self.visionModelId = visionModelId
    }

    func cancel() { cancelled = true }

    /// Run the requested enrichment operations on the given assets.
    func run(assets: [AssetPageItem], operations: Set<EnrichmentOperation>) async -> Result {
        guard !isRunning else { return Result(processed: 0, errors: 0, skipped: []) }
        isRunning = true
        cancelled = false
        errorCount = 0
        processedItems = 0
        skippedOperations = []

        // Filter to images only (faces/embeddings/OCR don't apply to video)
        let imageAssets = assets.filter { $0.mediaType == "image" }
        totalItems = imageAssets.count

        if operations.contains(.faces) {
            phase = "faces"
            await runFaceDetection(on: imageAssets)
        }

        if !cancelled && operations.contains(.embeddings) {
            if CLIPProvider.isAvailable {
                phase = "embeddings"
                processedItems = 0
                await runEmbeddings(on: imageAssets)
            } else {
                skippedOperations.append("embeddings (CLIP model not installed)")
            }
        }

        if !cancelled && operations.contains(.ocr) {
            phase = "ocr"
            processedItems = 0
            await runOCR(on: imageAssets)
        }

        if !cancelled && operations.contains(.vision) {
            if VisionProvider.isConfigured(apiURL: visionApiUrl, modelId: visionModelId) {
                phase = "vision"
                processedItems = 0
                await runVision(on: imageAssets)
            } else {
                skippedOperations.append("vision (not configured — set API URL in Settings)")
            }
        }

        phase = "done"
        isRunning = false
        return Result(processed: totalItems - errorCount, errors: errorCount, skipped: skippedOperations)
    }

    // MARK: - Face detection

    private func runFaceDetection(on assets: [AssetPageItem]) async {
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
                        embedding: nil
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
            } catch {
                lastError = "\(asset.relPath): \(error)"
                errorCount += 1
            }

            processedItems += 1
        }
    }

    // MARK: - CLIP embeddings

    private func runEmbeddings(on assets: [AssetPageItem]) async {
        var batch: [BatchEmbeddingsRequest.Item] = []

        for asset in assets {
            if cancelled { break }
            guard let proxyData = await loadProxy(assetId: asset.assetId) else {
                processedItems += 1
                continue
            }

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

            if batch.count >= 50 {
                await submitEmbeddingBatch(batch)
                batch.removeAll()
            }
        }

        if !batch.isEmpty {
            await submitEmbeddingBatch(batch)
        }
    }

    private func submitEmbeddingBatch(_ items: [BatchEmbeddingsRequest.Item]) async {
        do {
            let _: BatchEmbeddingsResponse = try await client.post(
                "/v1/assets/batch-embeddings",
                body: BatchEmbeddingsRequest(items: items)
            )
        } catch {
            lastError = "Embedding batch submit: \(error)"
            errorCount += 1
        }
    }

    // MARK: - OCR

    private func runOCR(on assets: [AssetPageItem]) async {
        var batch: [BatchOCRRequest.Item] = []

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
                    ocrText: text
                ))
            } catch {
                lastError = "\(asset.relPath): \(error)"
                errorCount += 1
            }

            processedItems += 1

            if batch.count >= 50 {
                await submitOCRBatch(batch)
                batch.removeAll()
            }
        }

        if !batch.isEmpty {
            await submitOCRBatch(batch)
        }
    }

    private func submitOCRBatch(_ items: [BatchOCRRequest.Item]) async {
        do {
            let _: BatchOCRResponse = try await client.post(
                "/v1/assets/batch-ocr",
                body: BatchOCRRequest(items: items)
            )
        } catch {
            lastError = "OCR batch submit: \(error)"
            errorCount += 1
        }
    }

    // MARK: - Vision AI descriptions

    private func runVision(on assets: [AssetPageItem]) async {
        var batch: [BatchVisionRequest.Item] = []

        for asset in assets {
            if cancelled { break }
            guard let proxyData = await loadProxy(assetId: asset.assetId) else {
                processedItems += 1
                continue
            }

            do {
                let result = try await VisionProvider.describe(
                    imageData: proxyData,
                    apiURL: visionApiUrl,
                    apiKey: visionApiKey,
                    modelId: visionModelId
                )
                batch.append(BatchVisionRequest.Item(
                    assetId: asset.assetId,
                    modelId: "openai-compatible",
                    modelVersion: visionModelId,
                    description: result.description,
                    tags: result.tags
                ))
            } catch {
                lastError = "\(asset.relPath): \(error)"
                errorCount += 1
            }

            processedItems += 1

            if batch.count >= 10 {
                await submitVisionBatch(batch)
                batch.removeAll()
            }
        }

        if !batch.isEmpty {
            await submitVisionBatch(batch)
        }
    }

    private func submitVisionBatch(_ items: [BatchVisionRequest.Item]) async {
        do {
            let _: BatchVisionResponse = try await client.post(
                "/v1/assets/batch-vision",
                body: BatchVisionRequest(items: items)
            )
        } catch {
            lastError = "Vision batch submit: \(error)"
            errorCount += 1
        }
    }

    // MARK: - Proxy loading

    private func loadProxy(assetId: String) async -> Data? {
        if let cached = ProxyCacheOnDisk.shared.get(assetId: assetId) {
            return cached
        }
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
