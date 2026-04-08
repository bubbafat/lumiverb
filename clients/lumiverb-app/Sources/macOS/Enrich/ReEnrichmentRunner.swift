import Foundation
import AVFoundation
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
    private let libraryRootPath: String?
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
        libraryRootPath: String? = nil,
        visionApiUrl: String = "",
        visionApiKey: String = "",
        visionModelId: String = ""
    ) {
        self.client = client
        self.libraryId = libraryId
        self.libraryRootPath = libraryRootPath
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

        let imageAssets = assets.filter { $0.mediaType == "image" }
        let videoAssets = assets.filter { $0.mediaType == "video" }

        // Image-only operations
        totalItems = imageAssets.count

        if operations.contains(.faces) {
            phase = "faces"
            await runFaceDetection(on: imageAssets)
        }

        if !cancelled && operations.contains(.embeddings) {
            phase = "embeddings"
            processedItems = 0
            await runEmbeddings(on: imageAssets)
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

        // Video operations
        if !cancelled && operations.contains(.videoPreview) {
            if let rootPath = libraryRootPath {
                phase = "video previews"
                processedItems = 0
                totalItems = videoAssets.count
                await runVideoPreview(on: videoAssets, rootPath: rootPath)
            } else {
                skippedOperations.append("video preview (library root path not available)")
            }
        }

        phase = "done"
        isRunning = false
        return Result(processed: totalItems - errorCount, errors: errorCount, skipped: skippedOperations)
    }

    // MARK: - Face detection

    /// Ensure ArcFace is available before running face detection with embeddings.
    private func ensureArcFace() async {
        if !ArcFaceProvider.isAvailable {
            do {
                try await ArcFaceProvider.ensureAvailable()
            } catch {
                lastError = "ArcFace download: \(error)"
            }
        }
    }

    private func runFaceDetection(on assets: [AssetPageItem]) async {
        await ensureArcFace()
        for asset in assets {
            if cancelled { break }
            guard let proxyData = await loadProxy(assetId: asset.assetId) else {
                processedItems += 1
                continue
            }

            do {
                guard let cgImage = FaceDetectionProvider.cgImage(from: proxyData) else {
                    processedItems += 1
                    continue
                }

                let faces = try FaceDetectionProvider.detectFaces(from: cgImage)

                let faceItems = faces.map { face in
                    var embedding: [Float]? = nil
                    if ArcFaceProvider.isAvailable,
                       let crop = FaceDetectionProvider.extractAlignedFaceCrop(from: cgImage, face: face) {
                        embedding = try? ArcFaceProvider.embed(faceImage: crop)
                    }
                    return FacesSubmitRequest.FaceItem(
                        boundingBox: face.boundingBox,
                        detectionConfidence: face.confidence,
                        embedding: embedding
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

    // MARK: - Embeddings (CLIP or Apple Vision)

    private var embeddingModelId: String {
        CLIPProvider.isAvailable ? CLIPProvider.modelId : FeaturePrintProvider.modelId
    }

    private var embeddingModelVersion: String {
        CLIPProvider.isAvailable ? CLIPProvider.modelVersion : FeaturePrintProvider.modelVersion
    }

    private func embedImage(_ data: Data) throws -> [Float] {
        if CLIPProvider.isAvailable {
            return try CLIPProvider.embed(imageData: data)
        }
        return try FeaturePrintProvider.embed(imageData: data)
    }

    private func runEmbeddings(on assets: [AssetPageItem]) async {
        var batch: [BatchEmbeddingsRequest.Item] = []

        for asset in assets {
            if cancelled { break }
            guard let proxyData = await loadProxy(assetId: asset.assetId) else {
                processedItems += 1
                continue
            }

            do {
                let vector = try embedImage(proxyData)
                batch.append(BatchEmbeddingsRequest.Item(
                    assetId: asset.assetId,
                    modelId: embeddingModelId,
                    modelVersion: embeddingModelVersion,
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

    // MARK: - Video preview generation

    private struct ArtifactUploadResponse: Decodable {
        let key: String
        let sha256: String
    }

    private func runVideoPreview(on assets: [AssetPageItem], rootPath: String) async {
        for asset in assets {
            if cancelled { break }

            let fullPath = (rootPath as NSString).appendingPathComponent(asset.relPath)
            let sourceURL = URL(fileURLWithPath: fullPath)

            guard FileManager.default.fileExists(atPath: fullPath) else {
                processedItems += 1
                continue
            }

            do {
                let previewData = try await VideoPreviewGenerator.generatePreview(sourceURL: sourceURL)

                let _: ArtifactUploadResponse = try await client.postMultipart(
                    "/v1/assets/\(asset.assetId)/artifacts/video_preview",
                    fields: [:],
                    fileField: "file",
                    fileData: previewData,
                    fileName: "\(asset.assetId).mp4",
                    mimeType: "video/mp4"
                )
            } catch {
                lastError = "\(asset.relPath): \(error)"
                errorCount += 1
            }

            processedItems += 1
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
