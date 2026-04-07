import Foundation

// MARK: - JWT token types

struct LoginRequest: Encodable {
    let email: String
    let password: String
}

struct LoginResponse: Decodable {
    let accessToken: String
    let refreshToken: String
}

struct RefreshRequest: Encodable {
    let refreshToken: String
}

struct RefreshResponse: Decodable {
    let accessToken: String
}

// MARK: - Auth manager

/// Manages authentication state: login, token refresh, and keychain persistence.
///
/// The auth manager owns the lifecycle of JWT tokens. On successful login it
/// stores both tokens in the keychain. On API calls, the `APIClient` uses the
/// access token. When a 401 is received, the caller should invoke `refresh()`
/// to get a new access token using the stored refresh token.
public actor AuthManager {
    private let client: APIClient
    private let keychain: KeychainHelper

    public init(client: APIClient, keychain: KeychainHelper = KeychainHelper()) {
        self.client = client
        self.keychain = keychain
    }

    /// Attempt login with email and password. On success, stores tokens and
    /// configures the API client.
    public func login(email: String, password: String) async throws {
        let body = LoginRequest(email: email, password: password)
        // Login endpoint doesn't require a token, so we set a temporary one
        await client.setAccessToken("login-placeholder")

        let response: LoginResponse
        do {
            response = try await client.post("/v1/auth/login", body: body)
        } catch {
            await client.setAccessToken(nil)
            throw error
        }

        await client.setAccessToken(response.accessToken)
        try keychain.save(key: "accessToken", value: response.accessToken)
        try keychain.save(key: "refreshToken", value: response.refreshToken)
    }

    /// Refresh the access token using the stored refresh token.
    /// Returns `true` if refresh succeeded, `false` if re-login is needed.
    public func refresh() async -> Bool {
        guard let refreshToken = try? keychain.read(key: "refreshToken") else {
            return false
        }

        let body = RefreshRequest(refreshToken: refreshToken)
        // Use the expired access token (server checks refresh token, not access)
        do {
            let response: RefreshResponse = try await client.post(
                "/v1/auth/refresh", body: body
            )
            await client.setAccessToken(response.accessToken)
            try keychain.save(key: "accessToken", value: response.accessToken)
            return true
        } catch {
            return false
        }
    }

    /// Restore a previous session from keychain. Returns `true` if a token
    /// was found and set on the client.
    public func restoreSession() async -> Bool {
        guard let token = try? keychain.read(key: "accessToken") else {
            return false
        }
        await client.setAccessToken(token)
        return true
    }

    /// Clear all stored tokens and reset the client.
    public func logout() async {
        await client.setAccessToken(nil)
        try? keychain.delete(key: "accessToken")
        try? keychain.delete(key: "refreshToken")
    }

    /// Whether we have a stored token (may be expired).
    public func hasStoredCredentials() -> Bool {
        (try? keychain.read(key: "accessToken")) != nil
    }
}
