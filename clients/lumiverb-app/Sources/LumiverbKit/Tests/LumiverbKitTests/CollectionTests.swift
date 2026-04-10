import XCTest
import Foundation
@testable import LumiverbKit

final class CollectionModelTests: XCTestCase {

    func testAssetCollectionDecoding() throws {
        let json = """
        {
            "collection_id": "col_1",
            "name": "Test",
            "description": "A test",
            "cover_asset_id": "a1",
            "owner_user_id": "user_1",
            "visibility": "shared",
            "ownership": "own",
            "sort_order": "added_at",
            "asset_count": 10,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-06-01T00:00:00"
        }
        """.data(using: .utf8)!

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let col = try decoder.decode(AssetCollection.self, from: json)

        XCTAssertEqual(col.collectionId, "col_1")
        XCTAssertEqual(col.name, "Test")
        XCTAssertEqual(col.description, "A test")
        XCTAssertEqual(col.coverAssetId, "a1")
        XCTAssertTrue(col.isOwn)
        XCTAssertEqual(col.parsedVisibility, .shared)
        XCTAssertEqual(col.parsedSortOrder, .addedAt)
        XCTAssertEqual(col.assetCount, 10)
        XCTAssertEqual(col.id, "col_1")
    }

    func testCollectionVisibilityAllCases() {
        XCTAssertEqual(CollectionVisibility.allCases.count, 3)
    }

    func testCollectionSortOrderAllCases() {
        XCTAssertEqual(CollectionSortOrder.allCases.count, 3)
    }

    func testCollectionAssetDecoding() throws {
        let json = """
        {
            "asset_id": "a1",
            "rel_path": "photos/test.jpg",
            "file_size": 5000,
            "media_type": "image",
            "width": 1920,
            "height": 1080,
            "taken_at": "2024-06-01T12:00:00",
            "status": "complete",
            "duration_sec": null,
            "camera_make": "Canon",
            "camera_model": "R5"
        }
        """.data(using: .utf8)!

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let asset = try decoder.decode(CollectionAsset.self, from: json)

        XCTAssertEqual(asset.assetId, "a1")
        XCTAssertFalse(asset.isVideo)
        XCTAssertEqual(asset.aspectRatio, 1920.0 / 1080.0, accuracy: 0.001)
        XCTAssertEqual(asset.cameraMake, "Canon")
    }

    func testCollectionAssetVideoDetection() throws {
        let json = """
        {
            "asset_id": "v1",
            "rel_path": "videos/clip.mp4",
            "file_size": 50000,
            "media_type": "video",
            "width": 3840,
            "height": 2160,
            "taken_at": null,
            "status": "complete",
            "duration_sec": 30.5,
            "camera_make": null,
            "camera_model": null
        }
        """.data(using: .utf8)!

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let asset = try decoder.decode(CollectionAsset.self, from: json)

        XCTAssertTrue(asset.isVideo)
        XCTAssertEqual(asset.durationSec, 30.5)
    }

    func testCollectionAssetsResponseDecoding() throws {
        let json = """
        {
            "items": [{
                "asset_id": "a1", "rel_path": "p.jpg", "file_size": 100,
                "media_type": "image", "width": 100, "height": 100,
                "taken_at": null, "status": "complete", "duration_sec": null,
                "camera_make": null, "camera_model": null
            }],
            "next_cursor": "abc123"
        }
        """.data(using: .utf8)!

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let response = try decoder.decode(CollectionAssetsResponse.self, from: json)

        XCTAssertEqual(response.items.count, 1)
        XCTAssertEqual(response.nextCursor, "abc123")
    }

    func testCreateCollectionRequestEncoding() throws {
        let req = CreateCollectionRequest(
            name: "My Photos",
            description: "Best shots",
            sortOrder: .takenAt,
            visibility: .shared,
            assetIds: ["a1", "a2"]
        )

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(req)
        let dict = try JSONSerialization.jsonObject(with: data) as! [String: Any]

        XCTAssertEqual(dict["name"] as? String, "My Photos")
        XCTAssertEqual(dict["description"] as? String, "Best shots")
        XCTAssertEqual(dict["sort_order"] as? String, "taken_at")
        XCTAssertEqual(dict["visibility"] as? String, "shared")
        XCTAssertEqual((dict["asset_ids"] as? [String])?.count, 2)
    }
}
