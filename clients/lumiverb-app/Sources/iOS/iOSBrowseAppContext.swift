import LumiverbKit

/// Adapter that presents `iOSAppState` as a `BrowseAppContext` so the
/// shared `BrowseState` can be constructed identically on both platforms.
///
/// iOS is browse-only: the embedding model is the server's CLIP so
/// similarity searches hit the vectors the scheduler indexed.
@MainActor
final class iOSBrowseAppContext: BrowseAppContext {
    private let appState: iOSAppState

    init(appState: iOSAppState) {
        self.appState = appState
    }

    var client: APIClient? { appState.client }
    var libraries: [Library] { appState.libraries }

    // The server's CLIP model, so `/v1/similar` looks up the indexed vectors.
    var embeddingModelId: String { "clip" }
    var embeddingModelVersion: String { "ViT-B-32-openai" }
}
