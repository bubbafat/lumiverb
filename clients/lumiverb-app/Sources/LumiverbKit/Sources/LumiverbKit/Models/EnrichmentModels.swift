import Foundation

// MARK: - Enrichment operations

/// Operations available for re-enrichment of assets.
public enum EnrichmentOperation: String, CaseIterable, Sendable {
    case faces = "Detect Faces"
    case embeddings = "Generate Embeddings"
    case ocr = "Extract Text"
    case vision = "Generate Descriptions"
    case videoPreview = "Generate Preview"
}

// MARK: - OCR

/// Request body for `POST /v1/assets/batch-ocr`.
public struct BatchOCRRequest: Encodable, Sendable {
    public struct Item: Encodable, Sendable {
        public let assetId: String
        public let ocrText: String

        public init(assetId: String, ocrText: String) {
            self.assetId = assetId
            self.ocrText = ocrText
        }
    }

    public let items: [Item]
    public init(items: [Item]) { self.items = items }
}

/// Response from `POST /v1/assets/batch-ocr`.
public struct BatchOCRResponse: Decodable, Sendable {
    public let updated: Int
    public let skipped: Int
}

// MARK: - Embeddings

/// Request body for `POST /v1/assets/batch-embeddings`.
public struct BatchEmbeddingsRequest: Encodable, Sendable {
    public struct Item: Encodable, Sendable {
        public let assetId: String
        public let modelId: String
        public let modelVersion: String
        public let vector: [Float]

        public init(assetId: String, modelId: String, modelVersion: String, vector: [Float]) {
            self.assetId = assetId
            self.modelId = modelId
            self.modelVersion = modelVersion
            self.vector = vector
        }
    }

    public let items: [Item]
    public init(items: [Item]) { self.items = items }
}

/// Response from `POST /v1/assets/batch-embeddings`.
public struct BatchEmbeddingsResponse: Decodable, Sendable {
    public let updated: Int
    public let skipped: Int
}

// MARK: - Faces

/// Request body for `POST /v1/assets/{asset_id}/faces`.
public struct FacesSubmitRequest: Encodable, Sendable {
    public let detectionModel: String
    public let detectionModelVersion: String
    public let faces: [FaceItem]

    public struct FaceItem: Encodable, Sendable {
        public let boundingBox: BoundingBox
        public let detectionConfidence: Float
        public let embedding: [Float]?

        public init(boundingBox: BoundingBox, detectionConfidence: Float, embedding: [Float]? = nil) {
            self.boundingBox = boundingBox
            self.detectionConfidence = detectionConfidence
            self.embedding = embedding
        }
    }

    public struct BoundingBox: Encodable, Sendable {
        public let x1: Float
        public let y1: Float
        public let x2: Float
        public let y2: Float

        public init(x1: Float, y1: Float, x2: Float, y2: Float) {
            self.x1 = x1; self.y1 = y1; self.x2 = x2; self.y2 = y2
        }
    }

    public init(detectionModel: String, detectionModelVersion: String, faces: [FaceItem]) {
        self.detectionModel = detectionModel
        self.detectionModelVersion = detectionModelVersion
        self.faces = faces
    }
}

/// Response from `POST /v1/assets/{asset_id}/faces`.
public struct FacesSubmitResponse: Decodable, Sendable {
    public let faceCount: Int
    public let faceIds: [String]
}

// MARK: - Transcript

/// Request body for `POST /v1/assets/{asset_id}/transcript`.
public struct TranscriptSubmitRequest: Encodable, Sendable {
    public let srt: String
    public let language: String
    public let source: String

    public init(srt: String, language: String, source: String = "whisper") {
        self.srt = srt
        self.language = language
        self.source = source
    }
}

/// Response from `POST /v1/assets/{asset_id}/transcript`.
public struct TranscriptSubmitResponse: Decodable, Sendable {
    public let assetId: String
    public let status: String
}

// MARK: - Vision (descriptions/tags)

/// Request body for `POST /v1/assets/batch-vision`.
public struct BatchVisionRequest: Encodable, Sendable {
    public struct Item: Encodable, Sendable {
        public let assetId: String
        public let modelId: String
        public let modelVersion: String
        public let description: String
        public let tags: [String]

        public init(assetId: String, modelId: String, modelVersion: String, description: String, tags: [String]) {
            self.assetId = assetId
            self.modelId = modelId
            self.modelVersion = modelVersion
            self.description = description
            self.tags = tags
        }
    }

    public let items: [Item]
    public init(items: [Item]) { self.items = items }
}

/// Response from `POST /v1/assets/batch-vision`.
public struct BatchVisionResponse: Decodable, Sendable {
    public let updated: Int
    public let skipped: Int
}

/// Response from `GET /v1/tenant/context`.
public struct TenantContext: Decodable, Sendable {
    public let tenantId: String
    public let visionApiUrl: String
    public let visionApiKey: String
    public let visionModelId: String
}

// MARK: - Repair Summary

/// Response from `GET /v1/assets/repair-summary`.
public struct RepairSummary: Decodable, Sendable {
    public let totalAssets: Int
    public let missingProxy: Int
    public let missingExif: Int
    public let missingVision: Int
    public let missingEmbeddings: Int
    public let missingFaces: Int
    public let missingFaceEmbeddings: Int
    public let missingOcr: Int
    public let missingVideoScenes: Int
    public let missingSceneVision: Int
    public let missingTranscription: Int
    public let staleSearchSync: Int
}
