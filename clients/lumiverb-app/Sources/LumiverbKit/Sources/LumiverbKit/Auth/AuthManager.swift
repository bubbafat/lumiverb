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

/// Manages authentication state: login, token refresh, and persistence
/// via a swappable `TokenStore` (defaults to `FileTokenStore`).
///
/// The Lumiverb API uses a single JWT that serves as both access and refresh
/// token. The JWT has an `exp` (access expiry, ~1 hour) and a `refresh_exp`
/// (refresh window, ~7 days). To refresh, send the expired JWT in the
/// Authorization header to `POST /v1/auth/refresh`.
public actor AuthManager {
    private let client: APIClient
    private let tokenStore: any TokenStore

    /// In-memory copy of the persisted token, populated on login,
    /// restoreSession, and refresh. The legacy macOS keychain used to
    /// prompt the user (per item, per access) for any read or
    /// modification when the calling binary wasn't on the item's
    /// trusted-apps list — dev builds compiled with
    /// `CODE_SIGNING_ALLOWED=NO` are never on that list because each
    /// rebuild rotates the binary identity. We've since switched the
    /// default `tokenStore` to `FileTokenStore`, which writes to
    /// `~/Library/Application Support/io.lumiverb.app/credentials.json`
    /// (mode 0600) and never prompts. Caching here is still useful as
    /// a fast path that avoids re-reading the file on every refresh.
    private var cachedToken: String?

    /// In-flight refresh task. Multiple 401s coalesce into a single refresh
    /// call — the server revokes the old token on refresh, so concurrent
    /// refreshes would each revoke the previous result, causing a cascade.
    private var refreshTask: Task<Bool, Never>?

    public init(client: APIClient, tokenStore: any TokenStore = FileTokenStore()) {
        self.client = client
        self.tokenStore = tokenStore

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
        try tokenStore.save(key: "accessToken", value: response.accessToken)
        cachedToken = response.accessToken
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
        // Prefer the in-memory cache. Fall back to a one-shot keychain read if
        // refresh ever fires before restoreSession ran (defensive — the
        // observed app flow always restores first, so this fallback should
        // not normally execute).
        let currentToken: String
        if let cached = cachedToken {
            currentToken = cached
        } else if let read = try? tokenStore.read(key: "accessToken") {
            cachedToken = read
            currentToken = read
        } else {
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
            try tokenStore.save(key: "accessToken", value: response.accessToken)
            cachedToken = response.accessToken
            return true
        } catch {
            return false
        }
    }

    /// Restore a previous session from the token store. Returns `true` if a token
    /// was found and set on the client.
    public func restoreSession() async -> Bool {
        guard let token = try? tokenStore.read(key: "accessToken") else {
            return false
        }
        cachedToken = token
        await client.setAccessToken(token)
        return true
    }

    /// Clear all stored tokens and reset the client.
    public func logout() async {
        await client.setAccessToken(nil)
        cachedToken = nil
        try? tokenStore.delete(key: "accessToken")
    }

    /// Whether we have a stored token (may be expired).
    public func hasStoredCredentials() -> Bool {
        if cachedToken != nil { return true }
        return (try? tokenStore.read(key: "accessToken")) != nil
    }
}
