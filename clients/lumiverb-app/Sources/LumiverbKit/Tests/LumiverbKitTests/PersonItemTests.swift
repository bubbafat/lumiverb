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

    // MARK: - PersonFaceItem / PersonFacesResponse

    func testDecodesPersonFaceItem() throws {
        let json = """
        {
            "face_id": "face_001",
            "asset_id": "ast_abc",
            "bounding_box": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            "detection_confidence": 0.95,
            "rel_path": "2025/Family/img.jpg"
        }
        """.data(using: .utf8)!

        let face = try decoder.decode(PersonFaceItem.self, from: json)
        XCTAssertEqual(face.faceId, "face_001")
        XCTAssertEqual(face.assetId, "ast_abc")
        XCTAssertEqual(face.detectionConfidence, 0.95)
        XCTAssertEqual(face.relPath, "2025/Family/img.jpg")
        XCTAssertNotNil(face.boundingBox)
        XCTAssertEqual(face.boundingBox?.x, 0.1)
        XCTAssertEqual(face.boundingBox?.width, 0.3)
        XCTAssertEqual(face.id, "face_001")
    }

    func testDecodesPersonFaceItemAllOptionalsNull() throws {
        let json = """
        {
            "face_id": "face_002",
            "asset_id": "ast_xyz",
            "bounding_box": null,
            "detection_confidence": null,
            "rel_path": null
        }
        """.data(using: .utf8)!

        let face = try decoder.decode(PersonFaceItem.self, from: json)
        XCTAssertEqual(face.faceId, "face_002")
        XCTAssertNil(face.boundingBox)
        XCTAssertNil(face.detectionConfidence)
        XCTAssertNil(face.relPath)
    }

    func testDecodesPersonFacesResponse() throws {
        let json = """
        {
            "items": [
                {
                    "face_id": "f1",
                    "asset_id": "a1",
                    "bounding_box": {"x1": 0.0, "y1": 0.0, "x2": 0.5, "y2": 0.5},
                    "detection_confidence": 0.9,
                    "rel_path": "p1.jpg"
                }
            ],
            "next_cursor": "f1"
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(PersonFacesResponse.self, from: json)
        XCTAssertEqual(response.items.count, 1)
        XCTAssertEqual(response.nextCursor, "f1")
        XCTAssertEqual(response.items[0].boundingBox?.width, 0.5)
    }

    // MARK: - NearestPersonItem

    func testDecodesNearestPersonItem() throws {
        let json = """
        {
            "person_id": "per_close",
            "display_name": "Maybe Susan",
            "face_count": 12,
            "distance": 0.18
        }
        """.data(using: .utf8)!

        let np = try decoder.decode(NearestPersonItem.self, from: json)
        XCTAssertEqual(np.personId, "per_close")
        XCTAssertEqual(np.displayName, "Maybe Susan")
        XCTAssertEqual(np.faceCount, 12)
        XCTAssertEqual(np.distance, 0.18, accuracy: 1e-6)
        XCTAssertEqual(np.id, "per_close")
    }

    // MARK: - Encodable mutation requests (snake_case round-trip)

    private let snakeEncoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = [.sortedKeys]
        return e
    }()

    func testEncodesPersonCreateRequestSnakeCase() throws {
        let req = PersonCreateRequest(displayName: "Alice", faceIds: ["f1", "f2"])
        let data = try snakeEncoder.encode(req)
        let json = String(data: data, encoding: .utf8)!
        XCTAssertEqual(json, #"{"display_name":"Alice","face_ids":["f1","f2"]}"#)
    }

    func testEncodesPersonCreateRequestNilFaceIds() throws {
        let req = PersonCreateRequest(displayName: "Empty Person")
        let data = try snakeEncoder.encode(req)
        let json = String(data: data, encoding: .utf8)!
        // face_ids omitted entirely (nil → not encoded by default)
        XCTAssertEqual(json, #"{"display_name":"Empty Person"}"#)
    }

    func testEncodesPersonUpdateRequest() throws {
        let req = PersonUpdateRequest(displayName: "Renamed")
        let data = try snakeEncoder.encode(req)
        XCTAssertEqual(String(data: data, encoding: .utf8), #"{"display_name":"Renamed"}"#)
    }

    func testEncodesMergeRequestSnakeCase() throws {
        let req = MergeRequest(sourcePersonId: "per_dupe")
        let data = try snakeEncoder.encode(req)
        XCTAssertEqual(String(data: data, encoding: .utf8), #"{"source_person_id":"per_dupe"}"#)
    }

    func testEncodesUndismissRequest() throws {
        let req = UndismissRequest(displayName: "Was Dismissed")
        let data = try snakeEncoder.encode(req)
        XCTAssertEqual(String(data: data, encoding: .utf8), #"{"display_name":"Was Dismissed"}"#)
    }
}
