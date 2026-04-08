import XCTest
@testable import LumiverbKit

final class BrowseFilterTests: XCTestCase {

    // MARK: - Default state

    func testDefaultFilterHasNoActiveFilters() {
        let filter = BrowseFilter()
        XCTAssertFalse(filter.hasActiveFilters)
    }

    func testDefaultQueryParamsContainSortOnly() {
        let filter = BrowseFilter()
        let params = filter.queryParams
        XCTAssertEqual(params["sort"], "taken_at")
        XCTAssertEqual(params["dir"], "desc")
        XCTAssertEqual(params.count, 2, "Default should only have sort and dir")
    }

    // MARK: - hasActiveFilters detects each field

    func testHasActiveFiltersMediaType() {
        var f = BrowseFilter()
        f.mediaType = "image"
        XCTAssertTrue(f.hasActiveFilters)
    }

    func testHasActiveFiltersCameraMake() {
        var f = BrowseFilter()
        f.cameraMake = "Canon"
        XCTAssertTrue(f.hasActiveFilters)
    }

    func testHasActiveFiltersCameraModel() {
        var f = BrowseFilter()
        f.cameraModel = "EOS R5"
        XCTAssertTrue(f.hasActiveFilters)
    }

    func testHasActiveFiltersLensModel() {
        var f = BrowseFilter()
        f.lensModel = "RF 50mm"
        XCTAssertTrue(f.hasActiveFilters)
    }

    func testHasActiveFiltersISORange() {
        var f = BrowseFilter()
        f.isoMin = 100
        XCTAssertTrue(f.hasActiveFilters)

        var f2 = BrowseFilter()
        f2.isoMax = 3200
        XCTAssertTrue(f2.hasActiveFilters)
    }

    func testHasActiveFiltersApertureRange() {
        var f = BrowseFilter()
        f.apertureMin = 1.4
        XCTAssertTrue(f.hasActiveFilters)

        var f2 = BrowseFilter()
        f2.apertureMax = 16.0
        XCTAssertTrue(f2.hasActiveFilters)
    }

    func testHasActiveFiltersFocalLengthRange() {
        var f = BrowseFilter()
        f.focalLengthMin = 24.0
        XCTAssertTrue(f.hasActiveFilters)

        var f2 = BrowseFilter()
        f2.focalLengthMax = 200.0
        XCTAssertTrue(f2.hasActiveFilters)
    }

    func testHasActiveFiltersGps() {
        var f = BrowseFilter()
        f.hasGps = true
        XCTAssertTrue(f.hasActiveFilters)

        var f2 = BrowseFilter()
        f2.hasGps = false
        XCTAssertTrue(f2.hasActiveFilters, "hasGps=false is still an active filter")
    }

    func testHasActiveFiltersFaces() {
        var f = BrowseFilter()
        f.hasFaces = true
        XCTAssertTrue(f.hasActiveFilters)

        var f2 = BrowseFilter()
        f2.hasFaces = false
        XCTAssertTrue(f2.hasActiveFilters, "hasFaces=false is still an active filter")
    }

    func testHasActiveFiltersPerson() {
        var f = BrowseFilter()
        f.personId = "person_123"
        XCTAssertTrue(f.hasActiveFilters)
    }

    func testHasActiveFiltersFavorite() {
        var f = BrowseFilter()
        f.favorite = true
        XCTAssertTrue(f.hasActiveFilters)
    }

    func testHasActiveFiltersStarRange() {
        var f = BrowseFilter()
        f.starMin = 3
        XCTAssertTrue(f.hasActiveFilters)

        var f2 = BrowseFilter()
        f2.starMax = 5
        XCTAssertTrue(f2.hasActiveFilters)
    }

    func testHasActiveFiltersColor() {
        var f = BrowseFilter()
        f.color = "red"
        XCTAssertTrue(f.hasActiveFilters)
    }

    func testHasActiveFiltersDateRange() {
        var f = BrowseFilter()
        f.dateFrom = "2024-01-01"
        XCTAssertTrue(f.hasActiveFilters)

        var f2 = BrowseFilter()
        f2.dateTo = "2024-12-31"
        XCTAssertTrue(f2.hasActiveFilters)
    }

    // MARK: - queryParams builds correct API parameters

    func testQueryParamsMediaType() {
        var f = BrowseFilter()
        f.mediaType = "video"
        XCTAssertEqual(f.queryParams["media_type"], "video")
    }

    func testQueryParamsCameraFields() {
        var f = BrowseFilter()
        f.cameraMake = "Sony"
        f.cameraModel = "A7IV"
        f.lensModel = "FE 24-70mm"
        let p = f.queryParams
        XCTAssertEqual(p["camera_make"], "Sony")
        XCTAssertEqual(p["camera_model"], "A7IV")
        XCTAssertEqual(p["lens_model"], "FE 24-70mm")
    }

    func testQueryParamsISORange() {
        var f = BrowseFilter()
        f.isoMin = 100
        f.isoMax = 6400
        let p = f.queryParams
        XCTAssertEqual(p["iso_min"], "100")
        XCTAssertEqual(p["iso_max"], "6400")
    }

    func testQueryParamsApertureRange() {
        var f = BrowseFilter()
        f.apertureMin = 1.4
        f.apertureMax = 22.0
        let p = f.queryParams
        XCTAssertEqual(p["aperture_min"], "1.4")
        XCTAssertEqual(p["aperture_max"], "22.0")
    }

    func testQueryParamsFocalLengthRange() {
        var f = BrowseFilter()
        f.focalLengthMin = 14.0
        f.focalLengthMax = 600.0
        let p = f.queryParams
        XCTAssertEqual(p["focal_length_min"], "14.0")
        XCTAssertEqual(p["focal_length_max"], "600.0")
    }

    func testQueryParamsPersonId() {
        var f = BrowseFilter()
        f.personId = "person_abc"
        XCTAssertEqual(f.queryParams["person_id"], "person_abc")
    }

    func testQueryParamsStarRange() {
        var f = BrowseFilter()
        f.starMin = 3
        f.starMax = 5
        let p = f.queryParams
        XCTAssertEqual(p["star_min"], "3")
        XCTAssertEqual(p["star_max"], "5")
    }

    func testQueryParamsColor() {
        var f = BrowseFilter()
        f.color = "green"
        XCTAssertEqual(f.queryParams["color"], "green")
    }

    func testQueryParamsDateRange() {
        var f = BrowseFilter()
        f.dateFrom = "2024-06-01"
        f.dateTo = "2024-06-30"
        let p = f.queryParams
        XCTAssertEqual(p["date_from"], "2024-06-01")
        XCTAssertEqual(p["date_to"], "2024-06-30")
    }

    // MARK: - Boolean filter params emit for both true AND false
    // The server supports has_faces=false (assets with no faces),
    // has_gps=false, and favorite=false per the API docs.

    func testQueryParamsHasGpsTrueEmitsParam() {
        var f = BrowseFilter()
        f.hasGps = true
        XCTAssertEqual(f.queryParams["has_gps"], "true")
    }

    func testQueryParamsHasGpsFalseEmitsParam() {
        var f = BrowseFilter()
        f.hasGps = false
        XCTAssertNotNil(f.queryParams["has_gps"],
            "has_gps=false should emit a query param — server uses it to filter assets without GPS")
    }

    func testQueryParamsHasFacesTrueEmitsParam() {
        var f = BrowseFilter()
        f.hasFaces = true
        XCTAssertEqual(f.queryParams["has_faces"], "true")
    }

    func testQueryParamsHasFacesFalseEmitsParam() {
        var f = BrowseFilter()
        f.hasFaces = false
        XCTAssertNotNil(f.queryParams["has_faces"],
            "has_faces=false should emit a query param — server uses it to filter assets without faces")
    }

    func testQueryParamsFavoriteTrueEmitsParam() {
        var f = BrowseFilter()
        f.favorite = true
        XCTAssertEqual(f.queryParams["favorite"], "true")
    }

    func testQueryParamsFavoriteFalseEmitsParam() {
        var f = BrowseFilter()
        f.favorite = false
        XCTAssertNotNil(f.queryParams["favorite"],
            "favorite=false should emit a query param — server uses it to filter non-favorited assets")
    }

    // MARK: - Custom sort

    func testCustomSortAppearsInQueryParams() {
        var f = BrowseFilter()
        f.sortField = "file_name"
        f.sortDirection = "asc"
        let p = f.queryParams
        XCTAssertEqual(p["sort"], "file_name")
        XCTAssertEqual(p["dir"], "asc")
    }

    // MARK: - Equatable

    func testEquality() {
        var a = BrowseFilter()
        var b = BrowseFilter()
        XCTAssertEqual(a, b)

        a.mediaType = "image"
        XCTAssertNotEqual(a, b)

        b.mediaType = "image"
        XCTAssertEqual(a, b)
    }

    // MARK: - All filters together

    func testQueryParamsAllFieldsSet() {
        var f = BrowseFilter()
        f.sortField = "file_name"
        f.sortDirection = "asc"
        f.mediaType = "image"
        f.cameraMake = "Nikon"
        f.cameraModel = "Z9"
        f.lensModel = "70-200mm"
        f.isoMin = 64
        f.isoMax = 12800
        f.apertureMin = 2.8
        f.apertureMax = 11.0
        f.focalLengthMin = 70.0
        f.focalLengthMax = 200.0
        f.hasGps = true
        f.hasFaces = true
        f.personId = "p_1"
        f.favorite = true
        f.starMin = 4
        f.starMax = 5
        f.color = "blue"
        f.dateFrom = "2024-01-01"
        f.dateTo = "2024-12-31"

        let p = f.queryParams
        // sort + dir + 19 filter params = 21 total
        XCTAssertEqual(p["sort"], "file_name")
        XCTAssertEqual(p["dir"], "asc")
        XCTAssertEqual(p["media_type"], "image")
        XCTAssertEqual(p["camera_make"], "Nikon")
        XCTAssertEqual(p["camera_model"], "Z9")
        XCTAssertEqual(p["lens_model"], "70-200mm")
        XCTAssertEqual(p["iso_min"], "64")
        XCTAssertEqual(p["iso_max"], "12800")
        XCTAssertEqual(p["aperture_min"], "2.8")
        XCTAssertEqual(p["aperture_max"], "11.0")
        XCTAssertEqual(p["focal_length_min"], "70.0")
        XCTAssertEqual(p["focal_length_max"], "200.0")
        XCTAssertEqual(p["has_gps"], "true")
        XCTAssertEqual(p["has_faces"], "true")
        XCTAssertEqual(p["person_id"], "p_1")
        XCTAssertEqual(p["favorite"], "true")
        XCTAssertEqual(p["star_min"], "4")
        XCTAssertEqual(p["star_max"], "5")
        XCTAssertEqual(p["color"], "blue")
        XCTAssertEqual(p["date_from"], "2024-01-01")
        XCTAssertEqual(p["date_to"], "2024-12-31")
    }
}
