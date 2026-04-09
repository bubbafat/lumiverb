import XCTest
import Foundation
@testable import LumiverbKit

// MARK: - In-memory token store for tests

/// Thread-safe in-memory replacement for KeychainHelper.
final class InMemoryTokenStore: TokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [String: String] = [:]
    private var _readCount = 0

    /// Number of times `read(key:)` has been called. Used by tests that need
    /// to assert AuthManager is hitting the in-memory cache instead of the
    /// keychain on every operation.
    var readCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return _readCount
    }

    func save(key: String, value: String) throws {
        lock.lock()
        defer { lock.unlock() }
        storage[key] = value
    }

    func read(key: String) throws -> String {
        lock.lock()
        defer { lock.unlock() }
        _readCount += 1
        guard let value = storage[key] else {
            throw KeychainError.itemNotFound
        }
        return value
    }

    func delete(key: String) throws {
        lock.lock()
        defer { lock.unlock() }
        storage.removeValue(forKey: key)
    }

    /// Test helper: peek at stored value without throwing (and without
    /// incrementing readCount).
    func peek(key: String) -> String? {
        lock.lock()
        defer { lock.unlock() }
        return storage[key]
    }
}

// MARK: - Request recorder

/// Thread-safe recorder for verifying request sequence and auth headers.
final class RequestRecorder: @unchecked Sendable {
    struct Entry: Sendable {
        let path: String
        let auth: String?
    }

    private let lock = NSLock()
    private var _requests: [Entry] = []

    var requests: [Entry] {
        lock.lock(); defer { lock.unlock() }
        return _requests
    }

    func record(path: String, auth: String?) {
        lock.lock(); defer { lock.unlock() }
        _requests.append(Entry(path: path, auth: auth))
    }
}

/// Thread-safe counter.
final class SendableCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var _value = 0
    var value: Int { lock.lock(); defer { lock.unlock() }; return _value }
    func increment() { lock.lock(); defer { lock.unlock() }; _value += 1 }
}

// MARK: - Helpers

private func jsonResponse(_ statusCode: Int, json: String, for request: URLRequest) -> (HTTPURLResponse, Data) {
    let response = HTTPURLResponse(
        url: request.url!, statusCode: statusCode,
        httpVersion: nil, headerFields: ["Content-Type": "application/json"]
    )!
    return (response, json.data(using: .utf8)!)
}

// MARK: - Tests

final class AuthManagerTests: XCTestCase {

    private var tokenStore: InMemoryTokenStore!
    private var client: APIClient!
    private var auth: AuthManager!

    private func makeSession() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        return URLSession(configuration: config)
    }

    override func setUp() {
        super.setUp()
        tokenStore = InMemoryTokenStore()
        client = APIClient(
            baseURL: URL(string: "https://test.lumiverb.io")!,
            session: makeSession()
        )
        auth = AuthManager(client: client, tokenStore: tokenStore)
    }

    override func tearDown() {
        MockURLProtocol.requestHandler = nil
        super.tearDown()
    }

    // MARK: - Login

    func testLoginStoresTokenAndSetsOnClient() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.path, "/v1/auth/login")
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"),
                         "Login should not send auth header")

            if let bodyData = request.httpBody {
                let body = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
                XCTAssertEqual(body["email"] as? String, "user@test.com")
                XCTAssertEqual(body["password"] as? String, "secret123")
            }

            let response = HTTPURLResponse(
                url: request.url!, statusCode: 200,
                httpVersion: nil, headerFields: ["Content-Type": "application/json"]
            )!
            let json = """
            {"access_token": "jwt_abc", "token_type": "bearer", "expires_in": 3600}
            """.data(using: .utf8)!
            return (response, json)
        }

        try await auth.login(email: "user@test.com", password: "secret123")

        // Token stored in keychain
        XCTAssertEqual(tokenStore.peek(key: "accessToken"), "jwt_abc")

        // Token set on client
        let clientToken = await client.currentToken()
        XCTAssertEqual(clientToken, "jwt_abc")
    }

    func testLoginThrowsOnServerError() async {
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 401,
                httpVersion: nil, headerFields: ["Content-Type": "application/json"]
            )!
            let json = """
            {"error": {"code": "invalid_credentials", "message": "Bad password"}}
            """.data(using: .utf8)!
            return (response, json)
        }

        do {
            try await auth.login(email: "user@test.com", password: "wrong")
            XCTFail("Expected error")
        } catch let error as APIError {
            // postUnauthenticated with 401 — should get unauthorized since it's unauthenticated
            // Actually, looking at the code: unauthenticated requests with 401 still hit the
            // 401 branch but with authenticated=false, so it goes to the else branch and throws .unauthorized
            guard case .unauthorized = error else {
                    XCTFail("Expected .unauthorized, got \(error)"); return
                }
        } catch {
            XCTFail("Unexpected error type: \(error)")
        }

        // Token should NOT be stored
        XCTAssertNil(tokenStore.peek(key: "accessToken"))
    }

    // MARK: - Refresh

    func testRefreshSuccessUpdatesToken() async throws {
        // Pre-store an expired token
        try tokenStore.save(key: "accessToken", value: "expired_jwt")

        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.path, "/v1/auth/refresh")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer expired_jwt")

            let response = HTTPURLResponse(
                url: request.url!, statusCode: 200,
                httpVersion: nil, headerFields: ["Content-Type": "application/json"]
            )!
            let json = """
            {"access_token": "fresh_jwt", "token_type": "bearer", "expires_in": 3600}
            """.data(using: .utf8)!
            return (response, json)
        }

        let success = await auth.refresh()
        XCTAssertTrue(success)
        XCTAssertEqual(tokenStore.peek(key: "accessToken"), "fresh_jwt")

        let clientToken = await client.currentToken()
        XCTAssertEqual(clientToken, "fresh_jwt")
    }

    func testRefreshReturnsFalseOnServerError() async throws {
        try tokenStore.save(key: "accessToken", value: "expired_jwt")

        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 401,
                httpVersion: nil, headerFields: ["Content-Type": "application/json"]
            )!
            let json = """
            {"error": {"code": "token_expired", "message": "Refresh window closed"}}
            """.data(using: .utf8)!
            return (response, json)
        }

        let success = await auth.refresh()
        XCTAssertFalse(success)

        // Original token should still be there (not cleared by failed refresh)
        XCTAssertEqual(tokenStore.peek(key: "accessToken"), "expired_jwt")
    }

    func testRefreshReturnsFalseWithNoStoredToken() async {
        let success = await auth.refresh()
        XCTAssertFalse(success)
    }

    // MARK: - Session restore

    func testRestoreSessionSetsTokenFromStore() async throws {
        try tokenStore.save(key: "accessToken", value: "stored_jwt")

        let restored = await auth.restoreSession()
        XCTAssertTrue(restored)

        let clientToken = await client.currentToken()
        XCTAssertEqual(clientToken, "stored_jwt")
    }

    func testRestoreSessionReturnsFalseWhenEmpty() async {
        let restored = await auth.restoreSession()
        XCTAssertFalse(restored)

        let clientToken = await client.currentToken()
        XCTAssertNil(clientToken)
    }

    // MARK: - Logout

    func testLogoutClearsTokenAndStore() async throws {
        // Set up an authenticated state
        try tokenStore.save(key: "accessToken", value: "active_jwt")
        await client.setAccessToken("active_jwt")

        await auth.logout()

        XCTAssertNil(tokenStore.peek(key: "accessToken"))
        let clientToken = await client.currentToken()
        XCTAssertNil(clientToken)
    }

    // MARK: - hasStoredCredentials

    func testHasStoredCredentialsReflectsState() async throws {
        let before = await auth.hasStoredCredentials()
        XCTAssertFalse(before)

        try tokenStore.save(key: "accessToken", value: "jwt")

        let after = await auth.hasStoredCredentials()
        XCTAssertTrue(after)
    }

    // MARK: - End-to-end refresh flow

    /// Verifies the exact token used on the retry after a 401 → refresh cycle.
    /// This is the test that catches stale-token-on-retry bugs.
    func testRetryAfterRefreshUsesNewToken() async throws {
        try tokenStore.save(key: "accessToken", value: "expired_jwt")
        await client.setAccessToken("expired_jwt")

        // Wait for refresh handler to be wired up
        try await Task.sleep(nanoseconds: 50_000_000)

        // Track every request in order
        let recorder = RequestRecorder()

        MockURLProtocol.requestHandler = { request in
            let path = request.url?.path ?? ""
            let authHeader = request.value(forHTTPHeaderField: "Authorization")
            recorder.record(path: path, auth: authHeader)

            if path == "/v1/libraries" && authHeader == "Bearer expired_jwt" {
                // First attempt with expired token → 401
                return jsonResponse(401, json: """
                {"error": {"code": "unauthorized", "message": "Expired"}}
                """, for: request)
            } else if path == "/v1/auth/refresh" {
                // Refresh endpoint — returns new token
                return jsonResponse(200, json: """
                {"access_token": "fresh_jwt", "token_type": "bearer", "expires_in": 3600}
                """, for: request)
            } else if path == "/v1/libraries" && authHeader == "Bearer fresh_jwt" {
                // Retry with new token → success
                return jsonResponse(200, json: """
                [{"library_id": "lib_1", "name": "Photos", "root_path": "/p", "created_at": "2024-01-01T00:00:00+00:00"}]
                """, for: request)
            } else {
                XCTFail("Unexpected request: \(path) auth=\(authHeader ?? "nil")")
                return jsonResponse(500, json: "{}", for: request)
            }
        }

        let libs: [Library] = try await client.get("/v1/libraries")
        XCTAssertEqual(libs.count, 1)

        // Verify exact request sequence
        let requests = recorder.requests
        XCTAssertEqual(requests.count, 3, "Expected 3 requests: original, refresh, retry")
        XCTAssertEqual(requests[0].path, "/v1/libraries")
        XCTAssertEqual(requests[0].auth, "Bearer expired_jwt", "First attempt should use expired token")
        XCTAssertEqual(requests[1].path, "/v1/auth/refresh")
        XCTAssertEqual(requests[1].auth, "Bearer expired_jwt", "Refresh should send expired token")
        XCTAssertEqual(requests[2].path, "/v1/libraries")
        XCTAssertEqual(requests[2].auth, "Bearer fresh_jwt", "Retry MUST use the fresh token")

        // Token state should be updated everywhere
        XCTAssertEqual(tokenStore.peek(key: "accessToken"), "fresh_jwt")
        let clientToken = await client.currentToken()
        XCTAssertEqual(clientToken, "fresh_jwt")
    }

    /// Verifies that getData (binary fetch) also retries with the fresh token.
    func testGetDataRetryAfterRefreshUsesNewToken() async throws {
        try tokenStore.save(key: "accessToken", value: "expired_jwt")
        await client.setAccessToken("expired_jwt")
        try await Task.sleep(nanoseconds: 50_000_000)

        let recorder = RequestRecorder()
        let imageData = Data([0xFF, 0xD8, 0xFF, 0xE0])

        MockURLProtocol.requestHandler = { request in
            let path = request.url?.path ?? ""
            let authHeader = request.value(forHTTPHeaderField: "Authorization")
            recorder.record(path: path, auth: authHeader)

            if path.hasSuffix("/proxy") && authHeader == "Bearer expired_jwt" {
                return jsonResponse(401, json: "Unauthorized", for: request)
            } else if path == "/v1/auth/refresh" {
                return jsonResponse(200, json: """
                {"access_token": "fresh_jwt", "token_type": "bearer", "expires_in": 3600}
                """, for: request)
            } else if path.hasSuffix("/proxy") && authHeader == "Bearer fresh_jwt" {
                let response = HTTPURLResponse(
                    url: request.url!, statusCode: 200,
                    httpVersion: nil, headerFields: ["Content-Type": "image/jpeg"]
                )!
                return (response, imageData)
            } else {
                XCTFail("Unexpected request: \(path) auth=\(authHeader ?? "nil")")
                return jsonResponse(500, json: "{}", for: request)
            }
        }

        let data = try await client.getData("/v1/assets/ast_1/proxy")
        XCTAssertEqual(data, imageData)

        let requests = recorder.requests
        XCTAssertEqual(requests.count, 3)
        XCTAssertEqual(requests[2].auth, "Bearer fresh_jwt", "getData retry MUST use fresh token")
    }

    /// Verifies that concurrent 401s coalesce into a single refresh call.
    func testConcurrent401sCoalesceIntoSingleRefresh() async throws {
        try tokenStore.save(key: "accessToken", value: "expired_jwt")
        await client.setAccessToken("expired_jwt")
        try await Task.sleep(nanoseconds: 50_000_000)

        let refreshCount = SendableCounter()

        MockURLProtocol.requestHandler = { request in
            let path = request.url?.path ?? ""
            let authHeader = request.value(forHTTPHeaderField: "Authorization")

            if path == "/v1/auth/refresh" {
                refreshCount.increment()
                // Small delay to allow concurrent refresh attempts to coalesce
                Thread.sleep(forTimeInterval: 0.05)
                return jsonResponse(200, json: """
                {"access_token": "fresh_jwt", "token_type": "bearer", "expires_in": 3600}
                """, for: request)
            } else if authHeader == "Bearer expired_jwt" {
                return jsonResponse(401, json: """
                {"error": {"code": "unauthorized", "message": "Expired"}}
                """, for: request)
            } else {
                return jsonResponse(200, json: """
                [{"library_id": "lib_1", "name": "Photos", "root_path": "/p", "created_at": "2024-01-01T00:00:00+00:00"}]
                """, for: request)
            }
        }

        // Fire two requests concurrently — both should get 401
        let client = self.client!
        try await withThrowingTaskGroup(of: [Library].self) { group in
            group.addTask { try await client.get("/v1/libraries") }
            group.addTask { try await client.get("/v1/libraries") }

            for try await libs in group {
                XCTAssertEqual(libs.count, 1)
            }
        }

        // Should have at most 1 refresh call (coalesced), not 2
        XCTAssertEqual(refreshCount.value, 1,
            "Expected 1 refresh call (coalesced), got \(refreshCount.value)")
    }

    // MARK: - Token caching (avoids redundant macOS keychain prompts)

    /// On unsigned dev macOS builds, every keychain access prompts the user
    /// for permission. After restoreSession() populates the in-memory cache,
    /// a subsequent refresh() must reuse the cached token instead of doing
    /// another keychain.read.
    func testRefreshAfterRestoreSessionDoesNotReReadKeychain() async throws {
        try tokenStore.save(key: "accessToken", value: "expired_jwt")

        let restored = await auth.restoreSession()
        XCTAssertTrue(restored)
        XCTAssertEqual(tokenStore.readCount, 1, "restoreSession reads keychain once")

        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.path, "/v1/auth/refresh")
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Authorization"),
                "Bearer expired_jwt",
                "refresh must use the token cached from restoreSession"
            )
            return jsonResponse(200, json: """
            {"access_token": "fresh_jwt", "token_type": "bearer", "expires_in": 3600}
            """, for: request)
        }

        let success = await auth.refresh()
        XCTAssertTrue(success)
        XCTAssertEqual(
            tokenStore.readCount, 1,
            "refresh after restoreSession must use the cache, not re-read keychain"
        )
        XCTAssertEqual(tokenStore.peek(key: "accessToken"), "fresh_jwt")
    }

    /// hasStoredCredentials() must hit the cache after restoreSession.
    func testHasStoredCredentialsAfterRestoreDoesNotReadKeychain() async throws {
        try tokenStore.save(key: "accessToken", value: "stored_jwt")

        _ = await auth.restoreSession()
        XCTAssertEqual(tokenStore.readCount, 1)

        let has = await auth.hasStoredCredentials()
        XCTAssertTrue(has)
        XCTAssertEqual(
            tokenStore.readCount, 1,
            "hasStoredCredentials must hit the in-memory cache when populated"
        )
    }

    /// If refresh() is somehow called before restoreSession ran, it should
    /// still work — fall back to a one-shot keychain read and populate the
    /// cache so subsequent operations don't read again.
    func testRefreshFallsBackToKeychainWhenCacheEmpty() async throws {
        try tokenStore.save(key: "accessToken", value: "expired_jwt")

        MockURLProtocol.requestHandler = { request in
            return jsonResponse(200, json: """
            {"access_token": "fresh_jwt", "token_type": "bearer", "expires_in": 3600}
            """, for: request)
        }

        // First refresh: cache is empty, must read once.
        let success1 = await auth.refresh()
        XCTAssertTrue(success1)
        XCTAssertEqual(
            tokenStore.readCount, 1,
            "first refresh with empty cache reads keychain once"
        )

        // Second refresh: cache populated by first call.
        let success2 = await auth.refresh()
        XCTAssertTrue(success2)
        XCTAssertEqual(
            tokenStore.readCount, 1,
            "second refresh must use the populated cache (no extra read)"
        )
    }

    /// logout() must clear the in-memory cache, otherwise hasStoredCredentials
    /// would still report `true` after logout.
    func testLogoutClearsCacheNotJustKeychain() async throws {
        try tokenStore.save(key: "accessToken", value: "stored_jwt")
        _ = await auth.restoreSession()

        await auth.logout()

        let has = await auth.hasStoredCredentials()
        XCTAssertFalse(has, "hasStoredCredentials must return false after logout")
    }

    // MARK: - Auto-refresh wiring

    func testAutoRefreshWiresUpOnInit() async throws {
        // Pre-store a token for refresh to find
        try tokenStore.save(key: "accessToken", value: "old_jwt")

        var callCount = 0
        MockURLProtocol.requestHandler = { request in
            callCount += 1
            if request.url?.path == "/v1/libraries" && callCount == 1 {
                // First libraries call: 401
                let response = HTTPURLResponse(
                    url: request.url!, statusCode: 401,
                    httpVersion: nil, headerFields: nil
                )!
                return (response, Data())
            } else if request.url?.path == "/v1/auth/refresh" {
                // Refresh call
                let response = HTTPURLResponse(
                    url: request.url!, statusCode: 200,
                    httpVersion: nil, headerFields: ["Content-Type": "application/json"]
                )!
                let json = """
                {"access_token": "new_jwt", "token_type": "bearer", "expires_in": 3600}
                """.data(using: .utf8)!
                return (response, json)
            } else {
                // Retried libraries call with new token
                XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer new_jwt")
                let response = HTTPURLResponse(
                    url: request.url!, statusCode: 200,
                    httpVersion: nil, headerFields: ["Content-Type": "application/json"]
                )!
                let json = """
                [{"library_id": "lib_1", "name": "Photos", "root_path": "/p", "created_at": "2024-01-01T00:00:00+00:00"}]
                """.data(using: .utf8)!
                return (response, json)
            }
        }

        // Set initial token on client so the GET proceeds
        await client.setAccessToken("old_jwt")

        // The refresh handler was wired in init via a Task — give it a moment to execute
        try await Task.sleep(nanoseconds: 50_000_000) // 50ms

        let libs: [Library] = try await client.get("/v1/libraries")
        XCTAssertEqual(libs.count, 1)
        XCTAssertEqual(tokenStore.peek(key: "accessToken"), "new_jwt")
    }
}
