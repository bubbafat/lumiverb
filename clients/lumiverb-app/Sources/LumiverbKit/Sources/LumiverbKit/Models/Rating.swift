import Foundation

// MARK: - Color labels

/// The six server-recognized color labels. Matches `VALID_COLORS` in
/// `src/server/models/tenant.py`.
public enum ColorLabel: String, CaseIterable, Codable, Sendable, Equatable {
    case red, orange, yellow, green, blue, purple
}

// MARK: - Rating

/// Per-user rating on a single asset. Mirrors `RatingResponse` from
/// `PUT /v1/assets/{id}/rating`.
public struct Rating: Codable, Sendable, Equatable {
    public var favorite: Bool
    public var stars: Int
    public var color: ColorLabel?

    public init(favorite: Bool = false, stars: Int = 0, color: ColorLabel? = nil) {
        self.favorite = favorite
        self.stars = stars
        self.color = color
    }

    /// Unrated default — no favorite, 0 stars, no color.
    public static let empty = Rating()
}

// MARK: - Three-way color change

/// Encodes the server's three-way color semantics:
/// - `.unchanged`: omit `color` from the JSON body entirely
/// - `.clear`: send `"color": null` (explicit null)
/// - `.set(label)`: send `"color": "red"` etc.
public enum ColorChange: Sendable, Equatable {
    case unchanged
    case clear
    case set(ColorLabel)
}

// MARK: - Rating update body

/// Request body for `PUT /v1/assets/{id}/rating` and the batch variant.
/// Manually serialized to JSON because Swift's `Encodable` cannot
/// distinguish "key absent" from "key present with null value".
public struct RatingUpdateBody: Sendable {
    public var favorite: Bool?
    public var stars: Int?
    public var color: ColorChange

    public init(favorite: Bool? = nil, stars: Int? = nil, color: ColorChange = .unchanged) {
        self.favorite = favorite
        self.stars = stars
        self.color = color
    }

    /// Build a JSON dictionary suitable for `JSONSerialization`.
    /// - `color == .unchanged` → key omitted
    /// - `color == .clear` → key present, value NSNull
    /// - `color == .set(label)` → key present, value string
    public func jsonObject() -> [String: Any] {
        var dict: [String: Any] = [:]
        if let favorite { dict["favorite"] = favorite }
        if let stars { dict["stars"] = stars }
        switch color {
        case .unchanged: break
        case .clear: dict["color"] = NSNull()
        case .set(let label): dict["color"] = label.rawValue
        }
        return dict
    }

    /// Serialized JSON `Data` for use as an HTTP body.
    public func jsonData() throws -> Data {
        try JSONSerialization.data(withJSONObject: jsonObject())
    }
}

// MARK: - Batch rating update body

/// Request body for `PUT /v1/assets/ratings` (batch).
public struct BatchRatingUpdateBody: Sendable {
    public var assetIds: [String]
    public var favorite: Bool?
    public var stars: Int?
    public var color: ColorChange

    public init(assetIds: [String], favorite: Bool? = nil, stars: Int? = nil, color: ColorChange = .unchanged) {
        self.assetIds = assetIds
        self.favorite = favorite
        self.stars = stars
        self.color = color
    }

    public func jsonData() throws -> Data {
        var dict: [String: Any] = ["asset_ids": assetIds]
        if let favorite { dict["favorite"] = favorite }
        if let stars { dict["stars"] = stars }
        switch color {
        case .unchanged: break
        case .clear: dict["color"] = NSNull()
        case .set(let label): dict["color"] = label.rawValue
        }
        return try JSONSerialization.data(withJSONObject: dict)
    }
}

// MARK: - Response types

/// Server response from `PUT /v1/assets/{id}/rating`.
public struct RatingResponse: Codable, Sendable {
    public let assetId: String
    public let favorite: Bool
    public let stars: Int
    public let color: String?

    public var rating: Rating {
        Rating(
            favorite: favorite,
            stars: stars,
            color: color.flatMap { ColorLabel(rawValue: $0) }
        )
    }
}

/// Server response from `POST /v1/assets/ratings/lookup`.
public struct RatingLookupResponse: Codable, Sendable {
    public let ratings: [String: RatingEntry]

    public struct RatingEntry: Codable, Sendable {
        public let favorite: Bool
        public let stars: Int
        public let color: String?

        public var rating: Rating {
            Rating(
                favorite: favorite,
                stars: stars,
                color: color.flatMap { ColorLabel(rawValue: $0) }
            )
        }
    }
}

/// Server response from `PUT /v1/assets/ratings` (batch).
public struct BatchRatingResponse: Codable, Sendable {
    public let updated: Int
}
