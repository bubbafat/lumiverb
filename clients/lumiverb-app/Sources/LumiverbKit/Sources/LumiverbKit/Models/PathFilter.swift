import Foundation

/// Path filter rule from the server.
public struct PathFilterRule: Decodable, Sendable {
    public let pattern: String
    public let filterType: String  // "include" or "exclude"
}

/// Merged path filter that applies tenant + library rules.
///
/// Priority (highest first):
/// 1. Library exclude → BLOCKED
/// 2. Library include → ALLOWED
/// 3. Tenant exclude → BLOCKED
/// 4. Tenant includes exist but no match → BLOCKED
/// 5. Default → ALLOWED
public struct PathFilter: Sendable {
    public let tenantIncludes: [String]
    public let tenantExcludes: [String]
    public let libraryIncludes: [String]
    public let libraryExcludes: [String]

    public init(
        tenantRules: [PathFilterRule] = [],
        libraryRules: [PathFilterRule] = []
    ) {
        self.tenantIncludes = tenantRules.filter { $0.filterType == "include" }.map(\.pattern)
        self.tenantExcludes = tenantRules.filter { $0.filterType == "exclude" }.map(\.pattern)
        self.libraryIncludes = libraryRules.filter { $0.filterType == "include" }.map(\.pattern)
        self.libraryExcludes = libraryRules.filter { $0.filterType == "exclude" }.map(\.pattern)
    }

    /// Initialize from server filter responses.
    public init(
        tenant: TenantFilterDefaultsResponse,
        library: LibraryFiltersResponse
    ) {
        self.tenantIncludes = tenant.includes.map(\.pattern)
        self.tenantExcludes = tenant.excludes.map(\.pattern)
        self.libraryIncludes = library.includes.map(\.pattern)
        self.libraryExcludes = library.excludes.map(\.pattern)
    }

    /// Check if a relative path is allowed by the filter rules.
    public func isAllowed(_ relPath: String) -> Bool {
        let normalized = relPath.replacingOccurrences(of: "\\", with: "/")

        // 1. Library exclude — highest priority
        for pattern in libraryExcludes {
            if globMatch(normalized, pattern: pattern) { return false }
        }

        // 2. Library include — overrides tenant
        for pattern in libraryIncludes {
            if globMatch(normalized, pattern: pattern) { return true }
        }

        // 3. Tenant exclude
        for pattern in tenantExcludes {
            if globMatch(normalized, pattern: pattern) { return false }
        }

        // 4. Tenant includes exist but no match
        if !tenantIncludes.isEmpty {
            for pattern in tenantIncludes {
                if globMatch(normalized, pattern: pattern) { return true }
            }
            return false
        }

        // 5. Default — allowed
        return true
    }

    /// Case-insensitive glob matching supporting *, ?, and **.
    ///
    /// - `*` matches any characters except `/`
    /// - `?` matches any single character except `/`
    /// - `**` matches any characters including `/` (zero or more path segments)
    private func globMatch(_ path: String, pattern: String) -> Bool {
        let p = pattern.lowercased()
        let s = path.lowercased()
        return globMatchRecursive(
            Array(s.unicodeScalars),
            Array(p.unicodeScalars),
            si: 0, pi: 0
        )
    }

    private func globMatchRecursive(
        _ s: [Unicode.Scalar], _ p: [Unicode.Scalar],
        si: Int, pi: Int
    ) -> Bool {
        var si = si
        var pi = pi

        while pi < p.count {
            if pi + 1 < p.count && p[pi] == "*" && p[pi + 1] == "*" {
                // Skip consecutive * characters
                pi += 2
                // Skip trailing /
                if pi < p.count && p[pi] == "/" { pi += 1 }
                // ** at end matches everything
                if pi >= p.count { return true }
                // Try matching remainder from every position
                for i in si...s.count {
                    if globMatchRecursive(s, p, si: i, pi: pi) {
                        return true
                    }
                }
                return false
            } else if p[pi] == "*" {
                pi += 1
                // * matches everything except /
                if pi >= p.count {
                    // * at end — match if no more slashes
                    return !s[si...].contains("/")
                }
                for i in si...s.count {
                    if i < s.count && s[i] == "/" {
                        // * doesn't cross /
                        return globMatchRecursive(s, p, si: i, pi: pi)
                    }
                    if globMatchRecursive(s, p, si: i, pi: pi) {
                        return true
                    }
                }
                return false
            } else if p[pi] == "?" {
                if si >= s.count || s[si] == "/" { return false }
                si += 1
                pi += 1
            } else {
                if si >= s.count || s[si] != p[pi] { return false }
                si += 1
                pi += 1
            }
        }

        return si >= s.count
    }
}
