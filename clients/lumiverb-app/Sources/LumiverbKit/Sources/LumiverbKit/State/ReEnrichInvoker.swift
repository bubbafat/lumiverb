import Foundation

/// Result of a re-enrichment run reported back to `BrowseState`.
public struct ReEnrichmentResult: Sendable {
    public let processed: Int
    public let errors: Int
    public let skipped: [String]

    public init(processed: Int, errors: Int, skipped: [String]) {
        self.processed = processed
        self.errors = errors
        self.skipped = skipped
    }
}

/// Pluggable re-enrichment runner. `BrowseState` delegates the actual
/// work to this protocol so the LumiverbKit module doesn't need to
/// import macOS-only enrichment providers (CLIP, ArcFace, Whisper,
/// Vision).
///
/// Implementations:
/// - **macOS:** `MacReEnrichInvoker` (in `Sources/macOS/Enrich/`) wraps
///   `ReEnrichmentRunner` and forwards progress via the polling closure.
/// - **iOS:** there is no implementation. iOS is browse-only — the
///   re-enrich UI is hidden in the iOS lightbox and BrowseState's
///   re-enrich methods short-circuit when `reEnrichInvoker` is nil.
///
/// The `progress` closure is called from a polling task on the main
/// actor every ~250 ms while work is in flight. `BrowseState` uses it
/// to update its `@Published` progress fields so the lightbox banner
/// stays in sync without BrowseState needing to know how the underlying
/// runner exposes its state.
@MainActor
public protocol ReEnrichInvoker: AnyObject {
    /// Run re-enrichment on a set of assets.
    /// - Parameters:
    ///   - libraryId: tenant-scoped library identifier.
    ///   - libraryRootPath: optional root for resolving rel paths into
    ///     filesystem URLs (used by providers that need source bytes).
    ///   - assets: the assets to process. The invoker decides which
    ///     operations apply per-asset based on the operation set.
    ///   - operations: which enrichment passes to perform.
    ///   - progress: called on the main actor with (processed, total, phase).
    func reEnrich(
        libraryId: String,
        libraryRootPath: String?,
        assets: [AssetPageItem],
        operations: Set<EnrichmentOperation>,
        progress: @escaping @MainActor (_ processed: Int, _ total: Int, _ phase: String) -> Void
    ) async -> ReEnrichmentResult

    /// Cancel any in-flight run started by this invoker. Safe to call
    /// even when nothing is running.
    func cancel() async
}
