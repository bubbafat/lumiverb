import Foundation

/// Cross-platform contract that `BrowseState` depends on for the bits of
/// app-level state it can't own itself: the API client, the library list
/// and the embedding model similarity search asks for.
///
/// Implementations:
/// - macOS: `AppState` conforms (defined in `Sources/macOS/AppState.swift`).
/// - iOS: an `iOSBrowseAppContext` adapter built around `iOSAppState`.
///
/// Why a protocol rather than concrete: BrowseState lives in LumiverbKit
/// and shares between macOS and iOS, but each platform has its own
/// app-state holder with platform-specific persistence (UserDefaults
/// keys, menu bar mirroring, etc.). The protocol is the seam.
@MainActor
public protocol BrowseAppContext: AnyObject {
    var client: APIClient? { get }
    var libraries: [Library] { get }

    /// The embedding model id and version that `findSimilar` passes to
    /// `/v1/similar` so the server selects the right indexed vectors.
    /// Both apps name the server's CLIP model (`clip` /
    /// `ViT-B-32-openai`), the one the scheduler embeds every photo with
    /// (`src/producers/clip`). If the server has no vectors for this
    /// model on the asset, similarity returns empty and the UI surfaces
    /// it as "no results".
    var embeddingModelId: String { get }
    var embeddingModelVersion: String { get }
}
