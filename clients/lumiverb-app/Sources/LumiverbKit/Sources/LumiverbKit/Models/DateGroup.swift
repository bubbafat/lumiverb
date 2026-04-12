import Foundation

/// A group of assets sharing the same calendar date, used to render
/// date-separated sections in the browse grid. Mirrors the web client's
/// `groupByDate.ts` logic exactly: same date-field priority, same
/// "Unknown date" fallback, same most-recent-first sort order.
public struct DateGroup: Sendable, Equatable {
    /// Human-readable date label, e.g. "Tuesday, June 4, 2024" or "Unknown date".
    public let label: String
    /// ISO date string (YYYY-MM-DD) or nil for the "Unknown date" group.
    public let dateISO: String?
    /// Assets in this group, in their original order.
    public let assets: [AssetPageItem]

    public init(label: String, dateISO: String?, assets: [AssetPageItem]) {
        self.label = label
        self.dateISO = dateISO
        self.assets = assets
    }
}

// MARK: - Grouping

/// ISO8601 parsers (reused across calls). Thread-safe: DateFormatter and
/// ISO8601DateFormatter are documented as safe for concurrent reads once
/// fully configured. The `nonisolated(unsafe)` silences the static-storage
/// check without changing runtime behavior.
nonisolated(unsafe) private let iso8601WithFrac: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return f
}()
nonisolated(unsafe) private let iso8601NoFrac: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    return f
}()

private func parseISO8601(_ string: String) -> Date? {
    iso8601WithFrac.date(from: string) ?? iso8601NoFrac.date(from: string)
}

/// Label formatter: "Tuesday, June 4, 2024"
private let labelFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "EEEE, MMMM d, yyyy"
    f.locale = Locale(identifier: "en_US")
    return f
}()

/// ISO date formatter: "2024-06-04"
private let isoDateFormatter: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd"
    f.locale = Locale(identifier: "en_US_POSIX")
    return f
}()

/// Generic date bucket for any item type. Used by `DateGroupedGrid` so
/// all four iOS grids (Photos / Collections / People / Favorites) can
/// share the same date-grouping logic regardless of their item shape.
public struct DateBucket<Item>: Identifiable {
    public let label: String
    public let dateISO: String?
    public let items: [Item]
    public let assetIds: [String]  // for select-all-in-date

    public var id: String { label }
}

/// Internal accumulator for `bucketByDate`. Defined at file scope
/// because Swift doesn't allow generic structs nested inside generic
/// functions.
fileprivate struct DateBucketAccumulator<I> {
    var label: String
    var dateISO: String?
    var items: [I] = []
    var assetIds: [String] = []
    var latestTimestamp: Date?
}

/// Generic version of `groupAssetsByDate`. Each item supplies its
/// `takenAt` (or fallback) string via a closure and its assetId via
/// another. Used by `DateGroupedGrid` to bucket any item shape.
public func bucketByDate<Item>(
    _ items: [Item],
    dateString: (Item) -> String?,
    assetId: (Item) -> String
) -> [DateBucket<Item>] {
    guard !items.isEmpty else { return [] }

    var groupsByLabel: [String: DateBucketAccumulator<Item>] = [:]
    var insertionOrder: [String] = []

    for item in items {
        let dateStr = dateString(item)
        var label = "Unknown date"
        var dateISO: String?
        var timestamp: Date?

        if let dateStr, let parsed = parseISO8601(dateStr) {
            label = labelFormatter.string(from: parsed)
            dateISO = isoDateFormatter.string(from: parsed)
            timestamp = parsed
        }

        let id = assetId(item)
        if var existing = groupsByLabel[label] {
            existing.items.append(item)
            existing.assetIds.append(id)
            if let ts = timestamp {
                if let current = existing.latestTimestamp {
                    existing.latestTimestamp = max(current, ts)
                } else {
                    existing.latestTimestamp = ts
                }
            }
            groupsByLabel[label] = existing
        } else {
            groupsByLabel[label] = DateBucketAccumulator<Item>(
                label: label,
                dateISO: dateISO,
                items: [item],
                assetIds: [id],
                latestTimestamp: timestamp
            )
            insertionOrder.append(label)
        }
    }

    var groups = insertionOrder.compactMap { groupsByLabel[$0] }

    groups.sort { a, b in
        switch (a.latestTimestamp, b.latestTimestamp) {
        case (nil, nil): return false
        case (nil, _): return false
        case (_, nil): return true
        case let (aTs?, bTs?): return aTs > bTs
        }
    }

    return groups.map {
        DateBucket(
            label: $0.label,
            dateISO: $0.dateISO,
            items: $0.items,
            assetIds: $0.assetIds
        )
    }
}

/// Group assets by calendar date using `takenAt` with `createdAt` fallback.
/// Sorted most-recent-first; assets with no parseable date go into an
/// "Unknown date" group at the end.
public func groupAssetsByDate(_ assets: [AssetPageItem]) -> [DateGroup] {
    guard !assets.isEmpty else { return [] }

    struct Accumulator {
        var label: String
        var dateISO: String?
        var assets: [AssetPageItem] = []
        var latestTimestamp: Date?
    }

    var groupsByLabel: [String: Accumulator] = [:]
    var insertionOrder: [String] = []

    for asset in assets {
        let dateStr = asset.takenAt ?? asset.createdAt
        var label = "Unknown date"
        var dateISO: String?
        var timestamp: Date?

        if let dateStr, let parsed = parseISO8601(dateStr) {
            label = labelFormatter.string(from: parsed)
            dateISO = isoDateFormatter.string(from: parsed)
            timestamp = parsed
        }

        if var existing = groupsByLabel[label] {
            existing.assets.append(asset)
            if let ts = timestamp {
                if let current = existing.latestTimestamp {
                    existing.latestTimestamp = max(current, ts)
                } else {
                    existing.latestTimestamp = ts
                }
            }
            groupsByLabel[label] = existing
        } else {
            groupsByLabel[label] = Accumulator(
                label: label,
                dateISO: dateISO,
                assets: [asset],
                latestTimestamp: timestamp
            )
            insertionOrder.append(label)
        }
    }

    var groups = insertionOrder.compactMap { groupsByLabel[$0] }

    groups.sort { a, b in
        switch (a.latestTimestamp, b.latestTimestamp) {
        case (nil, nil): return false
        case (nil, _): return false   // nil goes last
        case (_, nil): return true
        case let (aTs?, bTs?): return aTs > bTs  // most recent first
        }
    }

    return groups.map { DateGroup(label: $0.label, dateISO: $0.dateISO, assets: $0.assets) }
}
