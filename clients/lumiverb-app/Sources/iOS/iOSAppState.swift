import SwiftUI
import LumiverbKit

@MainActor
class iOSAppState: ObservableObject {
    @Published var isAuthenticated = false
    @Published var isConnecting = false
    @Published var connectionError: String?
    @Published var currentUser: CurrentUser?
    @Published var libraries: [Library] = []
    @Published var serverURL: String = ""

    private(set) var client: APIClient?
    private(set) var authManager: AuthManager?

    init() {
        if let saved = UserDefaults.standard.string(forKey: "serverURL"), !saved.isEmpty {
            serverURL = saved
        }
    }

    func configure(serverURL: String) {
        guard let url = URL(string: serverURL) else { return }
        self.serverURL = serverURL
        UserDefaults.standard.set(serverURL, forKey: "serverURL")

        let newClient = APIClient(baseURL: url)
        self.client = newClient
        // iOS uses the data-protection keychain (the default on iOS),
        // which never prompts and is the right home for credentials.
        // The macOS app's default `FileTokenStore` is needed only
        // because the legacy macOS keychain prompts for ad-hoc dev
        // builds.
        self.authManager = AuthManager(client: newClient, tokenStore: KeychainHelper())
    }

    func tryRestoreSession() async {
        guard let authManager else { return }
        isConnecting = true
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

    private func fetchUserAndLibraries() async {
        guard let client else { return }
        do {
            let user: CurrentUser = try await client.get("/v1/me")
            let libs: LibraryListResponse = try await client.get("/v1/libraries")
            currentUser = user
            libraries = libs
            isAuthenticated = true
            connectionError = nil
        } catch {
            let firstError = error
            if let authManager, await authManager.refresh() {
                do {
                    let user: CurrentUser = try await client.get("/v1/me")
                    let libs: LibraryListResponse = try await client.get("/v1/libraries")
                    currentUser = user
                    libraries = libs
                    isAuthenticated = true
                } catch {
                    connectionError = "After refresh: \(error)"
                }
            } else {
                connectionError = "First attempt: \(firstError)"
            }
        }
    }
}
