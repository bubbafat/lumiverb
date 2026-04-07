import XCTest
import Foundation
@testable import LumiverbKit

// MARK: - Mock URL Protocol

/// Intercepts URLSession requests so tests never hit the network.
final class MockURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var requestHandler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.requestHandler else {
            XCTFail("MockURLProtocol.requestHandler not set")
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

// MARK: - Sendable flag for concurrency-safe test assertions

final class SendableFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var _value = false
    var value: Bool { lock.lock(); defer { lock.unlock() }; return _value }
    func set() { lock.lock(); defer { lock.unlock() }; _value = true }
}

// MARK: - Helpers

private let testBaseURL = URL(string: "https://test.lumiverb.io")!

private func makeSession() -> URLSession {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [MockURLProtocol.self]
    return URLSession(configuration: config)
}

private func makeClient(token: String? = "test-token") -> APIClient {
    APIClient(baseURL: testBaseURL, session: makeSession(), accessToken: token)
}

private func jsonResponse(_ statusCode: Int, json: String, for request: URLRequest) -> (HTTPURLResponse, Data) {
    let response = HTTPURLResponse(
        url: request.url!, statusCode: statusCode,
        httpVersion: nil, headerFields: ["Content-Type": "application/json"]
    )!
    return (response, json.data(using: .utf8)!)
}

// MARK: - Tests

final class APIClientNetworkTests: XCTestCase {

    override func tearDown() {
        MockURLProtocol.requestHandler = nil
        super.tearDown()
    }

    // MARK: - GET

    func testGetDecodesResponse() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/v1/libraries")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
            return jsonResponse(200, json: """
            [{"library_id": "lib_1", "name": "Photos", "root_path": "/photos", "created_at": "2024-01-01T00:00:00+00:00"}]
            """, for: request)
        }

        let client = makeClient()
        let libs: [Library] = try await client.get("/v1/libraries")
        XCTAssertEqual(libs.count, 1)
        XCTAssertEqual(libs[0].name, "Photos")
    }

    func testGetWithQueryParams() async throws {
        MockURLProtocol.requestHandler = { request in
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
            let items = Dictionary(uniqueKeysWithValues: components.queryItems!.map { ($0.name, $0.value!) })
            XCTAssertEqual(items["library_id"], "lib_1")
            XCTAssertEqual(items["cursor"], "abc")
            return jsonResponse(200, json: """
            [{"library_id": "lib_1", "name": "Test", "root_path": "/test", "created_at": "2024-01-01T00:00:00+00:00"}]
            """, for: request)
        }

        let client = makeClient()
        let _: [Library] = try await client.get("/v1/libraries", query: ["library_id": "lib_1", "cursor": "abc"])
    }

    // MARK: - POST

    func testPostEncodesBodyAsSnakeCase() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")

            if let bodyData = request.httpBody {
                let body = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
                XCTAssertNotNil(body["email"])
                XCTAssertNotNil(body["password"])
            }

            return jsonResponse(200, json: """
            {"access_token": "jwt_new", "token_type": "bearer", "expires_in": 3600}
            """, for: request)
        }

        let client = makeClient()
        let response: LoginResponse = try await client.post(
            "/v1/auth/login",
            body: LoginRequest(email: "a@b.com", password: "pass")
        )
        XCTAssertEqual(response.accessToken, "jwt_new")
    }

    // MARK: - PUT

    func testPutSendsCorrectMethod() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "PUT")
            return jsonResponse(200, json: """
            {"library_id": "lib_1", "name": "Renamed", "root_path": "/photos", "created_at": "2024-01-01T00:00:00+00:00"}
            """, for: request)
        }

        let client = makeClient()
        let lib: Library = try await client.put("/v1/libraries/lib_1")
        XCTAssertEqual(lib.name, "Renamed")
    }

    // MARK: - DELETE

    func testDeleteSendsCorrectMethod() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "DELETE")
            return jsonResponse(204, json: "", for: request)
        }

        let client = makeClient()
        try await client.delete("/v1/libraries/lib_1")
    }

    // MARK: - No token

    func testThrowsNoTokenForAuthenticatedRequest() async {
        let client = makeClient(token: nil)
        do {
            let _: [Library] = try await client.get("/v1/libraries")
            XCTFail("Expected APIError.noToken")
        } catch let error as APIError {
            XCTAssertEqual(error, .noToken)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - Unauthenticated

    func testPostUnauthenticatedOmitsAuthHeader() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertNil(request.value(forHTTPHeaderField: "Authorization"))
            return jsonResponse(200, json: """
            {"access_token": "jwt", "token_type": "bearer", "expires_in": 3600}
            """, for: request)
        }

        let client = makeClient(token: nil)
        let response: LoginResponse = try await client.postUnauthenticated(
            "/v1/auth/login",
            body: LoginRequest(email: "a@b.com", password: "pass")
        )
        XCTAssertEqual(response.accessToken, "jwt")
    }

    // MARK: - Server errors

    func testServerErrorWithEnvelope() async {
        MockURLProtocol.requestHandler = { request in
            return jsonResponse(404, json: """
            {"error": {"code": "not_found", "message": "Library not found"}}
            """, for: request)
        }

        let client = makeClient()
        do {
            let _: [Library] = try await client.get("/v1/libraries/missing")
            XCTFail("Expected server error")
        } catch let error as APIError {
            if case .serverError(let statusCode, let message) = error {
                XCTAssertEqual(statusCode, 404)
                XCTAssertEqual(message, "Library not found")
            } else {
                XCTFail("Expected .serverError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testServerErrorWithoutEnvelope() async {
        MockURLProtocol.requestHandler = { request in
            return jsonResponse(500, json: "Internal Server Error", for: request)
        }

        let client = makeClient()
        do {
            let _: [Library] = try await client.get("/v1/libraries")
            XCTFail("Expected server error")
        } catch let error as APIError {
            if case .serverError(let statusCode, let message) = error {
                XCTAssertEqual(statusCode, 500)
                XCTAssertEqual(message, "Internal Server Error")
            } else {
                XCTFail("Expected .serverError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - Decoding errors

    func testDecodingErrorIncludesContext() async {
        MockURLProtocol.requestHandler = { request in
            return jsonResponse(200, json: """
            {"wrong_field": "value"}
            """, for: request)
        }

        let client = makeClient()
        do {
            let _: Library = try await client.get("/v1/libraries/lib_1")
            XCTFail("Expected decoding error")
        } catch let error as APIError {
            if case .decodingError(let message) = error {
                XCTAssert(message.contains("Missing key"), "Expected missing key info, got: \(message)")
            } else {
                XCTFail("Expected .decodingError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - 204 Empty response

    func testHandles204EmptyResponse() async throws {
        MockURLProtocol.requestHandler = { request in
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 204,
                httpVersion: nil, headerFields: nil
            )!
            return (response, Data())
        }

        let client = makeClient()
        let result: EmptyResponse = try await client.get("/v1/some-endpoint")
        _ = result // Just verifying it doesn't throw
    }

    // MARK: - 401 → Refresh → Retry

    func testRefreshesTokenOn401ThenRetries() async throws {
        var callCount = 0

        MockURLProtocol.requestHandler = { request in
            callCount += 1
            if callCount == 1 {
                // First call: 401
                return jsonResponse(401, json: """
                {"error": {"code": "unauthorized", "message": "Token expired"}}
                """, for: request)
            } else {
                // Second call (after refresh): success
                XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer refreshed-token")
                return jsonResponse(200, json: """
                [{"library_id": "lib_1", "name": "Photos", "root_path": "/p", "created_at": "2024-01-01T00:00:00+00:00"}]
                """, for: request)
            }
        }

        let client = makeClient()
        await client.setRefreshHandler {
            await client.setAccessToken("refreshed-token")
            return true
        }

        let libs: [Library] = try await client.get("/v1/libraries")
        XCTAssertEqual(libs.count, 1)
        XCTAssertEqual(callCount, 2)
    }

    func testThrowsUnauthorizedWhenRefreshFails() async {
        MockURLProtocol.requestHandler = { request in
            return jsonResponse(401, json: """
            {"error": {"code": "unauthorized", "message": "Token expired"}}
            """, for: request)
        }

        let client = makeClient()
        await client.setRefreshHandler { return false }

        do {
            let _: [Library] = try await client.get("/v1/libraries")
            XCTFail("Expected unauthorized")
        } catch let error as APIError {
            XCTAssertEqual(error, .unauthorized)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testThrowsUnauthorizedWhenNoRefreshHandler() async {
        MockURLProtocol.requestHandler = { request in
            return jsonResponse(401, json: """
            {"error": {"code": "unauthorized", "message": "Token expired"}}
            """, for: request)
        }

        let client = makeClient()
        // No refresh handler set

        do {
            let _: [Library] = try await client.get("/v1/libraries")
            XCTFail("Expected unauthorized")
        } catch let error as APIError {
            XCTAssertEqual(error, .unauthorized)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - postNoRetry skips refresh

    func testPostNoRetrySkipsRefreshOn401() async {
        MockURLProtocol.requestHandler = { request in
            return jsonResponse(401, json: """
            {"error": {"code": "unauthorized", "message": "Refresh token expired"}}
            """, for: request)
        }

        let client = makeClient()
        let refreshCalled = SendableFlag()
        await client.setRefreshHandler {
            refreshCalled.set()
            return true
        }

        do {
            let _: LoginResponse = try await client.postNoRetry("/v1/auth/refresh")
            XCTFail("Expected unauthorized")
        } catch let error as APIError {
            XCTAssertEqual(error, .unauthorized)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
        XCTAssertFalse(refreshCalled.value, "Refresh handler should not be called for postNoRetry")
    }

    // MARK: - getData (binary)

    func testGetDataReturnsBinaryData() async throws {
        let imageData = Data([0xFF, 0xD8, 0xFF, 0xE0]) // JPEG magic bytes
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 200,
                httpVersion: nil, headerFields: ["Content-Type": "image/jpeg"]
            )!
            return (response, imageData)
        }

        let client = makeClient()
        let data = try await client.getData("/v1/assets/ast_1/proxy")
        XCTAssertEqual(data, imageData)
    }

    func testGetDataReturnsNilFor404() async throws {
        MockURLProtocol.requestHandler = { request in
            return jsonResponse(404, json: "Not Found", for: request)
        }

        let client = makeClient()
        let data = try await client.getData("/v1/assets/ast_1/proxy")
        XCTAssertNil(data)
    }

    func testGetDataThrowsNoTokenWhenNotSet() async {
        let client = makeClient(token: nil)
        do {
            _ = try await client.getData("/v1/assets/ast_1/proxy")
            XCTFail("Expected APIError.noToken")
        } catch let error as APIError {
            XCTAssertEqual(error, .noToken)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testGetDataRefreshesOn401() async throws {
        var callCount = 0
        let imageData = Data([0x89, 0x50, 0x4E, 0x47]) // PNG magic bytes

        MockURLProtocol.requestHandler = { request in
            callCount += 1
            if callCount == 1 {
                return jsonResponse(401, json: "Unauthorized", for: request)
            } else {
                let response = HTTPURLResponse(
                    url: request.url!, statusCode: 200,
                    httpVersion: nil, headerFields: nil
                )!
                return (response, imageData)
            }
        }

        let client = makeClient()
        await client.setRefreshHandler {
            await client.setAccessToken("new-token")
            return true
        }

        let data = try await client.getData("/v1/assets/ast_1/thumbnail")
        XCTAssertEqual(data, imageData)
        XCTAssertEqual(callCount, 2)
    }

    // MARK: - Multipart upload

    func testPostMultipartSendsCorrectBoundary() async throws {
        MockURLProtocol.requestHandler = { request in
            let contentType = request.value(forHTTPHeaderField: "Content-Type")!
            XCTAssert(contentType.starts(with: "multipart/form-data; boundary="))

            if let bodyData = request.httpBody {
                let body = String(data: bodyData, encoding: .utf8)!
                XCTAssert(body.contains("name=\"library_id\""), "Missing library_id field")
                XCTAssert(body.contains("name=\"rel_path\""), "Missing rel_path field")
                XCTAssert(body.contains("filename=\"proxy.jpg\""), "Missing file")
            }

            return jsonResponse(200, json: """
            {"asset_id": "ast_new", "status": "created", "created": true, "proxy_key": null, "proxy_sha256": null, "thumbnail_key": null, "thumbnail_sha256": null, "width": null, "height": null}
            """, for: request)
        }

        let client = makeClient()
        let response: IngestResponse = try await client.postMultipart(
            "/v1/ingest",
            fields: ["library_id": "lib_1", "rel_path": "photo.jpg"],
            fileField: "proxy",
            fileData: Data([0xFF, 0xD8]),
            fileName: "proxy.jpg",
            mimeType: "image/jpeg"
        )
        XCTAssertEqual(response.assetId, "ast_new")
        XCTAssertTrue(response.created)
    }

    func testPostMultipartThrowsNoToken() async {
        let client = makeClient(token: nil)
        do {
            let _: IngestResponse = try await client.postMultipart(
                "/v1/ingest",
                fields: [:],
                fileField: "proxy",
                fileData: Data(),
                fileName: "f.jpg",
                mimeType: "image/jpeg"
            )
            XCTFail("Expected noToken")
        } catch let error as APIError {
            XCTAssertEqual(error, .noToken)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - deleteWithBody

    func testDeleteWithBodySendsPayload() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "DELETE")
            // Body may be in httpBody or httpBodyStream depending on URLSession internals
            if let bodyData = request.httpBody {
                let body = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
                XCTAssertNotNil(body["asset_ids"])
            }
            return jsonResponse(200, json: """
            {"trashed": ["a1", "a2"], "not_found": []}
            """, for: request)
        }

        let client = makeClient()
        let result: BatchDeleteResponse = try await client.deleteWithBody(
            "/v1/assets/batch-delete",
            body: BatchDeleteRequest(assetIds: ["a1", "a2"])
        )
        XCTAssertEqual(result.trashed.count, 2)
    }
}
