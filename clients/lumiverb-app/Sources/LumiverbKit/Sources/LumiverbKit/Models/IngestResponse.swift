import Foundation

/// Response from `POST /v1/ingest`.
public struct IngestResponse: Decodable, Sendable {
    public let assetId: String
    public let proxyKey: String?
    public let proxySha256: String?
    public let thumbnailKey: String?
    public let thumbnailSha256: String?
    public let status: String
    public let width: Int?
    public let height: Int?
    public let created: Bool
}

/// Request body for `DELETE /v1/assets` (batch soft-delete).
public struct BatchDeleteRequest: Encodable, Sendable {
    public let assetIds: [String]

    public init(assetIds: [String]) {
        self.assetIds = assetIds
    }
}

/// Response from `DELETE /v1/assets`.
public struct BatchDeleteResponse: Decodable, Sendable {
    public let trashed: [String]
    public let notFound: [String]
}

/// Request body for `POST /v1/assets/batch-moves`.
public struct BatchMoveRequest: Encodable, Sendable {
    public struct Item: Encodable, Sendable {
        public let assetId: String
        public let relPath: String

        public init(assetId: String, relPath: String) {
            self.assetId = assetId
            self.relPath = relPath
        }
    }

    public let items: [Item]

    public init(items: [Item]) {
        self.items = items
    }
}

/// Response from `POST /v1/assets/batch-moves`.
public struct BatchMoveResponse: Decodable, Sendable {
    public let updated: Int
    public let skipped: Int
}
