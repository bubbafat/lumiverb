import Combine
import LumiverbKit

/// Adapter that presents `iOSAppState` as a `BrowseAppContext` so the
/// shared `BrowseState` can be constructed identically on both platforms.
///
/// iOS is browse-only — enrichment config returns empty strings (the
/// re-enrich menu is gated off by a nil `reEnrichInvoker`), whisper is
/// permanently disabled, and the embedding model defaults to CLIP so
/// similarity searches hit the most-likely-indexed vectors on the server.
@MainActor
final class iOSBrowseAppContext: BrowseAppContext {
    private let appState: iOSAppState

    init(appState: iOSAppState) {
        self.appState = appState
    }

    var client: APIClient? { appState.client }
    var libraries: [Library] { appState.libraries }

    // Whisper is not available on iOS.
    var whisperEnabled: Bool { false }
    var whisperEnabledPublisher: AnyPublisher<Bool, Never> {
        Just(false).eraseToAnyPublisher()
    }

    // Enrichment config — iOS never enriches. Empty strings disable
    // the re-enrich action in the lightbox.
    var resolvedVisionApiUrl: String { "" }
    var resolvedVisionApiKey: String { "" }
    var resolvedVisionModelId: String { "" }
    var whisperModelSize: String { "" }
    var whisperLanguage: String { "" }
    var whisperBinaryPath: String { "" }

    // Default to CLIP so `/v1/similar` looks up the right indexed vectors.
    var embeddingModelId: String { "clip" }
    var embeddingModelVersion: String { "ViT-B-32-openai" }
}
