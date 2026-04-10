import XCTest
import Foundation
@testable import LumiverbKit

final class RatingModelTests: XCTestCase {

    // MARK: - Rating struct

    func testEmptyRating() {
        let r = Rating.empty
        XCTAssertFalse(r.favorite)
        XCTAssertEqual(r.stars, 0)
        XCTAssertNil(r.color)
    }

    func testRatingEquality() {
        let a = Rating(favorite: true, stars: 3, color: .blue)
        let b = Rating(favorite: true, stars: 3, color: .blue)
        XCTAssertEqual(a, b)
    }

    func testRatingInequality() {
        let a = Rating(favorite: true, stars: 3, color: .blue)
        let b = Rating(favorite: false, stars: 3, color: .blue)
        XCTAssertNotEqual(a, b)
    }

    // MARK: - ColorLabel

    func testColorLabelAllCases() {
        XCTAssertEqual(ColorLabel.allCases.count, 6)
        let rawValues = ColorLabel.allCases.map(\.rawValue)
        XCTAssertTrue(rawValues.contains("red"))
        XCTAssertTrue(rawValues.contains("orange"))
        XCTAssertTrue(rawValues.contains("yellow"))
        XCTAssertTrue(rawValues.contains("green"))
        XCTAssertTrue(rawValues.contains("blue"))
        XCTAssertTrue(rawValues.contains("purple"))
    }

    func testColorLabelRoundTrip() {
        for label in ColorLabel.allCases {
            let encoded = try! JSONEncoder().encode(label)
            let decoded = try! JSONDecoder().decode(ColorLabel.self, from: encoded)
            XCTAssertEqual(label, decoded)
        }
    }

    // MARK: - RatingUpdateBody JSON serialization (three-way color)

    func testUpdateBodyColorUnchanged() throws {
        let body = RatingUpdateBody(favorite: true, stars: 3, color: .unchanged)
        let json = try body.jsonObject()
        XCTAssertEqual(json["favorite"] as? Bool, true)
        XCTAssertEqual(json["stars"] as? Int, 3)
        // "color" key must be absent
        XCTAssertFalse(json.keys.contains("color"))
    }

    func testUpdateBodyColorClear() throws {
        let body = RatingUpdateBody(stars: 4, color: .clear)
        let json = try body.jsonObject()
        XCTAssertEqual(json["stars"] as? Int, 4)
        // "color" key present with NSNull value
        XCTAssertTrue(json.keys.contains("color"))
        XCTAssertTrue(json["color"] is NSNull)
        // "favorite" omitted
        XCTAssertFalse(json.keys.contains("favorite"))
    }

    func testUpdateBodyColorSet() throws {
        let body = RatingUpdateBody(color: .set(.purple))
        let json = try body.jsonObject()
        XCTAssertEqual(json["color"] as? String, "purple")
        XCTAssertFalse(json.keys.contains("favorite"))
        XCTAssertFalse(json.keys.contains("stars"))
    }

    func testUpdateBodyJsonDataProducesValidJson() throws {
        let body = RatingUpdateBody(favorite: false, stars: 0, color: .set(.red))
        let data = try body.jsonData()
        let parsed = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertEqual(parsed["favorite"] as? Bool, false)
        XCTAssertEqual(parsed["stars"] as? Int, 0)
        XCTAssertEqual(parsed["color"] as? String, "red")
    }

    func testUpdateBodyColorClearJsonHasNull() throws {
        let body = RatingUpdateBody(color: .clear)
        let data = try body.jsonData()
        let parsed = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertTrue(parsed["color"] is NSNull)
    }

    // MARK: - BatchRatingUpdateBody

    func testBatchBodyIncludesAssetIds() throws {
        let body = BatchRatingUpdateBody(
            assetIds: ["a1", "a2"],
            favorite: true,
            color: .set(.green)
        )
        let data = try body.jsonData()
        let parsed = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertEqual(parsed["asset_ids"] as? [String], ["a1", "a2"])
        XCTAssertEqual(parsed["favorite"] as? Bool, true)
        XCTAssertEqual(parsed["color"] as? String, "green")
        XCTAssertFalse(parsed.keys.contains("stars"))
    }

    // MARK: - RatingResponse decoding

    func testRatingResponseDecoding() throws {
        let json = """
        {"asset_id": "abc", "favorite": true, "stars": 5, "color": "blue"}
        """.data(using: .utf8)!
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let resp = try decoder.decode(RatingResponse.self, from: json)
        XCTAssertEqual(resp.assetId, "abc")
        XCTAssertTrue(resp.favorite)
        XCTAssertEqual(resp.stars, 5)
        XCTAssertEqual(resp.rating.color, .blue)
    }

    func testRatingResponseNullColor() throws {
        let json = """
        {"asset_id": "abc", "favorite": false, "stars": 0, "color": null}
        """.data(using: .utf8)!
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let resp = try decoder.decode(RatingResponse.self, from: json)
        XCTAssertNil(resp.rating.color)
    }

    // MARK: - RatingLookupResponse decoding

    func testLookupResponseDecoding() throws {
        let json = """
        {
            "ratings": {
                "a1": {"favorite": true, "stars": 3, "color": "red"},
                "a2": {"favorite": false, "stars": 0, "color": null}
            }
        }
        """.data(using: .utf8)!
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let resp = try decoder.decode(RatingLookupResponse.self, from: json)
        XCTAssertEqual(resp.ratings.count, 2)
        XCTAssertEqual(resp.ratings["a1"]?.rating.color, .red)
        XCTAssertEqual(resp.ratings["a1"]?.stars, 3)
        XCTAssertNil(resp.ratings["a2"]?.rating.color)
    }

    // MARK: - ColorChange equality

    func testColorChangeEquality() {
        XCTAssertEqual(ColorChange.unchanged, ColorChange.unchanged)
        XCTAssertEqual(ColorChange.clear, ColorChange.clear)
        XCTAssertEqual(ColorChange.set(.red), ColorChange.set(.red))
        XCTAssertNotEqual(ColorChange.set(.red), ColorChange.set(.blue))
        XCTAssertNotEqual(ColorChange.unchanged, ColorChange.clear)
    }
}
