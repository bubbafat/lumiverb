import XCTest
import Foundation
@testable import LumiverbKit

final class ProjectModelTests: XCTestCase {

    private func decoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }

    private func encode(_ value: some Encodable) throws -> [String: Any] {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let data = try encoder.encode(value)
        return try JSONSerialization.jsonObject(with: data) as! [String: Any]
    }

    func testProjectDecoding() throws {
        // A ProjectItem as GET /v1/projects returns it (fields the app
        // doesn't use, like trashed_asset_count and status, are ignored).
        let json = """
        {
            "project_id": "prj_1",
            "name": "Test",
            "description": "A test",
            "cover_asset_id": "a1",
            "owner_user_id": "user_1",
            "visibility": "shared",
            "ownership": "own",
            "sort_order": "added_at",
            "asset_count": 10,
            "trashed_asset_count": 0,
            "missing_asset_count": 0,
            "library_trashed_asset_count": 0,
            "archived_asset_count": 0,
            "created_at": "2024-01-01T00:00:00",
            "updated_at": "2024-06-01T00:00:00",
            "status": "active",
            "archived_at": null,
            "deleted_at": null
        }
        """.data(using: .utf8)!

        let project = try decoder().decode(Project.self, from: json)

        XCTAssertEqual(project.projectId, "prj_1")
        XCTAssertEqual(project.name, "Test")
        XCTAssertEqual(project.description, "A test")
        XCTAssertEqual(project.coverAssetId, "a1")
        XCTAssertTrue(project.isOwn)
        XCTAssertEqual(project.parsedVisibility, .shared)
        XCTAssertEqual(project.parsedSortOrder, .addedAt)
        XCTAssertEqual(project.assetCount, 10)
        XCTAssertEqual(project.id, "prj_1")
    }

    func testProjectVisibilityAllCases() {
        XCTAssertEqual(ProjectVisibility.allCases.count, 3)
    }

    func testProjectSortOrderAllCases() {
        XCTAssertEqual(ProjectSortOrder.allCases.count, 3)
    }

    func testProjectAssetDecoding() throws {
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

        let asset = try decoder().decode(ProjectAsset.self, from: json)

        XCTAssertEqual(asset.assetId, "a1")
        XCTAssertFalse(asset.isVideo)
        XCTAssertEqual(asset.aspectRatio, 1920.0 / 1080.0, accuracy: 0.001)
        XCTAssertEqual(asset.cameraMake, "Canon")
    }

    func testProjectAssetVideoDetection() throws {
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

        let asset = try decoder().decode(ProjectAsset.self, from: json)

        XCTAssertTrue(asset.isVideo)
        XCTAssertEqual(asset.durationSec, 30.5)
    }

    func testProjectAssetsResponseDecoding() throws {
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

        let response = try decoder().decode(ProjectAssetsResponse.self, from: json)

        XCTAssertEqual(response.items.count, 1)
        XCTAssertEqual(response.nextCursor, "abc123")
    }

    func testCreateProjectRequestEncoding() throws {
        let req = CreateProjectRequest(
            name: "My Photos",
            description: "Best shots",
            sortOrder: .takenAt,
            visibility: .shared,
            assetIds: ["a1", "a2"]
        )

        let dict = try encode(req)

        XCTAssertEqual(dict["name"] as? String, "My Photos")
        XCTAssertEqual(dict["description"] as? String, "Best shots")
        XCTAssertEqual(dict["sort_order"] as? String, "taken_at")
        XCTAssertEqual(dict["visibility"] as? String, "shared")
        XCTAssertEqual((dict["asset_ids"] as? [String])?.count, 2)
        XCTAssertNil(dict["from_search"])
    }

    // MARK: - A search saved as a project (from_search)

    func testFromSearchEncodesFilterAlgebra() throws {
        // POST /v1/projects reads from_search strictly (filter_registry.from_json):
        // only filters/sort/direction, each filter only type/value.
        let search = SavedQueryV2(filters: [
            LeafFilter(type: "camera_make", value: "Canon"),
            LeafFilter(type: "stars", value: "3+"),
            LeafFilter(type: "favorite", value: "yes"),
            LeafFilter(type: "library", value: "lib_1"),
        ], sort: "taken_at", direction: "desc")
        let req = CreateProjectRequest(name: "Test", fromSearch: search)

        let dict = try encode(req)

        XCTAssertNil(dict["asset_ids"])
        XCTAssertNil(dict["type"])
        XCTAssertNil(dict["saved_query"])
        let fromSearch = dict["from_search"] as! [String: Any]
        XCTAssertEqual(Set(fromSearch.keys), ["filters", "sort", "direction"])
        let filters = fromSearch["filters"] as! [[String: String]]
        XCTAssertEqual(filters.count, 4)
        XCTAssertTrue(filters.allSatisfy { Set($0.keys) == ["type", "value"] })
        XCTAssertEqual(filters.first, ["type": "camera_make", "value": "Canon"])
        XCTAssertEqual(fromSearch["sort"] as? String, "taken_at")
        XCTAssertEqual(fromSearch["direction"] as? String, "desc")
    }

    func testFromSearchOmitsMissingSort() throws {
        let req = CreateProjectRequest(
            name: "Test",
            fromSearch: SavedQueryV2(filters: [LeafFilter(type: "media", value: "video")])
        )

        let fromSearch = try encode(req)["from_search"] as! [String: Any]

        XCTAssertEqual(Set(fromSearch.keys), ["filters"])
    }

    func testBrowseFilterToLeafFilters() throws {
        // Simulates the flow from SaveSmartCollectionSheet:
        // BrowseFilter -> toLeafFilters() -> SavedQueryV2 -> from_search
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

        // Build the search and encode it as from_search
        let search = SavedQueryV2(filters: leafFilters, sort: "taken_at", direction: "desc")
        let req = CreateProjectRequest(name: "Test", fromSearch: search)

        let fromSearch = try encode(req)["from_search"] as! [String: Any]
        let filters = fromSearch["filters"] as! [[String: String]]

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
