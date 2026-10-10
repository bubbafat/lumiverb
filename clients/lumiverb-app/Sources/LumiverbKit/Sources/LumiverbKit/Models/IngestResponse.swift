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
    /// Why the clips go: the server requires it.
    public enum Reason: String, Encodable, Sendable {
        /// A person's trash: deleted for good after the trash days.
        case user
        /// A scan no longer finds the files: archived, back when they are.
        case missing
    }

    public let assetIds: [String]
    public let reason: Reason
    /// For `missing`: the count a 409 `mass_missing` named, to say the files
    /// really are gone (not a volume half mounted). Omitted when nil.
    public let confirmMissing: Int?

    public init(assetIds: [String], reason: Reason, confirmMissing: Int? = nil) {
        self.assetIds = assetIds
        self.reason = reason
        self.confirmMissing = confirmMissing
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
