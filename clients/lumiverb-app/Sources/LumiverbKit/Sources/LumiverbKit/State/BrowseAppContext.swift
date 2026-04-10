import Combine
import Foundation

/// Cross-platform contract that `BrowseState` depends on for the bits of
/// app-level state it can't own itself: the API client, the library list,
/// and the enrichment config used by the lightbox's re-enrich action.
///
/// Implementations:
/// - macOS: `AppState` conforms (defined in `Sources/macOS/AppState.swift`).
///   Returns the real values from its `@Published` fields.
/// - iOS: an `iOSBrowseAppContext` adapter built around `iOSAppState`.
///   The enrichment config getters return empty strings because iOS is
///   browse-only — the re-enrich UI is gated off and the values are
///   never actually consumed. The whisper publisher emits a single
///   `false` and never updates.
///
/// Why a protocol rather than concrete: BrowseState lives in LumiverbKit
/// and shares between macOS and iOS, but each platform has its own
/// app-state holder with platform-specific persistence (UserDefaults
/// keys, menu bar mirroring, etc.). The protocol is the seam.
@MainActor
public protocol BrowseAppContext: AnyObject {
    var client: APIClient? { get }
    var libraries: [Library] { get }

    /// Whether the user has enabled whisper transcription on this client.
    /// Drives the lightbox's re-enrich menu visibility. iOS returns
    /// `false` unconditionally — there is no whisper UI on iOS.
    var whisperEnabled: Bool { get }

    /// Combine publisher that fires whenever `whisperEnabled` changes.
    /// `BrowseState.init` subscribes to this so its mirrored
    /// `@Published var whisperEnabled` updates trigger SwiftUI re-renders
    /// in views that observe `BrowseState` rather than the platform's
    /// app-state holder. iOS impls can return `Just(false).eraseToAnyPublisher()`.
    var whisperEnabledPublisher: AnyPublisher<Bool, Never> { get }

    // Enrichment config used by the re-enrich call inside `BrowseState`.
    // iOS impls return empty strings — re-enrich is a macOS-only feature
    // and the iOS lightbox doesn't surface it.
    var resolvedVisionApiUrl: String { get }
    var resolvedVisionApiKey: String { get }
    var resolvedVisionModelId: String { get }
    var whisperModelSize: String { get }
    var whisperLanguage: String { get }
    var whisperBinaryPath: String { get }

    /// The embedding model id and version that `findSimilar` should
    /// pass to `/v1/similar` so the server selects the right indexed
    /// vectors. macOS reports whichever local provider is loaded
    /// (CLIP if available, otherwise FeaturePrint). iOS doesn't enrich
    /// — it returns a canonical CLIP id/version so the server lookup
    /// hits the most-likely-indexed model. If the server doesn't have
    /// vectors for this model on the asset, similarity returns empty
    /// and the UI surfaces it as "no results".
    var embeddingModelId: String { get }
    var embeddingModelVersion: String { get }
}
