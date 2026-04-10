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
