import XCTest
@testable import LumiverbKit

final class FilterResponseTests: XCTestCase {

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()

    // MARK: - FilterItem

    func testDecodesFilterItem() throws {
        let json = """
        {"pattern": "**/*.jpg"}
        """.data(using: .utf8)!

        let item = try decoder.decode(FilterItem.self, from: json)
        XCTAssertEqual(item.pattern, "**/*.jpg")
        XCTAssertNil(item.filterId)
        XCTAssertNil(item.defaultId)
        XCTAssertNil(item.createdAt)
    }

    func testDecodesFilterItemFromLibraryFilterShape() throws {
        // GET /v1/libraries/{id}/filters returns full filter rows with
        // filter_id + created_at. The settings UI needs these to delete
        // individual rows, so make sure the richer server shape decodes.
        let json = """
        {"filter_id": "fil_abc", "pattern": "**/Proxy/**", "created_at": "2025-12-05T12:00:00+00:00"}
        """.data(using: .utf8)!

        let item = try decoder.decode(FilterItem.self, from: json)
        XCTAssertEqual(item.pattern, "**/Proxy/**")
        XCTAssertEqual(item.filterId, "fil_abc")
        XCTAssertEqual(item.createdAt, "2025-12-05T12:00:00+00:00")
        XCTAssertNil(item.defaultId)
    }

    func testDecodesFilterItemFromTenantDefaultShape() throws {
        // GET /v1/tenant/filter-defaults uses `default_id` instead of
        // `filter_id` — both fields are optional on FilterItem so one
        // struct covers both responses.
        let json = """
        {"default_id": "def_xyz", "pattern": "**/._*", "created_at": "2024-01-01T00:00:00+00:00"}
        """.data(using: .utf8)!

        let item = try decoder.decode(FilterItem.self, from: json)
        XCTAssertEqual(item.pattern, "**/._*")
        XCTAssertEqual(item.defaultId, "def_xyz")
        XCTAssertEqual(item.createdAt, "2024-01-01T00:00:00+00:00")
        XCTAssertNil(item.filterId)
    }

    // MARK: - LibraryFiltersResponse

    func testDecodesLibraryFiltersResponse() throws {
        let json = """
        {
            "includes": [{"pattern": "photos/**"}],
            "excludes": [{"pattern": "trash/**"}, {"pattern": ".DS_Store"}]
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(LibraryFiltersResponse.self, from: json)
        XCTAssertEqual(response.includes.count, 1)
        XCTAssertEqual(response.includes[0].pattern, "photos/**")
        XCTAssertEqual(response.excludes.count, 2)
    }

    func testLibraryFiltersResponseInit() {
        let empty = LibraryFiltersResponse()
        XCTAssertTrue(empty.includes.isEmpty)
        XCTAssertTrue(empty.excludes.isEmpty)
    }
}

