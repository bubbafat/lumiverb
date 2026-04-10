import XCTest
@testable import LumiverbKit

/// Helper to build a minimal AssetPageItem with just the fields
/// the grouping function cares about.
private func makeAsset(
    id: String,
    takenAt: String? = nil,
    createdAt: String? = nil
) -> AssetPageItem {
    // Decode from JSON so we get a real AssetPageItem without
    // needing to add a public memberwise init.
    let json: [String: Any?] = [
        "asset_id": id,
        "rel_path": "\(id).jpg",
        "file_size": 100,
        "file_mtime": nil,
        "sha256": nil,
        "media_type": "image",
        "width": 100,
        "height": 100,
        "taken_at": takenAt,
        "status": "complete",
        "duration_sec": nil,
        "camera_make": nil,
        "camera_model": nil,
        "iso": nil,
        "aperture": nil,
        "focal_length": nil,
        "focal_length_35mm": nil,
        "lens_model": nil,
        "flash_fired": nil,
        "gps_lat": nil,
        "gps_lon": nil,
        "face_count": nil,
        "created_at": createdAt,
    ]
    let data = try! JSONSerialization.data(withJSONObject: json.compactMapValues { $0 })
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try! decoder.decode(AssetPageItem.self, from: data)
}

final class DateGroupTests: XCTestCase {

    func testEmptyInput() {
        let groups = groupAssetsByDate([])
        XCTAssertTrue(groups.isEmpty)
    }

    func testSingleDate() {
        let assets = [
            makeAsset(id: "a1", takenAt: "2024-06-04T10:00:00Z"),
            makeAsset(id: "a2", takenAt: "2024-06-04T14:00:00Z"),
        ]
        let groups = groupAssetsByDate(assets)
        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(groups[0].dateISO, "2024-06-04")
        XCTAssertEqual(groups[0].assets.count, 2)
        XCTAssertTrue(groups[0].label.contains("June 4, 2024"))
    }

    func testMultipleDatesSortedMostRecentFirst() {
        let assets = [
            makeAsset(id: "a1", takenAt: "2024-01-15T10:00:00Z"),
            makeAsset(id: "a2", takenAt: "2024-06-04T10:00:00Z"),
            makeAsset(id: "a3", takenAt: "2024-03-20T10:00:00Z"),
        ]
        let groups = groupAssetsByDate(assets)
        XCTAssertEqual(groups.count, 3)
        XCTAssertEqual(groups[0].dateISO, "2024-06-04")
        XCTAssertEqual(groups[1].dateISO, "2024-03-20")
        XCTAssertEqual(groups[2].dateISO, "2024-01-15")
    }

    func testNilDatesFallToUnknown() {
        let assets = [
            makeAsset(id: "a1"),
            makeAsset(id: "a2"),
        ]
        let groups = groupAssetsByDate(assets)
        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(groups[0].label, "Unknown date")
        XCTAssertNil(groups[0].dateISO)
        XCTAssertEqual(groups[0].assets.count, 2)
    }

    func testUnknownDateGroupGoesLast() {
        let assets = [
            makeAsset(id: "a1"),
            makeAsset(id: "a2", takenAt: "2024-06-04T10:00:00Z"),
        ]
        let groups = groupAssetsByDate(assets)
        XCTAssertEqual(groups.count, 2)
        XCTAssertEqual(groups[0].dateISO, "2024-06-04")
        XCTAssertEqual(groups[1].label, "Unknown date")
    }

    func testCreatedAtFallback() {
        let assets = [
            makeAsset(id: "a1", createdAt: "2024-08-01T12:00:00Z"),
        ]
        let groups = groupAssetsByDate(assets)
        XCTAssertEqual(groups.count, 1)
        XCTAssertEqual(groups[0].dateISO, "2024-08-01")
    }

    func testTakenAtTakesPriorityOverCreatedAt() {
        let assets = [
            makeAsset(id: "a1", takenAt: "2024-06-04T10:00:00Z", createdAt: "2024-08-01T12:00:00Z"),
        ]
        let groups = groupAssetsByDate(assets)
        XCTAssertEqual(groups[0].dateISO, "2024-06-04")
    }

    func testFractionalSecondsInISO8601() {
        let assets = [
            makeAsset(id: "a1", takenAt: "2024-06-04T10:00:00.123Z"),
        ]
        let groups = groupAssetsByDate(assets)
        XCTAssertEqual(groups[0].dateISO, "2024-06-04")
    }

    func testMixedDatesAndUnknown() {
        let assets = [
            makeAsset(id: "a1", takenAt: "2024-06-04T10:00:00Z"),
            makeAsset(id: "a2"),
            makeAsset(id: "a3", takenAt: "2024-06-04T14:00:00Z"),
            makeAsset(id: "a4", takenAt: "2024-01-01T08:00:00Z"),
            makeAsset(id: "a5"),
        ]
        let groups = groupAssetsByDate(assets)
        XCTAssertEqual(groups.count, 3)
        // June 4 first (most recent)
        XCTAssertEqual(groups[0].dateISO, "2024-06-04")
        XCTAssertEqual(groups[0].assets.count, 2)
        // Jan 1 second
        XCTAssertEqual(groups[1].dateISO, "2024-01-01")
        XCTAssertEqual(groups[1].assets.count, 1)
        // Unknown last
        XCTAssertEqual(groups[2].label, "Unknown date")
        XCTAssertEqual(groups[2].assets.count, 2)
    }

    func testPreservesAssetOrderWithinGroup() {
        let assets = [
            makeAsset(id: "first", takenAt: "2024-06-04T10:00:00Z"),
            makeAsset(id: "second", takenAt: "2024-06-04T14:00:00Z"),
            makeAsset(id: "third", takenAt: "2024-06-04T08:00:00Z"),
        ]
        let groups = groupAssetsByDate(assets)
        XCTAssertEqual(groups[0].assets.map(\.assetId), ["first", "second", "third"])
    }
}
