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

    private(set) var client: APIClient?
    private(set) var authManager: AuthManager?

    init() {
        // Try to restore saved server URL
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
            // Token may be expired — try refresh
            if let authManager, await authManager.refresh() {
                do {
                    let user: CurrentUser = try await client.get("/v1/me")
                    let libs: LibraryListResponse = try await client.get("/v1/libraries")
                    currentUser = user
                    libraries = libs
                    isAuthenticated = true
                    connectionError = nil
                } catch {
                    connectionError = "After refresh: \(error)"
                }
            } else {
                connectionError = "First attempt: \(firstError)"
            }
        }
    }
}
