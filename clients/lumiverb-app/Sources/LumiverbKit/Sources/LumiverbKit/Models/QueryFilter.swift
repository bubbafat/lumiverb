/// Filter algebra types for the unified query system.
///
/// Each filter is a `LeafFilter { type, value }` matching the server's
/// `?f=prefix:value` format. The `type` field matches the server's filter
/// prefix (e.g., "camera_make", "media", "iso", "favorite").

import Foundation

// MARK: - Core Types

/// A single filter predicate. Matches the server's LeafFilter serialization.
public struct LeafFilter: Codable, Equatable, Sendable, Hashable {
    public let type: String
    public let value: String

    public init(type: String, value: String) {
        self.type = type
        self.value = value
    }
}

/// A filter type descriptor from GET /v1/filters/capabilities.
public struct FilterCapability: Codable, Equatable, Sendable {
    public let prefix: String
    public let label: String
    public let valueKind: String
    public let faceted: Bool?
    public let enumValues: [String]?

    enum CodingKeys: String, CodingKey {
        case prefix, label
        case valueKind = "value_kind"
        case faceted
        case enumValues = "enum_values"
    }
}

/// Saved query format for smart collections (new filter algebra).
public struct SavedQueryV2: Codable, Equatable, Sendable {
    public let filters: [LeafFilter]
    public let sort: String?
    public let direction: String?

    public init(filters: [LeafFilter], sort: String? = nil, direction: String? = nil) {
        self.filters = filters
        self.sort = sort
        self.direction = direction
    }
}

// MARK: - URL Conversion

/// Convert filters to URL query items (repeated `f=prefix:value`).
public func filtersToQueryItems(
    _ filters: [LeafFilter],
    sort: String? = nil,
    direction: String? = nil,
    after: String? = nil,
    limit: Int? = nil
) -> [URLQueryItem] {
    var items: [URLQueryItem] = []
    for f in filters {
        items.append(URLQueryItem(name: "f", value: "\(f.type):\(f.value)"))
    }
    if let sort, sort != "taken_at" {
        items.append(URLQueryItem(name: "sort", value: sort))
    }
    if let direction, direction != "desc" {
        items.append(URLQueryItem(name: "dir", value: direction))
    }
    if let after {
        items.append(URLQueryItem(name: "after", value: after))
    }
    if let limit {
        items.append(URLQueryItem(name: "limit", value: String(limit)))
    }
    return items
}

// MARK: - Filter Helpers

/// Get a filter's value by type, or nil if not present.
public func getFilterValue(_ filters: [LeafFilter], type: String) -> String? {
    filters.first(where: { $0.type == type })?.value
}

/// Set or remove a filter by type. Returns a new array.
public func setFilter(_ filters: [LeafFilter], type: String, value: String?) -> [LeafFilter] {
    var result = filters.filter { $0.type != type }
    if let value, !value.isEmpty {
        result.append(LeafFilter(type: type, value: value))
    }
    return result
}

/// Remove all filters.
public func clearFilters() -> [LeafFilter] {
    []
}

// MARK: - Range Helpers

/// Parse "200-800" or "400+" or "-1600" into (min, max).
public func parseRange(_ value: String?) -> (min: String?, max: String?) {
    guard let value, !value.isEmpty else { return (nil, nil) }
    if value.hasSuffix("+") {
        return (String(value.dropLast()), nil)
    }
    if value.hasPrefix("-") {
        return (nil, String(value.dropFirst()))
    }
    if let dash = value.firstIndex(of: "-"), dash != value.startIndex {
        return (String(value[..<dash]), String(value[value.index(after: dash)...]))
    }
    return (value, value)
}

/// Compose range back to filter value. Returns nil if both are nil.
public func composeRange(min: String?, max: String?) -> String? {
    switch (min, max) {
    case (nil, nil): return nil
    case (let lo?, nil): return "\(lo)+"
    case (nil, let hi?): return "-\(hi)"
    case (let lo?, let hi?) where lo == hi: return lo
    case (let lo?, let hi?): return "\(lo)-\(hi)"
    }
}

/// Compose near location to filter value.
public func composeNear(lat: String, lon: String, radius: String) -> String {
    "\(lat),\(lon),\(radius)"
}

/// Compose date range to filter value. Returns nil if both are nil.
public func composeDate(from: String?, to: String?) -> String? {
    guard from != nil || to != nil else { return nil }
    return "\(from ?? ""),\(to ?? "")"
}

// MARK: - Labels

/// Human-readable label for a filter.
public func filterLabel(_ filter: LeafFilter) -> String {
    switch filter.type {
    case "query":
        // No wrapping quotes — users can type literal `"phrase"` for
        // exact-match and the decorative outer quotes would collide
        // with the input, rendering as `""phrase""`.
        return "Search: \(filter.value)"
    case "media":
        return filter.value == "image" ? "Photos" : filter.value == "video" ? "Videos" : "Media: \(filter.value)"
    case "favorite":
        return filter.value == "yes" ? "Favorites" : "Not favorites"
    case "has_gps":
        return filter.value == "yes" ? "Has GPS" : "No GPS"
    case "has_faces":
        return filter.value == "yes" ? "Has faces" : "No faces"
    case "has_exposure":
        return filter.value == "yes" ? "Has exposure data" : "No exposure data"
    case "has_rating":
        return filter.value == "yes" ? "Has rating" : "No rating"
    case "has_color":
        return filter.value == "yes" ? "Has color label" : "No color label"
    case "stars":
        if filter.value.contains("-") {
            let parts = filter.value.split(separator: "-")
            if parts.count == 2 {
                return parts[0] == parts[1] ? "\(parts[0]) star\(parts[0] == "1" ? "" : "s")" : "\(parts[0])–\(parts[1]) stars"
            }
        }
        if filter.value.hasSuffix("+") { return "\(filter.value.dropLast())+ stars" }
        return "\(filter.value) star\(filter.value == "1" ? "" : "s")"
    case "iso":
        if filter.value.contains("-") {
            let parts = filter.value.split(separator: "-")
            return "ISO \(parts[0])–\(parts.count > 1 ? String(parts[1]) : "")"
        }
        if filter.value.hasSuffix("+") { return "ISO \(filter.value.dropLast())+" }
        return "ISO \(filter.value)"
    case "date":
        return "Date: \(filter.value.replacingOccurrences(of: ",", with: " – "))"
    case "color":
        return "Color: \(filter.value)"
    case "tag":
        return "Tag: \(filter.value)"
    case "near":
        return "Near location"
    case "person":
        return "Person filter"
    case "camera_make":
        return "Camera: \(filter.value)"
    case "camera_model":
        return "Model: \(filter.value)"
    case "lens":
        return "Lens: \(filter.value)"
    case "path":
        return "Path: \(filter.value)"
    default:
        return "\(filter.type): \(filter.value)"
    }
}
