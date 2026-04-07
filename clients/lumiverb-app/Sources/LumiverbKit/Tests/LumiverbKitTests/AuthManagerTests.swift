import XCTest
import Foundation
@testable import LumiverbKit

// MARK: - In-memory token store for tests

/// Thread-safe in-memory replacement for KeychainHelper.
final class InMemoryTokenStore: TokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var storage: [String: String] = [:]

    func save(key: String, value: String) throws {
        lock.lock()
        defer { lock.unlock() }
        storage[key] = value
    }

    func read(key: String) throws -> String {
        lock.lock()
        defer { lock.unlock() }
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

    /// Test helper: peek at stored value without throwing.
    func peek(key: String) -> String? {
        lock.lock()
        defer { lock.unlock() }
        return storage[key]
    }
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
        auth = AuthManager(client: client, keychain: tokenStore)
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
            XCTAssertEqual(error, .unauthorized)
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
