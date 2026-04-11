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

    // MARK: - Smart collection type + saved_query

    func testSmartCollectionDecoding() throws {
        let json = """
        {
            "collection_id": "col_smart1",
            "name": "Canon Favorites",
            "description": null,
            "cover_asset_id": null,
            "owner_user_id": "user_1",
            "visibility": "private",
            "ownership": "own",
            "sort_order": "manual",
            "type": "smart",
            "saved_query": {
                "filters": {"camera_make": "Canon", "star_min": 3},
                "library_id": "lib_1"
            },
            "asset_count": 42,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00"
        }
        """.data(using: .utf8)!

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let col = try decoder.decode(AssetCollection.self, from: json)

        XCTAssertEqual(col.type, "smart")
        XCTAssertNotNil(col.savedQuery)
        XCTAssertEqual(col.isSmart, true)
    }

    func testStaticCollectionDecoding() throws {
        let json = """
        {
            "collection_id": "col_static1",
            "name": "Manual Collection",
            "description": null,
            "cover_asset_id": null,
            "owner_user_id": "user_1",
            "visibility": "private",
            "ownership": "own",
            "sort_order": "manual",
            "type": "static",
            "saved_query": null,
            "asset_count": 5,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00"
        }
        """.data(using: .utf8)!

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let col = try decoder.decode(AssetCollection.self, from: json)

        XCTAssertEqual(col.type, "static")
        XCTAssertNil(col.savedQuery)
        XCTAssertEqual(col.isSmart, false)
    }

    func testLegacyCollectionMissingTypeDefaultsToStatic() throws {
        // Old server responses may not include type/saved_query
        let json = """
        {
            "collection_id": "col_legacy",
            "name": "Old Collection",
            "description": null,
            "cover_asset_id": null,
            "owner_user_id": null,
            "visibility": "private",
            "ownership": "own",
            "sort_order": "manual",
            "asset_count": 0,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-01-01T00:00:00"
        }
        """.data(using: .utf8)!

        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let col = try decoder.decode(AssetCollection.self, from: json)

        XCTAssertEqual(col.type, "static")
        XCTAssertNil(col.savedQuery)
    }

    func testCreateSmartCollectionRequestEncoding() throws {
        let savedQuery = SavedQueryV2(filters: [
            LeafFilter(type: "camera_make", value: "Canon"),
            LeafFilter(type: "stars", value: "3+"),
            LeafFilter(type: "library", value: "lib_1"),
        ])
        let req = CreateCollectionRequest(
            name: "Canon Favorites",
            type: .smart,
            savedQuery: savedQuery
        )

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(req)
        let dict = try JSONSerialization.jsonObject(with: data) as! [String: Any]

        XCTAssertEqual(dict["type"] as? String, "smart")
        XCTAssertNotNil(dict["saved_query"])
    }

    func testSmartCollectionSavedQueryEncodesFilterAlgebra() throws {
        let savedQuery = SavedQueryV2(filters: [
            LeafFilter(type: "camera_make", value: "Canon"),
            LeafFilter(type: "stars", value: "3+"),
            LeafFilter(type: "favorite", value: "yes"),
            LeafFilter(type: "library", value: "lib_1"),
        ], sort: "taken_at", direction: "desc")
        let req = CreateCollectionRequest(
            name: "Test",
            type: .smart,
            savedQuery: savedQuery
        )

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(req)
        let dict = try JSONSerialization.jsonObject(with: data) as! [String: Any]

        let sq = dict["saved_query"] as! [String: Any]
        let filters = sq["filters"] as! [[String: String]]

        // Verify filter algebra format: [{type: "camera_make", value: "Canon"}, ...]
        XCTAssertEqual(filters.count, 4)
        let types = filters.map { $0["type"]! }
        XCTAssertTrue(types.contains("camera_make"))
        XCTAssertTrue(types.contains("stars"))
        XCTAssertTrue(types.contains("favorite"))
        XCTAssertTrue(types.contains("library"))
    }

    func testBrowseFilterToLeafFilters() throws {
        // Simulates the flow from SaveSmartCollectionSheet:
        // BrowseFilter -> toLeafFilters() -> SavedQueryV2
        var browseFilter = BrowseFilter()
        browseFilter.cameraMake = "Canon"
        browseFilter.starMin = 3
        browseFilter.favorite = true

        let leafFilters = browseFilter.toLeafFilters(libraryId: "lib_1")

        // Verify filter types
        let types = Set(leafFilters.map { $0.type })
        XCTAssertTrue(types.contains("camera_make"))
        XCTAssertTrue(types.contains("stars"))
        XCTAssertTrue(types.contains("favorite"))
        XCTAssertTrue(types.contains("library"))

        // Verify values
        XCTAssertEqual(leafFilters.first(where: { $0.type == "camera_make" })?.value, "Canon")
        XCTAssertEqual(leafFilters.first(where: { $0.type == "stars" })?.value, "3+")
        XCTAssertEqual(leafFilters.first(where: { $0.type == "favorite" })?.value, "yes")

        // Build SavedQueryV2 and encode
        let sq = SavedQueryV2(filters: leafFilters, sort: "taken_at", direction: "desc")
        let req = CreateCollectionRequest(name: "Test", type: .smart, savedQuery: sq)

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(req)
        let json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let savedQuery = json["saved_query"] as! [String: Any]
        let filters = savedQuery["filters"] as! [[String: String]]

        XCTAssertTrue(filters.contains(where: { $0["type"] == "camera_make" && $0["value"] == "Canon" }))
        XCTAssertTrue(filters.contains(where: { $0["type"] == "stars" && $0["value"] == "3+" }))
        XCTAssertTrue(filters.contains(where: { $0["type"] == "favorite" && $0["value"] == "yes" }))
    }
}

// MARK: - BrowseFilter new fields

final class BrowseFilterNewFieldTests: XCTestCase {

    func testHasRatingChiclet() {
        var filter = BrowseFilter()
        filter.hasRating = true

        let active = filter.activeFilters
        XCTAssertTrue(active.contains(where: { $0.id == "hasRating" }))
        XCTAssertEqual(active.first(where: { $0.id == "hasRating" })?.label, "Has rating")
    }

    func testHasRatingFalseChiclet() {
        var filter = BrowseFilter()
        filter.hasRating = false

        let active = filter.activeFilters
        XCTAssertTrue(active.contains(where: { $0.id == "hasRating" }))
        XCTAssertEqual(active.first(where: { $0.id == "hasRating" })?.label, "No rating")
    }

    func testHasColorChiclet() {
        var filter = BrowseFilter()
        filter.hasColor = true

        let active = filter.activeFilters
        XCTAssertTrue(active.contains(where: { $0.id == "hasColor" }))
        XCTAssertEqual(active.first(where: { $0.id == "hasColor" })?.label, "Has color")
    }

    func testHasColorFalseChiclet() {
        var filter = BrowseFilter()
        filter.hasColor = false

        let active = filter.activeFilters
        XCTAssertTrue(active.contains(where: { $0.id == "hasColor" }))
        XCTAssertEqual(active.first(where: { $0.id == "hasColor" })?.label, "No color")
    }

    func testHasRatingQueryParam() {
        var filter = BrowseFilter()
        filter.hasRating = true

        XCTAssertEqual(filter.queryParams["has_rating"], "true")
    }

    func testHasColorQueryParam() {
        var filter = BrowseFilter()
        filter.hasColor = true

        XCTAssertEqual(filter.queryParams["has_color"], "true")
    }

    func testHasActiveFiltersIncludesNewFields() {
        var filter = BrowseFilter()
        XCTAssertFalse(filter.hasActiveFilters)

        filter.hasRating = true
        XCTAssertTrue(filter.hasActiveFilters)

        filter = BrowseFilter()
        filter.hasColor = false
        XCTAssertTrue(filter.hasActiveFilters)
    }

    func testClearAllResetsNewFields() {
        var filter = BrowseFilter()
        filter.hasRating = true
        filter.hasColor = false

        filter.clearAll()

        XCTAssertNil(filter.hasRating)
        XCTAssertNil(filter.hasColor)
    }
}
