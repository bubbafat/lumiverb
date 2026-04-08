import XCTest
@testable import LumiverbKit

/// Tests decoding of `FaceListItem`, `FaceMatchedPerson`, the flexible
/// `FaceBoundingBox`, and the encoding of `FaceAssignRequest`.
///
/// `FaceBoundingBox` accepts both wire formats (`{x,y,w,h}` from
/// InsightFace and `{x1,y1,x2,y2}` from the Swift Vision path) so the
/// macOS lightbox can render face overlays correctly regardless of which
/// detection provider produced any given asset's faces.
final class FaceListItemTests: XCTestCase {

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    private let snakeEncoder: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        e.outputFormatting = [.sortedKeys]
        return e
    }()

    // MARK: - FaceBoundingBox: both wire formats

    func testDecodesFaceBoundingBoxXYWHFormat() throws {
        let json = #"{"x": 0.10, "y": 0.20, "w": 0.30, "h": 0.40}"#.data(using: .utf8)!
        let bb = try decoder.decode(FaceBoundingBox.self, from: json)
        XCTAssertEqual(bb.x, 0.10, accuracy: 1e-6)
        XCTAssertEqual(bb.y, 0.20, accuracy: 1e-6)
        XCTAssertEqual(bb.width, 0.30, accuracy: 1e-6)
        XCTAssertEqual(bb.height, 0.40, accuracy: 1e-6)
        XCTAssertEqual(bb.x2, 0.40, accuracy: 1e-6)
        XCTAssertEqual(bb.y2, 0.60, accuracy: 1e-6)
    }

    func testDecodesFaceBoundingBoxX1Y1X2Y2Format() throws {
        let json = #"{"x1": 0.10, "y1": 0.20, "x2": 0.50, "y2": 0.70}"#.data(using: .utf8)!
        let bb = try decoder.decode(FaceBoundingBox.self, from: json)
        XCTAssertEqual(bb.x, 0.10, accuracy: 1e-6)
        XCTAssertEqual(bb.y, 0.20, accuracy: 1e-6)
        XCTAssertEqual(bb.width, 0.40, accuracy: 1e-6)
        XCTAssertEqual(bb.height, 0.50, accuracy: 1e-6)
        XCTAssertEqual(bb.x2, 0.50, accuracy: 1e-6)
        XCTAssertEqual(bb.y2, 0.70, accuracy: 1e-6)
    }

    /// `convertFromSnakeCase` is in effect on the wire decoder. Make sure
    /// the bbox decoder still works through it (the field names are all
    /// single-letter and have no underscores so the strategy is a no-op,
    /// but this confirms there are no surprise interactions).
    func testFaceBoundingBoxThroughSnakeCaseDecoder() throws {
        let json = #"{"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}"#.data(using: .utf8)!
        let bb = try decoder.decode(FaceBoundingBox.self, from: json)
        XCTAssertEqual(bb.width, 0.3, accuracy: 1e-6)
    }

    func testFaceBoundingBoxRejectsMissingShape() {
        let json = #"{"foo": "bar"}"#.data(using: .utf8)!
        XCTAssertThrowsError(try decoder.decode(FaceBoundingBox.self, from: json))
    }

    // MARK: - FaceMatchedPerson

    func testDecodesFaceMatchedPerson() throws {
        let json = """
        {"person_id": "per_alice", "display_name": "Alice", "dismissed": false}
        """.data(using: .utf8)!

        let p = try decoder.decode(FaceMatchedPerson.self, from: json)
        XCTAssertEqual(p.personId, "per_alice")
        XCTAssertEqual(p.displayName, "Alice")
        XCTAssertFalse(p.dismissed)
    }

    func testDecodesFaceMatchedPersonDismissed() throws {
        let json = """
        {"person_id": "per_dis", "display_name": "Dismissed Cluster", "dismissed": true}
        """.data(using: .utf8)!

        let p = try decoder.decode(FaceMatchedPerson.self, from: json)
        XCTAssertTrue(p.dismissed)
    }

    // MARK: - FaceListItem / FaceListResponse

    func testDecodesFaceListItemWithPerson() throws {
        let json = """
        {
            "face_id": "face_1",
            "bounding_box": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4},
            "detection_confidence": 0.92,
            "person": {"person_id": "per_alice", "display_name": "Alice", "dismissed": false}
        }
        """.data(using: .utf8)!

        let face = try decoder.decode(FaceListItem.self, from: json)
        XCTAssertEqual(face.faceId, "face_1")
        XCTAssertEqual(face.detectionConfidence, 0.92)
        XCTAssertNotNil(face.boundingBox)
        XCTAssertEqual(face.boundingBox?.width, 0.3)
        XCTAssertEqual(face.person?.personId, "per_alice")
        XCTAssertEqual(face.person?.displayName, "Alice")
        XCTAssertFalse(face.person?.dismissed ?? true)
    }

    func testDecodesFaceListItemUnassigned() throws {
        let json = """
        {
            "face_id": "face_2",
            "bounding_box": {"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0},
            "detection_confidence": null,
            "person": null
        }
        """.data(using: .utf8)!

        let face = try decoder.decode(FaceListItem.self, from: json)
        XCTAssertEqual(face.faceId, "face_2")
        XCTAssertNil(face.detectionConfidence)
        XCTAssertNil(face.person)
        XCTAssertEqual(face.boundingBox?.width, 1.0)
        XCTAssertEqual(face.boundingBox?.height, 1.0)
    }

    func testDecodesFaceListResponseMixed() throws {
        // Realistic mixed payload: one assigned, one unassigned, one
        // dismissed-cluster face (gray in the lightbox), and a face from
        // the *other* bbox format provider — proves the lightbox can
        // render an asset whose faces came from different detectors.
        let json = """
        {
            "faces": [
                {
                    "face_id": "f1",
                    "bounding_box": {"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
                    "detection_confidence": 0.95,
                    "person": {"person_id": "per_a", "display_name": "Alice", "dismissed": false}
                },
                {
                    "face_id": "f2",
                    "bounding_box": {"x1": 0.4, "y1": 0.4, "x2": 0.6, "y2": 0.6},
                    "detection_confidence": 0.87,
                    "person": null
                },
                {
                    "face_id": "f3",
                    "bounding_box": {"x": 0.7, "y": 0.7, "w": 0.1, "h": 0.1},
                    "detection_confidence": 0.99,
                    "person": {"person_id": "per_dis", "display_name": "noise", "dismissed": true}
                }
            ]
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(FaceListResponse.self, from: json)
        XCTAssertEqual(response.faces.count, 3)
        XCTAssertNotNil(response.faces[0].person)
        XCTAssertNil(response.faces[1].person)
        XCTAssertTrue(response.faces[2].person?.dismissed ?? false)
        // f2 used the {x1,y1,x2,y2} format — verify it normalized correctly.
        let f2bb = try XCTUnwrap(response.faces[1].boundingBox)
        XCTAssertEqual(f2bb.width, 0.2, accuracy: 1e-6)
        XCTAssertEqual(f2bb.height, 0.2, accuracy: 1e-6)
    }

    // MARK: - FaceAssignRequest encoding

    /// Default Swift JSONEncoder *omits* nil optional fields rather than
    /// encoding them as `"key":null`. Server-side Pydantic accepts both
    /// forms (an Optional field with None default), so omit-nil is the
    /// cleaner wire format and we standardize on it.
    func testEncodesFaceAssignRequestWithPersonId() throws {
        let req = FaceAssignRequest(personId: "per_alice")
        let data = try snakeEncoder.encode(req)
        let json = String(data: data, encoding: .utf8)!
        XCTAssertEqual(json, #"{"person_id":"per_alice"}"#)
    }

    func testEncodesFaceAssignRequestWithNewPersonName() throws {
        let req = FaceAssignRequest(newPersonName: "Bob")
        let data = try snakeEncoder.encode(req)
        let json = String(data: data, encoding: .utf8)!
        XCTAssertEqual(json, #"{"new_person_name":"Bob"}"#)
    }
}
