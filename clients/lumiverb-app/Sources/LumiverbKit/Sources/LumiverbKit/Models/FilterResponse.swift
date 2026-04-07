import Foundation

/// Item in a filter list (tenant or library level).
public struct FilterItem: Decodable, Sendable {
    public let pattern: String
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
