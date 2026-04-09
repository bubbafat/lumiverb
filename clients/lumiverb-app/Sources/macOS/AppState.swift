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
