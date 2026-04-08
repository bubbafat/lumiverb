import XCTest
@testable import LumiverbKit

final class PersonItemTests: XCTestCase {

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    // MARK: - PersonItem

    func testDecodesPersonItem() throws {
        let json = """
        {
            "person_id": "per_abc",
            "display_name": "Alice",
            "face_count": 42,
            "representative_face_id": "face_001",
            "representative_asset_id": "ast_123",
            "confirmation_count": 5
        }
        """.data(using: .utf8)!

        let person = try decoder.decode(PersonItem.self, from: json)
        XCTAssertEqual(person.personId, "per_abc")
        XCTAssertEqual(person.displayName, "Alice")
        XCTAssertEqual(person.faceCount, 42)
        XCTAssertEqual(person.representativeFaceId, "face_001")
        XCTAssertEqual(person.representativeAssetId, "ast_123")
        XCTAssertEqual(person.confirmationCount, 5)
    }

    func testPersonItemIdentifiableId() throws {
        let json = """
        {
            "person_id": "per_xyz",
            "display_name": "Bob",
            "face_count": 7,
            "representative_face_id": null,
            "representative_asset_id": null,
            "confirmation_count": 0
        }
        """.data(using: .utf8)!

        let person = try decoder.decode(PersonItem.self, from: json)
        XCTAssertEqual(person.id, "per_xyz", "Identifiable id should equal personId")
        XCTAssertNil(person.representativeFaceId)
        XCTAssertNil(person.representativeAssetId)
    }

    // MARK: - PersonListResponse

    func testDecodesPersonListResponse() throws {
        let json = """
        {
            "items": [
                {
                    "person_id": "per_1",
                    "display_name": "Alice",
                    "face_count": 10,
                    "representative_face_id": "face_1",
                    "representative_asset_id": "ast_1",
                    "confirmation_count": 3
                },
                {
                    "person_id": "per_2",
                    "display_name": "Bob",
                    "face_count": 5,
                    "representative_face_id": null,
                    "representative_asset_id": null,
                    "confirmation_count": 0
                }
            ],
            "next_cursor": "cursor_abc"
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(PersonListResponse.self, from: json)
        XCTAssertEqual(response.items.count, 2)
        XCTAssertEqual(response.items[0].displayName, "Alice")
        XCTAssertEqual(response.items[1].displayName, "Bob")
        XCTAssertEqual(response.nextCursor, "cursor_abc")
    }

    func testDecodesPersonListResponseNullCursor() throws {
        let json = """
        {"items": [], "next_cursor": null}
        """.data(using: .utf8)!

        let response = try decoder.decode(PersonListResponse.self, from: json)
        XCTAssertTrue(response.items.isEmpty)
        XCTAssertNil(response.nextCursor)
    }
}
