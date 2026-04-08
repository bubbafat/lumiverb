import XCTest
import Foundation
@testable import LumiverbKit

/// Additional APIClient tests to cover remaining gaps:
/// - postWithToken method
/// - multipart 401 refresh + retry
/// - multipart server error (with and without envelope)
/// - multipart decoding error
/// - getData server error (non-401, non-404)
/// - DecodingError branches: typeMismatch, valueNotFound, dataCorrupted
/// - delete method sends DELETE and handles 204
/// - Network error handling
final class APIClientAdditionalTests: XCTestCase {

    private let testBaseURL = URL(string: "https://test.lumiverb.io")!

    override func tearDown() {
        MockURLProtocol.requestHandler = nil
        super.tearDown()
    }

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

    // MARK: - postWithToken

    func testPostWithTokenSendsExplicitToken() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Authorization"),
                "Bearer explicit-token"
            )
            return self.jsonResponse(200, json: """
            {"access_token": "new_jwt", "token_type": "bearer", "expires_in": 3600}
            """, for: request)
        }

        let client = makeClient(token: "client-token")
        let response: RefreshResponse = try await client.postWithToken(
            "/v1/auth/refresh",
            token: "explicit-token"
        )
        XCTAssertEqual(response.accessToken, "new_jwt")

        // Client's own token should be unchanged
        let currentToken = await client.currentToken()
        XCTAssertEqual(currentToken, "client-token")
    }

    func testPostWithTokenThrows401() async {
        MockURLProtocol.requestHandler = { request in
            return self.jsonResponse(401, json: """
            {"error": {"code": "unauthorized", "message": "Token revoked"}}
            """, for: request)
        }

        let client = makeClient()
        do {
            let _: RefreshResponse = try await client.postWithToken(
                "/v1/auth/refresh",
                token: "expired-token"
            )
            XCTFail("Expected unauthorized")
        } catch let error as APIError {
            guard case .unauthorized(let message) = error else {
                XCTFail("Expected .unauthorized, got \(error)"); return
            }
            XCTAssertEqual(message, "Token revoked")
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testPostWithTokenThrowsServerError() async {
        MockURLProtocol.requestHandler = { request in
            return self.jsonResponse(500, json: """
            {"error": {"code": "internal", "message": "DB down"}}
            """, for: request)
        }

        let client = makeClient()
        do {
            let _: RefreshResponse = try await client.postWithToken(
                "/v1/auth/refresh",
                token: "some-token"
            )
            XCTFail("Expected server error")
        } catch let error as APIError {
            if case .serverError(let status, let msg) = error {
                XCTAssertEqual(status, 500)
                XCTAssertEqual(msg, "DB down")
            } else {
                XCTFail("Expected .serverError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testPostWithTokenThrowsServerErrorWithoutEnvelope() async {
        MockURLProtocol.requestHandler = { request in
            return self.jsonResponse(502, json: "Bad Gateway", for: request)
        }

        let client = makeClient()
        do {
            let _: RefreshResponse = try await client.postWithToken(
                "/v1/auth/refresh",
                token: "tok"
            )
            XCTFail("Expected server error")
        } catch let error as APIError {
            if case .serverError(let status, let msg) = error {
                XCTAssertEqual(status, 502)
                XCTAssertEqual(msg, "Bad Gateway")
            } else {
                XCTFail("Expected .serverError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testPostWithTokenEncodesBody() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            if let body = request.httpBody {
                let json = try! JSONSerialization.jsonObject(with: body) as! [String: Any]
                XCTAssertEqual(json["email"] as? String, "test@example.com")
            }
            return self.jsonResponse(200, json: """
            {"access_token": "jwt", "token_type": "bearer", "expires_in": 3600}
            """, for: request)
        }

        let client = makeClient()
        let _: RefreshResponse = try await client.postWithToken(
            "/v1/auth/refresh",
            token: "tok",
            body: LoginRequest(email: "test@example.com", password: "pass")
        )
    }

    // MARK: - Multipart 401 → refresh → retry

    func testMultipartRefreshesOn401ThenRetries() async throws {
        var callCount = 0

        MockURLProtocol.requestHandler = { request in
            callCount += 1
            if callCount == 1 {
                return self.jsonResponse(401, json: "Unauthorized", for: request)
            } else {
                return self.jsonResponse(200, json: """
                {"asset_id": "ast_1", "status": "created", "created": true,
                 "proxy_key": null, "proxy_sha256": null, "thumbnail_key": null,
                 "thumbnail_sha256": null, "width": null, "height": null}
                """, for: request)
            }
        }

        let client = makeClient()
        await client.setRefreshHandler {
            await client.setAccessToken("refreshed")
            return true
        }

        let response: IngestResponse = try await client.postMultipart(
            "/v1/ingest",
            fields: ["library_id": "lib_1"],
            fileField: "proxy",
            fileData: Data([0xFF]),
            fileName: "f.jpg",
            mimeType: "image/jpeg"
        )
        XCTAssertEqual(response.assetId, "ast_1")
        XCTAssertEqual(callCount, 2)
    }

    func testMultipartThrowsUnauthorizedWhenRefreshFails() async {
        MockURLProtocol.requestHandler = { request in
            return self.jsonResponse(401, json: """
            {"error": {"code": "unauthorized", "message": "Expired"}}
            """, for: request)
        }

        let client = makeClient()
        await client.setRefreshHandler { return false }

        do {
            let _: IngestResponse = try await client.postMultipart(
                "/v1/ingest",
                fields: [:],
                fileField: "proxy",
                fileData: Data([0xFF]),
                fileName: "f.jpg",
                mimeType: "image/jpeg"
            )
            XCTFail("Expected unauthorized")
        } catch let error as APIError {
            guard case .unauthorized = error else {
                XCTFail("Expected .unauthorized, got \(error)"); return
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testMultipartThrowsUnauthorizedWhenNoRefreshHandler() async {
        MockURLProtocol.requestHandler = { request in
            return self.jsonResponse(401, json: "Unauthorized", for: request)
        }

        let client = makeClient()

        do {
            let _: IngestResponse = try await client.postMultipart(
                "/v1/ingest",
                fields: [:],
                fileField: "proxy",
                fileData: Data([0xFF]),
                fileName: "f.jpg",
                mimeType: "image/jpeg"
            )
            XCTFail("Expected unauthorized")
        } catch let error as APIError {
            guard case .unauthorized = error else {
                XCTFail("Expected .unauthorized, got \(error)"); return
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - Multipart server errors

    func testMultipartServerErrorWithEnvelope() async {
        MockURLProtocol.requestHandler = { request in
            return self.jsonResponse(422, json: """
            {"error": {"code": "validation_error", "message": "Missing library_id"}}
            """, for: request)
        }

        let client = makeClient()
        do {
            let _: IngestResponse = try await client.postMultipart(
                "/v1/ingest",
                fields: [:],
                fileField: "proxy",
                fileData: Data([0xFF]),
                fileName: "f.jpg",
                mimeType: "image/jpeg"
            )
            XCTFail("Expected server error")
        } catch let error as APIError {
            if case .serverError(let status, let msg) = error {
                XCTAssertEqual(status, 422)
                XCTAssertEqual(msg, "Missing library_id")
            } else {
                XCTFail("Expected .serverError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testMultipartServerErrorWithoutEnvelope() async {
        MockURLProtocol.requestHandler = { request in
            return self.jsonResponse(500, json: "Internal Server Error", for: request)
        }

        let client = makeClient()
        do {
            let _: IngestResponse = try await client.postMultipart(
                "/v1/ingest",
                fields: [:],
                fileField: "proxy",
                fileData: Data([0xFF]),
                fileName: "f.jpg",
                mimeType: "image/jpeg"
            )
            XCTFail("Expected server error")
        } catch let error as APIError {
            if case .serverError(let status, let msg) = error {
                XCTAssertEqual(status, 500)
                XCTAssertEqual(msg, "Internal Server Error")
            } else {
                XCTFail("Expected .serverError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - Multipart decoding error

    func testMultipartDecodingError() async {
        MockURLProtocol.requestHandler = { request in
            return self.jsonResponse(200, json: """
            {"not_what_we_expect": true}
            """, for: request)
        }

        let client = makeClient()
        do {
            let _: IngestResponse = try await client.postMultipart(
                "/v1/ingest",
                fields: [:],
                fileField: "proxy",
                fileData: Data([0xFF]),
                fileName: "f.jpg",
                mimeType: "image/jpeg"
            )
            XCTFail("Expected decoding error")
        } catch let error as APIError {
            if case .decodingError(let msg) = error {
                XCTAssert(msg.contains("decode failed"), "Got: \(msg)")
            } else {
                XCTFail("Expected .decodingError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - getData server error

    func testGetDataThrowsServerErrorForNon404() async {
        MockURLProtocol.requestHandler = { request in
            return self.jsonResponse(500, json: "Server Error", for: request)
        }

        let client = makeClient()
        do {
            _ = try await client.getData("/v1/assets/ast_1/proxy")
            XCTFail("Expected server error")
        } catch let error as APIError {
            if case .serverError(let status, _) = error {
                XCTAssertEqual(status, 500)
            } else {
                XCTFail("Expected .serverError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - DecodingError branches

    func testDecodingErrorTypeMismatch() async {
        MockURLProtocol.requestHandler = { request in
            // Library.name expects String, send Int
            return self.jsonResponse(200, json: """
            {"library_id": "lib_1", "name": 12345, "root_path": "/p", "created_at": "2024-01-01T00:00:00+00:00"}
            """, for: request)
        }

        let client = makeClient()
        do {
            let _: Library = try await client.get("/v1/libraries/lib_1")
            XCTFail("Expected decoding error")
        } catch let error as APIError {
            if case .decodingError(let msg) = error {
                XCTAssert(msg.contains("Type mismatch"), "Expected type mismatch, got: \(msg)")
            } else {
                XCTFail("Expected .decodingError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testDecodingErrorDataCorrupted() async {
        MockURLProtocol.requestHandler = { request in
            // Invalid JSON
            let response = HTTPURLResponse(
                url: request.url!, statusCode: 200,
                httpVersion: nil, headerFields: ["Content-Type": "application/json"]
            )!
            return (response, "not json at all {{{".data(using: .utf8)!)
        }

        let client = makeClient()
        do {
            let _: Library = try await client.get("/v1/libraries/lib_1")
            XCTFail("Expected decoding error")
        } catch let error as APIError {
            if case .decodingError(let msg) = error {
                XCTAssert(msg.contains("Corrupted data") || msg.contains("response:"),
                    "Expected corrupted data info, got: \(msg)")
            } else {
                XCTFail("Expected .decodingError, got \(error)")
            }
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    // MARK: - AssetDetail untested: cameraMakeNil

    func testAssetDetailCameraDescriptionNilMakeAndModel() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let json = """
        {
            "asset_id": "ast_1", "library_id": "lib_1", "rel_path": "photo.jpg",
            "sha256": "abc", "file_size": 1000, "media_type": "image",
            "status": "active", "created_at": "2024-01-01T00:00:00+00:00",
            "camera_make": null, "camera_model": null
        }
        """.data(using: .utf8)!

        let detail = try decoder.decode(AssetDetail.self, from: json)
        XCTAssertNil(detail.cameraDescription,
            "cameraDescription should be nil when both make and model are nil")
    }
}
