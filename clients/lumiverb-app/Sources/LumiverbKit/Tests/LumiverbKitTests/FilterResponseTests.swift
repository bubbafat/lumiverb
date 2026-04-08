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
    }

    // MARK: - TenantFilterDefaultsResponse

    func testDecodesTenantFilterDefaults() throws {
        let json = """
        {
            "includes": [{"pattern": "**/*.jpg"}, {"pattern": "**/*.png"}],
            "excludes": [{"pattern": "**/._*"}]
        }
        """.data(using: .utf8)!

        let response = try decoder.decode(TenantFilterDefaultsResponse.self, from: json)
        XCTAssertEqual(response.includes.count, 2)
        XCTAssertEqual(response.includes[0].pattern, "**/*.jpg")
        XCTAssertEqual(response.includes[1].pattern, "**/*.png")
        XCTAssertEqual(response.excludes.count, 1)
        XCTAssertEqual(response.excludes[0].pattern, "**/._*")
    }

    func testDecodesTenantFilterDefaultsEmpty() throws {
        let json = """
        {"includes": [], "excludes": []}
        """.data(using: .utf8)!

        let response = try decoder.decode(TenantFilterDefaultsResponse.self, from: json)
        XCTAssertEqual(response.includes.count, 0)
        XCTAssertEqual(response.excludes.count, 0)
    }

    func testTenantFilterDefaultsResponseInit() {
        let empty = TenantFilterDefaultsResponse()
        XCTAssertTrue(empty.includes.isEmpty)
        XCTAssertTrue(empty.excludes.isEmpty)

        let items = [FilterItem(pattern: "*.cr3")]
        let withData = TenantFilterDefaultsResponse(
            includes: items,
            excludes: []
        )
        XCTAssertEqual(withData.includes.count, 1)
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

    // MARK: - PathFilter init from filter responses

    func testPathFilterInitFromFilterResponses() {
        let tenant = TenantFilterDefaultsResponse(
            includes: [FilterItem(pattern: "**/*.jpg")],
            excludes: [FilterItem(pattern: "**/._*")]
        )
        let library = LibraryFiltersResponse(
            includes: [FilterItem(pattern: "photos/**")],
            excludes: [FilterItem(pattern: "trash/**")]
        )

        let filter = PathFilter(tenant: tenant, library: library)
        XCTAssertEqual(filter.tenantIncludes, ["**/*.jpg"])
        XCTAssertEqual(filter.tenantExcludes, ["**/._*"])
        XCTAssertEqual(filter.libraryIncludes, ["photos/**"])
        XCTAssertEqual(filter.libraryExcludes, ["trash/**"])
    }

    func testPathFilterFromFilterResponsesFiltersCorrectly() {
        let tenant = TenantFilterDefaultsResponse(
            includes: [],
            excludes: [FilterItem(pattern: "**/._*")]
        )
        let library = LibraryFiltersResponse(
            includes: [],
            excludes: [FilterItem(pattern: "trash/**")]
        )

        let filter = PathFilter(tenant: tenant, library: library)
        XCTAssertTrue(filter.isAllowed("photos/sunset.jpg"))
        XCTAssertFalse(filter.isAllowed("trash/deleted.jpg"))
        XCTAssertFalse(filter.isAllowed("photos/._hidden"))
    }
}

// FilterItem doesn't have a public init in the source, so we add one for tests.
extension FilterItem {
    init(pattern: String) {
        let json = """
        {"pattern": "\(pattern)"}
        """.data(using: .utf8)!
        let decoder = JSONDecoder()
        self = try! decoder.decode(FilterItem.self, from: json)
    }
}
