import SwiftUI
import LumiverbKit

/// Observable state for the People browse panel (Phase 6 M3 of ADR-014).
///
/// Lives parallel to `BrowseState` rather than on it: the People view has
/// its own pagination, its own selection, and its own per-person face
/// list, and none of that should evict the user's library browse state
/// when they pop back. Both states share `appState.client` so they hit
/// the same authenticated API session.
///
/// Two paginated cursors:
/// - `nextPeopleCursor` walks `GET /v1/people` (sorted by face count desc)
/// - `nextFacesCursor` walks `GET /v1/people/{id}/faces` for the open
///   detail view; cleared whenever the selection changes.
@MainActor
final class PeopleState: ObservableObject {
    let appState: AppState

    // MARK: - People list

    @Published var people: [PersonItem] = []
    @Published var isLoadingPeople = false
    @Published var hasMorePeople = true
    @Published var peopleError: String?
    private var nextPeopleCursor: String?

    // MARK: - Person detail

    @Published var selectedPerson: PersonItem?
    @Published var personFaces: [PersonFaceItem] = []
    @Published var isLoadingFaces = false
    @Published var hasMoreFaces = true
    @Published var personFacesError: String?
    private var nextFacesCursor: String?

    init(appState: AppState) {
        self.appState = appState
    }

    var client: APIClient? { appState.client }

    // MARK: - People list loading

    /// First-time load. Idempotent: skips work if the list is already
    /// populated, so revisiting the People tab doesn't re-fetch page 1.
    func loadIfNeeded() async {
        if people.isEmpty, hasMorePeople {
            await loadNextPage()
        }
    }

    func loadNextPage() async {
        guard let client else { return }
        guard !isLoadingPeople, hasMorePeople else { return }

        isLoadingPeople = true
        peopleError = nil
        defer { isLoadingPeople = false }

        do {
            var query: [String: String] = ["limit": "50"]
            if let cursor = nextPeopleCursor { query["after"] = cursor }
            let response: PersonListResponse = try await client.get(
                "/v1/people", query: query
            )
            people.append(contentsOf: response.items)
            nextPeopleCursor = response.nextCursor
            hasMorePeople = response.nextCursor != nil
        } catch {
            self.peopleError = "Failed to load people: \(error)"
            hasMorePeople = false
        }
    }

    // MARK: - Person detail loading

    /// Open the detail view for `person` and start fetching their faces.
    /// Resets per-person state so the previous selection's faces don't
    /// briefly leak into the new view while page 1 loads.
    func selectPerson(_ person: PersonItem) {
        selectedPerson = person
        personFaces = []
        nextFacesCursor = nil
        hasMoreFaces = true
        personFacesError = nil
        Task { await loadNextFacesPage() }
    }

    /// Pop back to the people grid. Called by the navigation back button.
    func clearSelection() {
        selectedPerson = nil
        personFaces = []
        nextFacesCursor = nil
        hasMoreFaces = true
        personFacesError = nil
    }

    func loadNextFacesPage() async {
        guard let client, let personId = selectedPerson?.personId else { return }
        guard !isLoadingFaces, hasMoreFaces else { return }

        isLoadingFaces = true
        personFacesError = nil
        defer { isLoadingFaces = false }

        do {
            var query: [String: String] = ["limit": "50"]
            if let cursor = nextFacesCursor { query["after"] = cursor }
            let response: PersonFacesResponse = try await client.get(
                "/v1/people/\(personId)/faces", query: query
            )
            personFaces.append(contentsOf: response.items)
            nextFacesCursor = response.nextCursor
            hasMoreFaces = response.nextCursor != nil
        } catch {
            self.personFacesError = "Failed to load faces: \(error)"
            hasMoreFaces = false
        }
    }
}
