import Foundation

// MARK: - JWT token types

struct LoginRequest: Encodable {
    let email: String
    let password: String
}

/// Server returns: `{access_token, token_type, expires_in}`.
/// The access token doubles as the refresh token (within its refresh window).
struct LoginResponse: Decodable {
    let accessToken: String
    let tokenType: String
    let expiresIn: Int
}

struct RefreshResponse: Decodable {
    let accessToken: String
    let tokenType: String
    let expiresIn: Int
}

// MARK: - Auth manager

/// Manages authentication state: login, token refresh, and keychain persistence.
///
/// The Lumiverb API uses a single JWT that serves as both access and refresh
/// token. The JWT has an `exp` (access expiry, ~1 hour) and a `refresh_exp`
/// (refresh window, ~7 days). To refresh, send the expired JWT in the
/// Authorization header to `POST /v1/auth/refresh`.
public actor AuthManager {
    private let client: APIClient
    private let keychain: any TokenStore

    /// In-flight refresh task. Multiple 401s coalesce into a single refresh
    /// call — the server revokes the old token on refresh, so concurrent
    /// refreshes would each revoke the previous result, causing a cascade.
    private var refreshTask: Task<Bool, Never>?

    public init(client: APIClient, keychain: any TokenStore = KeychainHelper()) {
        self.client = client
        self.keychain = keychain

        // Wire up auto-refresh: when APIClient gets a 401, it calls this
        let authManager = self
        Task {
            await client.setRefreshHandler {
                await authManager.refresh()
            }
        }
    }

    /// Attempt login with email and password. On success, stores the token
    /// and configures the API client.
    public func login(email: String, password: String) async throws {
        let body = LoginRequest(email: email, password: password)

        let response: LoginResponse = try await client.postUnauthenticated(
            "/v1/auth/login", body: body
        )

        await client.setAccessToken(response.accessToken)
        try keychain.save(key: "accessToken", value: response.accessToken)
    }

    /// Refresh the access token by sending the current (possibly expired) JWT.
    /// Returns `true` if refresh succeeded, `false` if re-login is needed.
    ///
    /// Serialized: if a refresh is already in flight, subsequent calls wait
    /// for it instead of starting a new one. This prevents the server from
    /// revoking a just-issued token when multiple 401s trigger concurrent
    /// refresh attempts.
    public func refresh() async -> Bool {
        // If a refresh is already in flight, wait for it
        if let existing = refreshTask {
            return await existing.value
        }

        let task = Task { [weak self] () -> Bool in
            guard let self else { return false }
            return await self.performRefresh()
        }
        refreshTask = task
        let result = await task.value
        refreshTask = nil
        return result
    }

    private func performRefresh() async -> Bool {
        guard let currentToken = try? keychain.read(key: "accessToken") else {
            return false
        }

        do {
            // Send the expired token directly to the refresh endpoint without
            // overwriting the client's accessToken — other requests may be
            // using a valid token that we'd clobber.
            let response: RefreshResponse = try await client.postWithToken(
                "/v1/auth/refresh",
                token: currentToken
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
    }

    /// Whether we have a stored token (may be expired).
    public func hasStoredCredentials() -> Bool {
        (try? keychain.read(key: "accessToken")) != nil
    }
}
