import XCTest
import Combine
@testable import LumiverbKit

/// Tests for the search + filter stacking model:
/// - New text search clears all filters
/// - Metadata clicks from lightbox stack as refinements
/// - Tag clicks stack as refinements (don't replace search)
/// - Filters persist across lightbox open/close
/// - Mode is preserved when applying metadata filters
@MainActor
final class SearchFilterStackingTests: XCTestCase {

    // MARK: - Helpers

    /// Minimal BrowseAppContext stub for testing.
    private class StubAppContext: BrowseAppContext {
        var client: APIClient? = nil
        var libraries: [Library] = []
        var whisperEnabled: Bool = false
        var whisperEnabledPublisher: AnyPublisher<Bool, Never> {
            Just(false).eraseToAnyPublisher()
        }
        var resolvedVisionApiUrl: String = ""
        var resolvedVisionApiKey: String = ""
        var resolvedVisionModelId: String = ""
        var whisperModelSize: String = ""
        var whisperLanguage: String = ""
        var whisperBinaryPath: String = ""
        var embeddingModelId: String = ""
        var embeddingModelVersion: String = ""
    }

    private func makeBrowseState() -> BrowseState {
        BrowseState(appContext: StubAppContext())
    }

    // MARK: - performSearch clears filters

    func testPerformSearchPreservesFilters() async {
        let state = makeBrowseState()
        state.filters.cameraMake = "Canon"
        state.filters.isoMin = 800
        state.filters.isoMax = 800
        state.filters.tag = "sunset"
        state.selectedPath = "2024/Travel"

        state.searchQuery = "Brian"
        await state.performSearch()

        // Filters stack — performSearch does NOT clear them.
        // Only the chiclet bar's "Clear all" clears filters.
        XCTAssertEqual(state.filters.cameraMake, "Canon")
        XCTAssertEqual(state.filters.isoMin, 800)
        XCTAssertEqual(state.filters.tag, "sunset")
        XCTAssertEqual(state.selectedPath, "2024/Travel")
    }

    // MARK: - applyMetadataFilter stacks

    func testApplyMetadataFilterStacks() {
        let state = makeBrowseState()
        state.filters.cameraMake = "Canon"
        state.filters.cameraModel = "EOS R5"

        state.applyMetadataFilter { f in
            f.isoMin = 800
            f.isoMax = 800
        }

        // Camera filter should still be there
        XCTAssertEqual(state.filters.cameraMake, "Canon")
        XCTAssertEqual(state.filters.cameraModel, "EOS R5")
        // ISO filter should be added
        XCTAssertEqual(state.filters.isoMin, 800)
        XCTAssertEqual(state.filters.isoMax, 800)
    }

    func testApplyMetadataFilterStacksMultiple() {
        let state = makeBrowseState()

        state.applyMetadataFilter { f in f.cameraMake = "Canon" }
        state.applyMetadataFilter { f in f.isoMin = 400; f.isoMax = 400 }
        state.applyMetadataFilter { f in f.tag = "sunset" }

        XCTAssertEqual(state.filters.cameraMake, "Canon")
        XCTAssertEqual(state.filters.isoMin, 400)
        XCTAssertEqual(state.filters.tag, "sunset")
    }

    // MARK: - Mode preservation

    func testApplyMetadataFilterPreservesSearchMode() {
        let state = makeBrowseState()
        state.mode = .search
        state.searchQuery = "Sue"

        state.applyMetadataFilter { f in f.tag = "sunglasses" }

        XCTAssertEqual(state.mode, .search)
        XCTAssertEqual(state.searchQuery, "Sue")
        XCTAssertEqual(state.filters.tag, "sunglasses")
    }

    func testApplyMetadataFilterPreservesLibraryMode() {
        let state = makeBrowseState()
        state.mode = .library

        state.applyMetadataFilter { f in f.cameraMake = "Sony" }

        XCTAssertEqual(state.mode, .library)
        XCTAssertEqual(state.filters.cameraMake, "Sony")
    }

    func testApplyMetadataFilterPreservesSimilarMode() {
        let state = makeBrowseState()
        state.mode = .similar("asset_123")

        state.applyMetadataFilter { f in f.mediaType = "video" }

        XCTAssertEqual(state.mode, .similar("asset_123"))
        XCTAssertEqual(state.filters.mediaType, "video")
    }

    // MARK: - Tag as filter (not search replacement)

    func testTagFilterDoesNotReplaceSearchQuery() {
        let state = makeBrowseState()
        state.mode = .search
        state.searchQuery = "Sue"

        state.applyMetadataFilter { f in f.tag = "sunglasses" }

        XCTAssertEqual(state.searchQuery, "Sue")
        XCTAssertEqual(state.filters.tag, "sunglasses")
        XCTAssertEqual(state.mode, .search)
    }

    // MARK: - Path filter stacking

    func testPathFilterPreservedByMetadataFilter() {
        let state = makeBrowseState()
        state.selectedPath = "2024/Travel"

        state.applyMetadataFilter { f in f.isoMin = 100 }

        XCTAssertEqual(state.selectedPath, "2024/Travel")
        XCTAssertEqual(state.filters.isoMin, 100)
    }

    // MARK: - clearAll

    func testClearAllResetsEverythingExceptSort() {
        let state = makeBrowseState()
        state.filters.sortField = "created_at"
        state.filters.sortDirection = "asc"
        state.filters.cameraMake = "Canon"
        state.filters.isoMin = 800
        state.filters.tag = "sunset"

        state.filters.clearAll()

        XCTAssertNil(state.filters.cameraMake)
        XCTAssertNil(state.filters.isoMin)
        XCTAssertNil(state.filters.tag)
        // Sort preserved
        XCTAssertEqual(state.filters.sortField, "created_at")
        XCTAssertEqual(state.filters.sortDirection, "asc")
    }

    // MARK: - Active filters enumeration

    func testActiveFiltersIncludesTag() {
        var filter = BrowseFilter()
        filter.tag = "sunset"

        let active = filter.activeFilters
        XCTAssertTrue(active.contains(where: { $0.id == "tag" }))
        XCTAssertEqual(active.first(where: { $0.id == "tag" })?.label, "Tag: sunset")
    }

    func testActiveFiltersIncludesCamera() {
        var filter = BrowseFilter()
        filter.cameraMake = "Canon"
        filter.cameraModel = "EOS R5"

        let active = filter.activeFilters
        XCTAssertTrue(active.contains(where: { $0.id == "camera" }))
        XCTAssertEqual(active.first(where: { $0.id == "camera" })?.label, "Canon EOS R5")
    }

    func testActiveFiltersClearRemovesOnlyThatFilter() {
        var filter = BrowseFilter()
        filter.cameraMake = "Canon"
        filter.isoMin = 800
        filter.isoMax = 800
        filter.tag = "sunset"

        // Clear the ISO filter
        let isoFilter = filter.activeFilters.first(where: { $0.id == "iso" })!
        isoFilter.clear(&filter)

        XCTAssertNil(filter.isoMin)
        XCTAssertNil(filter.isoMax)
        // Others preserved
        XCTAssertEqual(filter.cameraMake, "Canon")
        XCTAssertEqual(filter.tag, "sunset")
    }

    // MARK: - Person filter + tag stacking (exact user scenario)

    func testPersonFilterThenTagPreservesPerson() {
        let state = makeBrowseState()

        // Step 1: User picks Susan from person dropdown
        let personJSON = """
        {"person_id":"person_1","display_name":"Susan","face_count":50,"representative_face_id":"face_1","representative_asset_id":null,"confirmation_count":0}
        """.data(using: .utf8)!
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let person = try! decoder.decode(PersonItem.self, from: personJSON)
        state.filterByPerson(person)

        XCTAssertEqual(state.mode, .library)
        XCTAssertEqual(state.filters.personId, "person_1")
        XCTAssertEqual(state.filters.personDisplayName, "Susan")

        // Step 2: User clicks an image (lightbox opens)
        state.selectedAssetId = "asset_1"

        // Step 3: User clicks "sunglasses" tag in lightbox
        state.applyMetadataFilter { f in
            f.tag = "sunglasses"
        }

        // Person filter must be preserved
        XCTAssertEqual(state.filters.personId, "person_1", "Person filter lost after tag click")
        XCTAssertEqual(state.filters.personDisplayName, "Susan", "Person display name lost after tag click")
        // Tag must be added
        XCTAssertEqual(state.filters.tag, "sunglasses", "Tag not set")
        // Lightbox must be closed
        XCTAssertNil(state.selectedAssetId)
        // Must stay in library mode (person filter is a browse filter)
        XCTAssertEqual(state.mode, .library)
    }

    func testPersonFilterChicletExists() {
        var filter = BrowseFilter()
        filter.personId = "person_1"
        filter.personDisplayName = "Susan"

        let active = filter.activeFilters
        XCTAssertTrue(active.contains(where: { $0.id == "person" }), "Person chiclet missing")
        XCTAssertEqual(active.first(where: { $0.id == "person" })?.label, "Susan")
    }

    // MARK: - Person suggestion + onSubmit race

    func testPerformSearchNeverClearsFilters() async {
        let state = makeBrowseState()
        state.filters.personId = "person_1"
        state.filters.personDisplayName = "Susan"
        state.filters.isoMin = 400

        // With or without a query, filters are preserved
        state.searchQuery = "Brian"
        await state.performSearch()

        XCTAssertEqual(state.filters.personId, "person_1")
        XCTAssertEqual(state.filters.personDisplayName, "Susan")
        XCTAssertEqual(state.filters.isoMin, 400)
    }

    // MARK: - Lightbox close preserves filters

    func testCloseLightboxPreservesFilters() {
        let state = makeBrowseState()
        state.filters.cameraMake = "Sony"
        state.filters.tag = "portrait"
        state.selectedAssetId = "asset_1"

        state.closeLightbox()

        XCTAssertNil(state.selectedAssetId)
        XCTAssertEqual(state.filters.cameraMake, "Sony")
        XCTAssertEqual(state.filters.tag, "portrait")
    }
}
