import Foundation

/// Item in a filter list (tenant or library level).
///
/// The scanner only consumes `pattern`, but the library-settings UI needs
/// the identifier + timestamp to show/delete individual filter rows.
/// `filterId` is populated on `GET /v1/libraries/{id}/filters` responses
/// and `defaultId` on `GET /v1/tenant/filter-defaults` — both are optional
/// so the same struct can decode either shape without a separate type.
public struct FilterItem: Decodable, Sendable {
    public let pattern: String
    public let filterId: String?
    public let defaultId: String?
    public let createdAt: String?

    public init(
        pattern: String,
        filterId: String? = nil,
        defaultId: String? = nil,
        createdAt: String? = nil
    ) {
        self.pattern = pattern
        self.filterId = filterId
        self.defaultId = defaultId
        self.createdAt = createdAt
    }
}

/// Response from `GET /v1/tenant/filter-defaults`.
public struct TenantFilterDefaultsResponse: Decodable, Sendable {
    public let includes: [FilterItem]
    public let excludes: [FilterItem]

    public init(includes: [FilterItem] = [], excludes: [FilterItem] = []) {
        self.includes = includes
        self.excludes = excludes
    }
}

/// Response from `GET /v1/libraries/{id}/filters`.
public struct LibraryFiltersResponse: Decodable, Sendable {
    public let includes: [FilterItem]
    public let excludes: [FilterItem]

    public init(includes: [FilterItem] = [], excludes: [FilterItem] = []) {
        self.includes = includes
        self.excludes = excludes
    }
}
