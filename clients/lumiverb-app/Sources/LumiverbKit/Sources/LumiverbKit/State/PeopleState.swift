import SwiftUI

/// Whether the People view is showing active people (`/v1/people`) or
/// dismissed people (`/v1/people/dismissed`). Switching modes resets the
/// list cursor and refetches from page 1. Phase 6 M6 of ADR-014.
enum PeopleListMode: Equatable {
    case active
    case dismissed
}

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
public final class PeopleState: ObservableObject {

    // MARK: - People list

    @Published public var people: [PersonItem] = []
    @Published public var isLoadingPeople = false
    @Published public var hasMorePeople = true
    @Published public var peopleError: String?
    private var nextPeopleCursor: String?

    /// Active vs dismissed list source. Mutated by the segmented control
    /// in `PeopleView`; flipping it via `setMode(_:)` resets and reloads.
    @Published private(set) var mode: PeopleListMode = .active

    // MARK: - Mutation state

    /// Person ids currently undergoing rename / delete / merge / undismiss
    /// — used by the detail view to show a spinner and disable buttons.
    @Published public var pendingMutations: Set<String> = []

    @Published public var mutationError: String?

    /// Set after a successful delete or merge so the detail view can pop
    /// itself back to the grid. Cleared by the detail view on consume.
    @Published public var dismissDetailRequest: Bool = false

    // MARK: - Merge picker (typeahead)

    @Published public var mergeSearchQuery: String = ""
    @Published public var mergeSearchResults: [PersonItem] = []
    @Published public var isSearchingMerge: Bool = false
    private var mergeSearchTask: Task<Void, Never>?

    // MARK: - Person detail

    @Published public var selectedPerson: PersonItem?
    @Published public var personFaces: [PersonFaceItem] = []
    @Published public var isLoadingFaces = false
    @Published public var hasMoreFaces = true
    @Published public var personFacesError: String?
    private var nextFacesCursor: String?

    public init(client: APIClient?) {
        self.client = client
    }

    public let client: APIClient?

    // MARK: - People list loading

    /// First-time load. Idempotent: skips work if the list is already
    /// populated, so revisiting the People tab doesn't re-fetch page 1.
    public func loadIfNeeded() async {
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

        let path = mode == .active ? "/v1/people" : "/v1/people/dismissed"

        do {
            var query: [String: String] = ["limit": "50"]
            if let cursor = nextPeopleCursor { query["after"] = cursor }
            let response: PersonListResponse = try await client.get(
                path, query: query
            )
            people.append(contentsOf: response.items)
            nextPeopleCursor = response.nextCursor
            hasMorePeople = response.nextCursor != nil
        } catch {
            // Cancellation is not a real failure — happens when the view
            // is briefly torn down (e.g., section toggle racing with the
            // initial fetch). Leave hasMorePeople true so the next visit
            // can retry. Anything else is a real error worth surfacing.
            if Self.isCancellation(error) { return }
            self.peopleError = "Failed to load people: \(error)"
            hasMorePeople = false
        }
    }

    /// True if `error` is a Swift task cancellation or a URLSession
    /// cancelled error (which the APIClient flattens into
    /// `APIError.networkError("cancelled")`).
    private static func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        if let urlErr = error as? URLError, urlErr.code == .cancelled { return true }
        if case APIError.networkError(let msg) = error,
           msg.lowercased().contains("cancel") {
            return true
        }
        return false
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
            if Self.isCancellation(error) { return }
            self.personFacesError = "Failed to load faces: \(error)"
            hasMoreFaces = false
        }
    }

    // MARK: - Mode switching

    /// Flip between active and dismissed lists. Resets pagination and
    /// kicks a fresh fetch — the two lists are completely separate so
    /// the cursor can't carry over.
    func setMode(_ newMode: PeopleListMode) {
        guard newMode != mode else { return }
        mode = newMode
        people = []
        nextPeopleCursor = nil
        hasMorePeople = true
        peopleError = nil
        clearSelection()
        Task { await loadNextPage() }
    }

    // MARK: - Mutations (M6)

    /// Rename a person via `PATCH /v1/people/{id}`. Updates the in-memory
    /// list optimistically — both `people` (so the grid card refreshes)
    /// and `selectedPerson` (so the detail header refreshes without a
    /// pop/repush dance).
    func renamePerson(_ person: PersonItem, to newName: String) async {
        let trimmed = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed != person.displayName else { return }
        await mutate(personId: person.personId) { client in
            let updated: PersonItem = try await client.patch(
                "/v1/people/\(person.personId)",
                body: PersonUpdateRequest(displayName: trimmed)
            )
            self.applyUpdated(updated)
        }
    }

    /// Delete a person and remove them from the list. Detail view pops
    /// back to the grid via the `dismissDetailRequest` flag.
    func deletePerson(_ person: PersonItem) async {
        await mutate(personId: person.personId) { client in
            try await client.delete("/v1/people/\(person.personId)")
            self.removePerson(person.personId)
            self.dismissDetailRequest = true
        }
    }

    /// Merge `source` into `target` — every face on `source` gets
    /// reattached to `target`, then `source` is deleted server-side.
    /// Removes `source` from the list and pops the detail view.
    func mergePerson(_ source: PersonItem, into target: PersonItem) async {
        guard source.personId != target.personId else { return }
        await mutate(personId: source.personId) { client in
            // The endpoint takes the *source* in the body; the URL is
            // the *target* (the kept person). Server returns the merged
            // PersonItem with an updated face count.
            let merged: PersonItem = try await client.post(
                "/v1/people/\(target.personId)/merge",
                body: MergeRequest(sourcePersonId: source.personId)
            )
            self.removePerson(source.personId)
            self.applyUpdated(merged)
            self.dismissDetailRequest = true
        }
    }

    /// Restore a dismissed person under a new display name. Pops the
    /// detail view because the dismissed list no longer contains them.
    func undismissPerson(_ person: PersonItem, as newName: String) async {
        let trimmed = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        await mutate(personId: person.personId) { client in
            let _: PersonItem = try await client.post(
                "/v1/people/\(person.personId)/undismiss",
                body: UndismissRequest(displayName: trimmed)
            )
            // Whether we're in dismissed mode or active, the local list
            // no longer reflects reality — drop them from the visible
            // list and let the user pop back.
            self.removePerson(person.personId)
            self.dismissDetailRequest = true
        }
    }

    private func mutate(
        personId: String,
        op: (APIClient) async throws -> Void
    ) async {
        guard let client else { return }
        pendingMutations.insert(personId)
        mutationError = nil
        defer { pendingMutations.remove(personId) }
        do {
            try await op(client)
        } catch {
            if Self.isCancellation(error) { return }
            mutationError = describe(error)
        }
    }

    private func describe(_ error: Error) -> String {
        if case APIError.serverError(_, let message) = error {
            return message
        }
        return "\(error)"
    }

    /// Patch a refreshed `PersonItem` into both `people` and
    /// `selectedPerson` so the UI doesn't need a roundtrip refetch.
    private func applyUpdated(_ updated: PersonItem) {
        if let idx = people.firstIndex(where: { $0.personId == updated.personId }) {
            people[idx] = updated
        }
        if selectedPerson?.personId == updated.personId {
            selectedPerson = updated
        }
    }

    private func removePerson(_ personId: String) {
        people.removeAll { $0.personId == personId }
    }

    // MARK: - Merge picker typeahead

    /// Debounced typeahead for the merge picker. Excludes the source
    /// person from the result list so the user can't try to merge
    /// someone into themselves.
    func debouncedMergeSearch(query: String, excluding excludedPersonId: String) {
        mergeSearchQuery = query
        mergeSearchTask?.cancel()
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            mergeSearchResults = []
            return
        }
        mergeSearchTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(300))
            guard !Task.isCancelled else { return }
            await self?.searchMergeTargets(query: trimmed, excluding: excludedPersonId)
        }
    }

    private func searchMergeTargets(query: String, excluding excludedPersonId: String) async {
        guard let client else { return }
        isSearchingMerge = true
        defer { isSearchingMerge = false }
        do {
            let response: PersonListResponse = try await client.get(
                "/v1/people", query: ["q": query, "limit": "10"]
            )
            if mergeSearchQuery.trimmingCharacters(in: .whitespacesAndNewlines) == query {
                mergeSearchResults = response.items.filter { $0.personId != excludedPersonId }
            }
        } catch {
            // Non-fatal — empty result is fine
        }
    }

    func resetMergeSearch() {
        mergeSearchTask?.cancel()
        mergeSearchTask = nil
        mergeSearchQuery = ""
        mergeSearchResults = []
    }
}
