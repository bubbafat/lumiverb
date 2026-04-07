import XCTest
import Foundation
@testable import LumiverbKit

final class APIClientTests: XCTestCase {

    func testThrowsNoTokenWhenNotSet() async {
        let client = APIClient(baseURL: URL(string: "https://example.com")!)
        do {
            let _: [Library] = try await client.get("/v1/libraries")
            XCTFail("Expected APIError.noToken")
        } catch let error as APIError {
            XCTAssertEqual(error, .noToken)
        } catch {
            XCTFail("Unexpected error: \(error)")
        }
    }

    func testSetsAndReadsToken() async {
        let client = APIClient(baseURL: URL(string: "https://example.com")!)
        let before = await client.currentToken()
        XCTAssertNil(before)

        await client.setAccessToken("test-token")
        let after = await client.currentToken()
        XCTAssertEqual(after, "test-token")
    }
}

final class ModelDecodingTests: XCTestCase {

    private var decoder: JSONDecoder {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }

    func testDecodesLibrary() throws {
        let json = """
        {
            "library_id": "lib_123",
            "name": "Photos",
            "root_path": "/mnt/photos",
            "created_at": "2024-01-15T10:00:00+00:00"
        }
        """.data(using: .utf8)!

        let lib = try decoder.decode(Library.self, from: json)
        XCTAssertEqual(lib.libraryId, "lib_123")
        XCTAssertEqual(lib.name, "Photos")
        XCTAssertEqual(lib.rootPath, "/mnt/photos")
        XCTAssertEqual(lib.id, "lib_123")
    }

    func testDecodesLibraryList() throws {
        let json = """
        [
            {"library_id": "a", "name": "A", "root_path": "/a", "created_at": "2024-01-01T00:00:00+00:00"},
            {"library_id": "b", "name": "B", "root_path": "/b", "created_at": "2024-01-02T00:00:00+00:00"}
        ]
        """.data(using: .utf8)!

        let libs = try decoder.decode(LibraryListResponse.self, from: json)
        XCTAssertEqual(libs.count, 2)
        XCTAssertEqual(libs[0].name, "A")
        XCTAssertEqual(libs[1].name, "B")
    }

    func testDecodesCurrentUser() throws {
        let json = """
        {"user_id": "usr_1", "email": "test@example.com", "role": "admin"}
        """.data(using: .utf8)!

        let user = try decoder.decode(CurrentUser.self, from: json)
        XCTAssertEqual(user.userId, "usr_1")
        XCTAssertEqual(user.email, "test@example.com")
        XCTAssertEqual(user.role, "admin")
    }

    func testDecodesErrorEnvelope() throws {
        let json = """
        {"error": {"code": "not_found", "message": "Library not found"}}
        """.data(using: .utf8)!

        let envelope = try decoder.decode(ErrorEnvelope.self, from: json)
        XCTAssertEqual(envelope.error.code, "not_found")
        XCTAssertEqual(envelope.error.message, "Library not found")
    }
}
