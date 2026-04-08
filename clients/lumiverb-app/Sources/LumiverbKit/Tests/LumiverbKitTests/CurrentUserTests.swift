import XCTest
@testable import LumiverbKit

final class CurrentUserTests: XCTestCase {

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    func testDecodesFullCurrentUser() throws {
        let json = """
        {"user_id": "usr_123", "email": "alice@example.com", "role": "admin"}
        """.data(using: .utf8)!

        let user = try decoder.decode(CurrentUser.self, from: json)
        XCTAssertEqual(user.userId, "usr_123")
        XCTAssertEqual(user.email, "alice@example.com")
        XCTAssertEqual(user.role, "admin")
    }

    func testDecodesCurrentUserNullOptionals() throws {
        let json = """
        {"user_id": null, "email": null, "role": "viewer"}
        """.data(using: .utf8)!

        let user = try decoder.decode(CurrentUser.self, from: json)
        XCTAssertNil(user.userId)
        XCTAssertNil(user.email)
        XCTAssertEqual(user.role, "viewer")
    }

    func testDecodesCurrentUserMissingOptionals() throws {
        let json = """
        {"role": "editor"}
        """.data(using: .utf8)!

        let user = try decoder.decode(CurrentUser.self, from: json)
        XCTAssertNil(user.userId)
        XCTAssertNil(user.email)
        XCTAssertEqual(user.role, "editor")
    }

    // MARK: - displayName

    func testDisplayNameReturnsEmailWhenPresent() throws {
        let json = """
        {"email": "bob@example.com", "role": "editor"}
        """.data(using: .utf8)!

        let user = try decoder.decode(CurrentUser.self, from: json)
        XCTAssertEqual(user.displayName, "bob@example.com")
    }

    func testDisplayNameFallsBackToRoleWhenNoEmail() throws {
        let json = """
        {"role": "admin"}
        """.data(using: .utf8)!

        let user = try decoder.decode(CurrentUser.self, from: json)
        XCTAssertEqual(user.displayName, "admin")
    }
}
