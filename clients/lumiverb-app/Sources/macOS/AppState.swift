import SwiftUI
import LumiverbKit

/// Shared observable state for the macOS app.
@MainActor
class AppState: ObservableObject {
    @Published var isAuthenticated = false
    @Published var isConnecting = false
    @Published var connectionError: String?
    @Published var currentUser: CurrentUser?
    @Published var libraries: [Library] = []
    @Published var serverURL: String = ""

    /// Library IDs the user has marked as favorites. Persists to
    /// UserDefaults. Surfaced in the menu bar so users can jump straight
    /// to a frequently-used library without first opening the browse
    /// window. Empty by default — users opt in via the sidebar context
    /// menu.
    @Published var favoriteLibraryIds: Set<String> = []

    /// Library the menu bar (or another out-of-band caller) wants the
    /// browse window to open with. `BrowseWindow` consumes this on appear
    /// and on `.onChange` and clears it. Nil = no pending request.
    @Published var pendingSelectedLibraryId: String?

    // Vision API configuration (local overrides > tenant defaults)
    @Published var visionApiUrl: String = ""
    @Published var visionApiKey: String = ""
    @Published var visionModelId: String = ""
    // Tenant-provided defaults (read-only, shown in UI for reference)
    @Published var tenantVisionApiUrl: String = ""
    @Published var tenantVisionModelId: String = ""

    // Whisper.cpp transcription config. Defaults match the Python production
    // reference (`whisper_model="small"`). The `whisperEnabled` toggle is
    // the primary opt-in: when off, the entire transcription pipeline is
    // disabled — the menu item is shown but greyed out, the model directory
    // can be cleaned up, and no model auto-downloads happen. Empty
    // `whisperLanguage` means auto-detect; empty `whisperBinaryPath` means
    // auto-discover from common Homebrew install locations.
    @Published var whisperEnabled: Bool = false
    @Published var whisperModelSize: String = "small"
    @Published var whisperLanguage: String = ""
    @Published var whisperBinaryPath: String = ""

    private(set) var client: APIClient?
    private(set) var authManager: AuthManager?

    /// Pair of platform-specific caches handed to browse views via the
    /// SwiftUI environment. macOS uses the disk-backed implementations
    /// (`MacProxyDiskCache.shared` shares the proxy cache with the Python
    /// CLI; `MacThumbnailDiskCache.shared` is macOS-app-local). Defined
    /// here in `AppState` so the BrowseWindow scene can install it as an
    /// `.environment(\.cacheBundle, ...)` value at the root of the view
    /// tree without needing to know which concrete types to construct.
    let cacheBundle: CacheBundle = CacheBundle(
        proxies: MacProxyDiskCache.shared,
        thumbnails: MacThumbnailDiskCache.shared
    )

    /// Singleton scroll accessor for the macOS BrowseWindow's grid views.
    /// Wraps an `NSScrollViewBox` whose `scrollView` weak reference is
    /// populated by `NSScrollViewIntrospector` from inside whichever grid
    /// is currently on-screen. ADR-015 M2 will switch the grid views to
    /// read this from `@Environment(\.scrollAccessor)`; until then, the
    /// existing per-grid `NSScrollViewBox` plumbing is what actually
    /// drives scroll commands.
    let scrollAccessor: MacScrollAccessor = MacScrollAccessor()

    /// Resolved vision config: local override > tenant default.
    var resolvedVisionApiUrl: String {
        visionApiUrl.isEmpty ? tenantVisionApiUrl : visionApiUrl
    }
    var resolvedVisionApiKey: String { visionApiKey }
    var resolvedVisionModelId: String {
        visionModelId.isEmpty ? tenantVisionModelId : visionModelId
    }
    var isVisionConfigured: Bool {
        !resolvedVisionApiUrl.isEmpty && !resolvedVisionModelId.isEmpty
    }

    var isWhisperConfigured: Bool {
        WhisperProvider.isConfigured(modelSize: whisperModelSize, binaryPath: whisperBinaryPath)
    }

    /// True if whisper is enabled but the chosen model file is missing on
    /// disk — used by Settings to surface a "Download now" banner without
    /// auto-triggering a download as a side-effect of opening the page.
    var isWhisperEnabledButModelMissing: Bool {
        whisperEnabled && !isWhisperConfigured
    }

    init() {
        // Try to restore saved server URL
        if let saved = UserDefaults.standard.string(forKey: "serverURL"), !saved.isEmpty {
            serverURL = saved
        }
        // Restore saved vision config
        visionApiUrl = UserDefaults.standard.string(forKey: "visionApiUrl") ?? ""
        visionApiKey = UserDefaults.standard.string(forKey: "visionApiKey") ?? ""
        visionModelId = UserDefaults.standard.string(forKey: "visionModelId") ?? ""
        // Restore whisper config
        whisperEnabled = UserDefaults.standard.bool(forKey: "whisperEnabled")
        whisperModelSize = UserDefaults.standard.string(forKey: "whisperModelSize") ?? "small"
        whisperLanguage = UserDefaults.standard.string(forKey: "whisperLanguage") ?? ""
        whisperBinaryPath = UserDefaults.standard.string(forKey: "whisperBinaryPath") ?? ""
        // Restore favorites
        favoriteLibraryIds = Set(
            UserDefaults.standard.stringArray(forKey: "favoriteLibraryIds") ?? []
        )
    }

    // MARK: - Favorites

    func isFavoriteLibrary(_ libraryId: String) -> Bool {
        favoriteLibraryIds.contains(libraryId)
    }

    /// Toggle a library's favorite state and persist immediately.
    func toggleFavoriteLibrary(_ libraryId: String) {
        if favoriteLibraryIds.contains(libraryId) {
            favoriteLibraryIds.remove(libraryId)
        } else {
            favoriteLibraryIds.insert(libraryId)
        }
        UserDefaults.standard.set(
            Array(favoriteLibraryIds), forKey: "favoriteLibraryIds"
        )
    }

    /// Libraries that are currently marked as favorites, in the same order
    /// they appear in `libraries`. Used by the menu bar.
    var favoriteLibraries: [Library] {
        libraries.filter { favoriteLibraryIds.contains($0.libraryId) }
    }

    func saveVisionConfig() {
        UserDefaults.standard.set(visionApiUrl, forKey: "visionApiUrl")
        UserDefaults.standard.set(visionApiKey, forKey: "visionApiKey")
        UserDefaults.standard.set(visionModelId, forKey: "visionModelId")
    }

    func saveWhisperConfig() {
        UserDefaults.standard.set(whisperEnabled, forKey: "whisperEnabled")
        UserDefaults.standard.set(whisperModelSize, forKey: "whisperModelSize")
        UserDefaults.standard.set(whisperLanguage, forKey: "whisperLanguage")
        UserDefaults.standard.set(whisperBinaryPath, forKey: "whisperBinaryPath")
    }

    func configure(serverURL: String) {
        guard let url = URL(string: serverURL) else { return }
        self.serverURL = serverURL
        UserDefaults.standard.set(serverURL, forKey: "serverURL")

        let newClient = APIClient(baseURL: url)
        self.client = newClient
        self.authManager = AuthManager(client: newClient)
    }

    func tryRestoreSession() async {
        guard let authManager else { return }

        isConnecting = true
        connectionError = nil

        let restored = await authManager.restoreSession()
        if restored {
            await fetchUserAndLibraries()
        }
        isConnecting = false
    }

    func login(email: String, password: String) async {
        guard let authManager else { return }

        isConnecting = true
        connectionError = nil

        do {
            try await authManager.login(email: email, password: password)
            await fetchUserAndLibraries()
        } catch let error as APIError {
            switch error {
            case .unauthorized:
                connectionError = "Invalid email or password"
            case .serverError(_, let message):
                connectionError = message
            case .networkError(let message):
                connectionError = "Connection failed: \(message)"
            default:
                connectionError = "Login failed"
            }
        } catch {
            connectionError = error.localizedDescription
        }
        isConnecting = false
    }

    func logout() async {
        await authManager?.logout()
        isAuthenticated = false
        currentUser = nil
        libraries = []
    }

    /// Refresh the library list from the server.
    func refreshLibraries() async {
        guard let client else { return }
        do {
            let libs: LibraryListResponse = try await client.get("/v1/libraries")
            libraries = libs
        } catch {
            // Non-fatal — keep stale list
        }
    }

    /// Create a new library, refresh the local list, and return the created
    /// library. Throws `APIError` so callers can surface server errors (409
    /// on duplicate name, 401, etc.) inline in the UI.
    ///
    /// The `rootPath` is stored on the server as a plain POSIX path and is
    /// round-tripped to any client reading `/v1/libraries`. This is the same
    /// model the CLI uses — we're not persisting a security-scoped bookmark
    /// because the macOS app is not sandboxed (`Lumiverb.entitlements` only
    /// declares `com.apple.security.network.client`).
    @discardableResult
    func createLibrary(name: String, rootPath: String) async throws -> Library {
        guard let client else {
            throw APIError.noToken
        }
        let created: Library = try await client.post(
            "/v1/libraries",
            body: CreateLibraryRequest(name: name, rootPath: rootPath)
        )
        await refreshLibraries()
        return created
    }

    // MARK: - Library settings (rename / re-root / path filters)

    /// Update a library's name and/or root path via `PATCH /v1/libraries/{id}`.
    /// Only non-nil fields are sent. Refreshes the local list on success so
    /// the sidebar and scanner immediately pick up the new values.
    ///
    /// Note: changing `rootPath` on the server does not move any files or
    /// re-scan. The scanner will see the new path on its next run; if the
    /// path is wrong the pipeline surfaces "Library root not found" on next
    /// scan. The UI should be clear that this is re-pointing, not moving.
    @discardableResult
    func updateLibrary(
        libraryId: String,
        name: String? = nil,
        rootPath: String? = nil
    ) async throws -> Library {
        guard let client else { throw APIError.noToken }
        let updated: Library = try await client.patch(
            "/v1/libraries/\(libraryId)",
            body: LibraryUpdateRequest(name: name, rootPath: rootPath)
        )
        await refreshLibraries()
        return updated
    }

    /// Fetch the current include + exclude path filters for a library.
    func listLibraryFilters(libraryId: String) async throws -> LibraryFiltersResponse {
        guard let client else { throw APIError.noToken }
        return try await client.get("/v1/libraries/\(libraryId)/filters")
    }

    /// Add a path filter. For exclude filters, pass `trashMatching: true`
    /// after previewing the count — the server will soft-trash any already
    /// indexed assets that match and return the count in `trashedCount`.
    @discardableResult
    func addLibraryFilter(
        libraryId: String,
        type: String,
        pattern: String,
        trashMatching: Bool = false
    ) async throws -> LibraryFilterItemWithType {
        guard let client else { throw APIError.noToken }
        return try await client.post(
            "/v1/libraries/\(libraryId)/filters",
            body: CreateLibraryFilterRequest(
                type: type,
                pattern: pattern,
                trashMatching: trashMatching
            )
        )
    }

    /// Count the number of already-indexed assets that would be trashed if
    /// this exclude pattern were applied. Server validates the pattern and
    /// returns 400 on invalid glob syntax — surface that to the user.
    func previewLibraryFilter(
        libraryId: String,
        type: String,
        pattern: String
    ) async throws -> PreviewFilterResponse {
        guard let client else { throw APIError.noToken }
        return try await client.post(
            "/v1/libraries/\(libraryId)/filters/preview",
            body: PreviewFilterRequest(type: type, pattern: pattern)
        )
    }

    /// Remove a library-scoped path filter. Does NOT un-trash any assets that
    /// were trashed by a prior `trashMatching` add — that's a deliberate
    /// server-side choice; deleted excludes just stop applying to future scans.
    func deleteLibraryFilter(libraryId: String, filterId: String) async throws {
        guard let client else { throw APIError.noToken }
        try await client.delete("/v1/libraries/\(libraryId)/filters/\(filterId)")
    }

    /// Fetch tenant context to get vision API defaults.
    func fetchTenantContext() async {
        guard let client else { return }
        do {
            let ctx: TenantContext = try await client.get("/v1/tenant/context")
            tenantVisionApiUrl = ctx.visionApiUrl
            tenantVisionModelId = ctx.visionModelId
            // Don't overwrite local key with tenant key — tenant key
            // is returned from context but users may want their own.
        } catch {
            // Non-fatal — local config still works
        }
    }

    private func fetchUserAndLibraries() async {
        guard let client else { return }

        do {
            let user: CurrentUser = try await client.get("/v1/me")
            let libs: LibraryListResponse = try await client.get("/v1/libraries")
            currentUser = user
            libraries = libs
            isAuthenticated = true
            connectionError = nil
            await fetchTenantContext()
        } catch {
            let firstError = error
            // Token may be expired — try refresh
            if let authManager, await authManager.refresh() {
                do {
                    let user: CurrentUser = try await client.get("/v1/me")
                    let libs: LibraryListResponse = try await client.get("/v1/libraries")
                    currentUser = user
                    libraries = libs
                    isAuthenticated = true
                    connectionError = nil
                    await fetchTenantContext()
                } catch {
                    connectionError = "After refresh: \(error)"
                }
            } else {
                connectionError = "First attempt: \(firstError)"
            }
        }
    }
}
