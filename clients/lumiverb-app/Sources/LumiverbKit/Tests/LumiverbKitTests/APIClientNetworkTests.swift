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

/// URLSession may move the POST body from `httpBody` onto `httpBodyStream`
/// before handing the request to URLProtocol. Tests that want to assert on
/// the JSON body have to fall back to draining the stream.
private func readRequestBody(_ request: URLRequest) -> Data? {
    if let body = request.httpBody, !body.isEmpty {
        return body
    }
    guard let stream = request.httpBodyStream else { return nil }
    stream.open()
    defer { stream.close() }
    var data = Data()
    let bufferSize = 4096
    let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
    defer { buffer.deallocate() }
    while stream.hasBytesAvailable {
        let read = stream.read(buffer, maxLength: bufferSize)
        if read <= 0 { break }
        data.append(buffer, count: read)
    }
    return data.isEmpty ? nil : data
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
            guard case .unauthorized = error else {
                    XCTFail("Expected .unauthorized, got \(error)"); return
                }
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
            guard case .unauthorized = error else {
                    XCTFail("Expected .unauthorized, got \(error)"); return
                }
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
            guard case .unauthorized = error else {
                    XCTFail("Expected .unauthorized, got \(error)"); return
                }
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

    // MARK: - Library creation

    func testCreateLibraryPostsSnakeCaseBody() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/v1/libraries")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")

            // The generic post() encoder converts camelCase → snake_case.
            // Verify the wire payload uses the exact keys the server's
            // CreateLibraryRequest BaseModel expects. URLSession sometimes
            // moves the body onto httpBodyStream — read whichever is set.
            guard let bodyData = readRequestBody(request) else {
                XCTFail("expected request body")
                return jsonResponse(400, json: "{}", for: request)
            }
            let body = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
            XCTAssertEqual(body["name"] as? String, "My Photos")
            XCTAssertEqual(body["root_path"] as? String, "/Users/alice/Pictures")
            XCTAssertEqual(body.keys.sorted(), ["name", "root_path"])

            // POST /v1/libraries returns a LibraryResponse — same field shape
            // as list items but without last_scan_at/status. Library's
            // Decodable should accept the subset because those fields are
            // optional.
            return jsonResponse(200, json: """
            {"library_id": "lib_new", "name": "My Photos", "root_path": "/Users/alice/Pictures", "is_public": false}
            """, for: request)
        }

        let client = makeClient()
        let created: Library = try await client.post(
            "/v1/libraries",
            body: CreateLibraryRequest(name: "My Photos", rootPath: "/Users/alice/Pictures")
        )
        XCTAssertEqual(created.libraryId, "lib_new")
        XCTAssertEqual(created.name, "My Photos")
        XCTAssertEqual(created.rootPath, "/Users/alice/Pictures")
        XCTAssertEqual(created.isPublic, false)
        // List-only fields should decode as nil from the POST response.
        XCTAssertNil(created.lastScanAt)
        XCTAssertNil(created.status)
    }

    func testCreateLibrarySurfacesDuplicateNameError() async {
        MockURLProtocol.requestHandler = { request in
            // Match the server's error envelope shape.
            return jsonResponse(409, json: """
            {"error": {"code": "conflict", "message": "A library with this name already exists"}}
            """, for: request)
        }

        let client = makeClient()
        do {
            let _: Library = try await client.post(
                "/v1/libraries",
                body: CreateLibraryRequest(name: "Dupe", rootPath: "/tmp/x")
            )
            XCTFail("expected serverError")
        } catch let APIError.serverError(statusCode, message) {
            XCTAssertEqual(statusCode, 409)
            XCTAssertEqual(message, "A library with this name already exists")
        } catch {
            XCTFail("unexpected error: \(error)")
        }
    }

    // MARK: - Library settings (rename / re-root)

    func testUpdateLibraryPatchesOnlyProvidedFields() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "PATCH")
            XCTAssertEqual(request.url?.path, "/v1/libraries/lib_1")

            // Swift Encodable omits nil fields by default, so a name-only
            // update must not send a root_path key at all — the server
            // treats key absence as "leave unchanged".
            guard let bodyData = readRequestBody(request) else {
                XCTFail("expected request body")
                return jsonResponse(400, json: "{}", for: request)
            }
            let body = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
            XCTAssertEqual(body["name"] as? String, "Renamed")
            XCTAssertNil(body["root_path"])
            XCTAssertNil(body["is_public"])

            return jsonResponse(200, json: """
            {"library_id": "lib_1", "name": "Renamed", "root_path": "/old/path", "is_public": false}
            """, for: request)
        }

        let client = makeClient()
        let updated: Library = try await client.patch(
            "/v1/libraries/lib_1",
            body: LibraryUpdateRequest(name: "Renamed")
        )
        XCTAssertEqual(updated.name, "Renamed")
        XCTAssertEqual(updated.rootPath, "/old/path")
    }

    func testUpdateLibraryCanChangeRootPath() async throws {
        MockURLProtocol.requestHandler = { request in
            guard let bodyData = readRequestBody(request) else {
                XCTFail("expected request body")
                return jsonResponse(400, json: "{}", for: request)
            }
            let body = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
            XCTAssertEqual(body["root_path"] as? String, "/new/root")
            XCTAssertNil(body["name"])

            return jsonResponse(200, json: """
            {"library_id": "lib_1", "name": "Photos", "root_path": "/new/root", "is_public": false}
            """, for: request)
        }

        let client = makeClient()
        let updated: Library = try await client.patch(
            "/v1/libraries/lib_1",
            body: LibraryUpdateRequest(rootPath: "/new/root")
        )
        XCTAssertEqual(updated.rootPath, "/new/root")
    }

    // MARK: - Library path filters

    func testAddLibraryFilterSendsSnakeCaseBody() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/v1/libraries/lib_1/filters")

            guard let bodyData = readRequestBody(request) else {
                XCTFail("expected request body")
                return jsonResponse(400, json: "{}", for: request)
            }
            let body = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
            XCTAssertEqual(body["type"] as? String, "exclude")
            XCTAssertEqual(body["pattern"] as? String, "**/Proxy/**")
            XCTAssertEqual(body["trash_matching"] as? Bool, true)

            return jsonResponse(201, json: """
            {
                "filter_id": "fil_123",
                "type": "exclude",
                "pattern": "**/Proxy/**",
                "created_at": "2026-04-09T12:00:00+00:00",
                "trashed_count": 42
            }
            """, for: request)
        }

        let client = makeClient()
        let result: LibraryFilterItemWithType = try await client.post(
            "/v1/libraries/lib_1/filters",
            body: CreateLibraryFilterRequest(
                type: "exclude",
                pattern: "**/Proxy/**",
                trashMatching: true
            )
        )
        XCTAssertEqual(result.filterId, "fil_123")
        XCTAssertEqual(result.type, "exclude")
        XCTAssertEqual(result.trashedCount, 42)
    }

    func testPreviewLibraryFilterDecodesMatchCount() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/v1/libraries/lib_1/filters/preview")
            return jsonResponse(200, json: """
            {"matching_asset_count": 17}
            """, for: request)
        }

        let client = makeClient()
        let preview: PreviewFilterResponse = try await client.post(
            "/v1/libraries/lib_1/filters/preview",
            body: PreviewFilterRequest(type: "exclude", pattern: "**/*.tmp")
        )
        XCTAssertEqual(preview.matchingAssetCount, 17)
    }

    func testPreviewLibraryFilterSurfacesInvalidPatternError() async {
        MockURLProtocol.requestHandler = { request in
            return jsonResponse(400, json: """
            {"error": {"code": "bad_request", "message": "invalid glob pattern"}}
            """, for: request)
        }

        let client = makeClient()
        do {
            let _: PreviewFilterResponse = try await client.post(
                "/v1/libraries/lib_1/filters/preview",
                body: PreviewFilterRequest(type: "exclude", pattern: "[unclosed")
            )
            XCTFail("expected serverError")
        } catch let APIError.serverError(statusCode, message) {
            XCTAssertEqual(statusCode, 400)
            XCTAssertEqual(message, "invalid glob pattern")
        } catch {
            XCTFail("unexpected error: \(error)")
        }
    }

    func testDeleteLibraryFilterSendsCorrectPath() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "DELETE")
            XCTAssertEqual(request.url?.path, "/v1/libraries/lib_1/filters/fil_abc")
            return jsonResponse(204, json: "", for: request)
        }

        let client = makeClient()
        try await client.delete("/v1/libraries/lib_1/filters/fil_abc")
    }

    func testListLibraryFiltersDecodesRichShape() async throws {
        // Round-trip GET /v1/libraries/{id}/filters into LibraryFiltersResponse
        // and verify the per-row filter_id + created_at flow through so the
        // settings UI can delete specific rows.
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/v1/libraries/lib_1/filters")
            return jsonResponse(200, json: """
            {
                "includes": [
                    {"filter_id": "fil_in1", "pattern": "photos/**", "created_at": "2025-06-01T00:00:00+00:00"}
                ],
                "excludes": [
                    {"filter_id": "fil_ex1", "pattern": "**/Proxy/**", "created_at": "2025-06-02T00:00:00+00:00"},
                    {"filter_id": "fil_ex2", "pattern": "**/.DS_Store", "created_at": "2025-06-03T00:00:00+00:00"}
                ]
            }
            """, for: request)
        }

        let client = makeClient()
        let filters: LibraryFiltersResponse = try await client.get("/v1/libraries/lib_1/filters")
        XCTAssertEqual(filters.includes.count, 1)
        XCTAssertEqual(filters.includes[0].filterId, "fil_in1")
        XCTAssertEqual(filters.includes[0].pattern, "photos/**")
        XCTAssertEqual(filters.excludes.count, 2)
        XCTAssertEqual(filters.excludes[0].filterId, "fil_ex1")
        XCTAssertEqual(filters.excludes[1].filterId, "fil_ex2")
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

    // MARK: - Ratings

    func testUpdateRatingSendsCorrectBody() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "PUT")
            XCTAssertEqual(request.url?.path, "/v1/assets/asset_1/rating")

            if let bodyData = readRequestBody(request) {
                let json = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
                XCTAssertEqual(json["stars"] as? Int, 4)
                XCTAssertEqual(json["color"] as? String, "blue")
                XCTAssertFalse(json.keys.contains("favorite"))
            } else {
                XCTFail("Expected request body")
            }

            return jsonResponse(200, json: """
            {"asset_id": "asset_1", "favorite": false, "stars": 4, "color": "blue"}
            """, for: request)
        }

        let client = makeClient()
        let rating = try await client.updateRating(
            assetId: "asset_1",
            body: RatingUpdateBody(stars: 4, color: .set(.blue))
        )
        XCTAssertEqual(rating.stars, 4)
        XCTAssertEqual(rating.color, .blue)
        XCTAssertFalse(rating.favorite)
    }

    func testUpdateRatingColorClearSendsNull() async throws {
        MockURLProtocol.requestHandler = { request in
            if let bodyData = readRequestBody(request) {
                let json = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
                XCTAssertTrue(json.keys.contains("color"))
                XCTAssertTrue(json["color"] is NSNull)
            } else {
                XCTFail("Expected request body")
            }

            return jsonResponse(200, json: """
            {"asset_id": "a1", "favorite": false, "stars": 0, "color": null}
            """, for: request)
        }

        let client = makeClient()
        let rating = try await client.updateRating(
            assetId: "a1",
            body: RatingUpdateBody(color: .clear)
        )
        XCTAssertNil(rating.color)
    }

    func testUpdateRatingColorUnchangedOmitsKey() async throws {
        MockURLProtocol.requestHandler = { request in
            if let bodyData = readRequestBody(request) {
                let json = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
                XCTAssertFalse(json.keys.contains("color"))
                XCTAssertEqual(json["favorite"] as? Bool, true)
            } else {
                XCTFail("Expected request body")
            }

            return jsonResponse(200, json: """
            {"asset_id": "a1", "favorite": true, "stars": 0, "color": null}
            """, for: request)
        }

        let client = makeClient()
        let rating = try await client.updateRating(
            assetId: "a1",
            body: RatingUpdateBody(favorite: true, color: .unchanged)
        )
        XCTAssertTrue(rating.favorite)
    }

    func testBatchUpdateRatings() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "PUT")
            XCTAssertEqual(request.url?.path, "/v1/assets/ratings")

            if let bodyData = readRequestBody(request) {
                let json = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
                XCTAssertEqual((json["asset_ids"] as? [String])?.count, 3)
                XCTAssertEqual(json["stars"] as? Int, 5)
            }

            return jsonResponse(200, json: """
            {"updated": 3}
            """, for: request)
        }

        let client = makeClient()
        let updated = try await client.batchUpdateRatings(
            body: BatchRatingUpdateBody(assetIds: ["a1", "a2", "a3"], stars: 5)
        )
        XCTAssertEqual(updated, 3)
    }

    func testLookupRatings() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/v1/assets/ratings/lookup")

            return jsonResponse(200, json: """
            {
                "ratings": {
                    "a1": {"favorite": true, "stars": 3, "color": "red"},
                    "a2": {"favorite": false, "stars": 0, "color": null}
                }
            }
            """, for: request)
        }

        let client = makeClient()
        let ratings = try await client.lookupRatings(assetIds: ["a1", "a2"])
        XCTAssertEqual(ratings.count, 2)
        XCTAssertEqual(ratings["a1"]?.stars, 3)
        XCTAssertEqual(ratings["a1"]?.color, .red)
        XCTAssertTrue(ratings["a1"]?.favorite ?? false)
        XCTAssertNil(ratings["a2"]?.color)
    }

    // MARK: - Collections

    private static let sampleCollectionJson = """
    {
        "collection_id": "col_1",
        "name": "Favorites",
        "description": null,
        "cover_asset_id": null,
        "owner_user_id": "user_1",
        "visibility": "private",
        "ownership": "own",
        "sort_order": "manual",
        "asset_count": 5,
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00"
    }
    """

    func testListCollections() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            XCTAssertEqual(request.url?.path, "/v1/collections")
            return jsonResponse(200, json: """
            {"items": [\(Self.sampleCollectionJson)]}
            """, for: request)
        }

        let client = makeClient()
        let cols = try await client.listCollections()
        XCTAssertEqual(cols.count, 1)
        XCTAssertEqual(cols[0].name, "Favorites")
        XCTAssertTrue(cols[0].isOwn)
    }

    func testCreateCollection() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.url?.path, "/v1/collections")

            if let bodyData = readRequestBody(request) {
                let json = try! JSONSerialization.jsonObject(with: bodyData) as! [String: Any]
                XCTAssertEqual(json["name"] as? String, "Test")
                XCTAssertEqual(json["visibility"] as? String, "private")
            }

            return jsonResponse(201, json: Self.sampleCollectionJson, for: request)
        }

        let client = makeClient()
        let col = try await client.createCollection(
            body: CreateCollectionRequest(name: "Test")
        )
        XCTAssertEqual(col.collectionId, "col_1")
    }

    func testUpdateCollection() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "PATCH")
            XCTAssertTrue(request.url?.path.contains("/v1/collections/col_1") ?? false)
            return jsonResponse(200, json: Self.sampleCollectionJson, for: request)
        }

        let client = makeClient()
        let col = try await client.updateCollection(
            id: "col_1", body: UpdateCollectionRequest(name: "Renamed")
        )
        XCTAssertEqual(col.name, "Favorites") // server returned sample
    }

    func testDeleteCollection() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "DELETE")
            return jsonResponse(204, json: "{}", for: request)
        }

        let client = makeClient()
        try await client.deleteCollection(id: "col_1")
    }

    func testAddAssetsToCollection() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertTrue(request.url?.path.contains("/assets") ?? false)
            return jsonResponse(200, json: """
            {"added": 2}
            """, for: request)
        }

        let client = makeClient()
        let added = try await client.addAssetsToCollection(id: "col_1", assetIds: ["a1", "a2"])
        XCTAssertEqual(added, 2)
    }

    func testRemoveAssetsFromCollection() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "DELETE")
            return jsonResponse(200, json: """
            {"removed": 1}
            """, for: request)
        }

        let client = makeClient()
        let removed = try await client.removeAssetsFromCollection(id: "col_1", assetIds: ["a1"])
        XCTAssertEqual(removed, 1)
    }

    func testListCollectionAssets() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "GET")
            let components = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
            let items = Dictionary(uniqueKeysWithValues:
                (components.queryItems ?? []).map { ($0.name, $0.value!) }
            )
            XCTAssertEqual(items["limit"], "50")
            return jsonResponse(200, json: """
            {
                "items": [{
                    "asset_id": "a1", "rel_path": "photo.jpg", "file_size": 1234,
                    "media_type": "image", "width": 800, "height": 600,
                    "taken_at": null, "status": "complete", "duration_sec": null,
                    "camera_make": null, "camera_model": null
                }],
                "next_cursor": "cur_2"
            }
            """, for: request)
        }

        let client = makeClient()
        let response = try await client.listCollectionAssets(id: "col_1", limit: 50)
        XCTAssertEqual(response.items.count, 1)
        XCTAssertEqual(response.items[0].assetId, "a1")
        XCTAssertEqual(response.nextCursor, "cur_2")
    }
}
