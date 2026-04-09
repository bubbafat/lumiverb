import SwiftUI
import LumiverbKit

/// View model for the lightbox face overlay (M2 of ADR-014 Phase 6).
///
/// Owns the per-asset face list and the in-flight loading state. Hosted as
/// a `@StateObject` inside `LightboxView` so its lifetime matches the open
/// lightbox; the view triggers `loadFaces(forAsset:)` from a `.task(id:)`
/// keyed on the current asset's id, so navigation between assets cancels
/// the previous fetch and starts a new one automatically.
///
/// **State scope.** This is a small view-scoped state object — it does not
/// live on `BrowseState`. The same model will be reused in M4 (face
/// assignment in the lightbox) where it'll grow to handle assign/dismiss
/// mutations and cache invalidation. People-page and cluster-review state
/// (M3, M5) will get their own separate `@ObservedObject` since they're
/// not lightbox-scoped.
@MainActor
final class LightboxFacesViewModel: ObservableObject {
    @Published var faces: [FaceListItem] = []
    @Published var isLoading: Bool = false

    // MARK: - M4 selection / mutation state

    /// The face that the user has tapped to open the assignment popover.
    /// Drives `.popover(isPresented:)` on each `FaceBoxView`.
    @Published var selectedFaceId: String?

    /// Set while an assign / unassign request is in flight, so the popover
    /// can show a spinner and disable its action buttons.
    @Published var pendingMutation: Bool = false

    /// Last mutation error, displayed in the popover footer. Cleared when
    /// the user starts a new mutation or selects a different face.
    @Published var mutationError: String?

    // MARK: - M4 person search state (for the popover's typeahead)

    @Published var personSearchQuery: String = ""
    @Published var personSearchResults: [PersonItem] = []
    @Published var isSearchingPeople: Bool = false
    private var personSearchTask: Task<Void, Never>?

    // MARK: - Per-face nearest-people suggestions (rank by similarity)

    /// Top-N named people ranked by cosine similarity to the *clicked
    /// face's* embedding. This is the signal the user actually wants
    /// in the popover — "who looks like this face?" — and replaces the
    /// old behavior of sorting candidates by total photo count, which
    /// was wrong for heterogeneous clusters. Populated by
    /// ``loadNearestPeopleForFace`` when a face is selected. Hits
    /// `GET /v1/faces/{face_id}/nearest-people` (the per-face variant
    /// of the cluster endpoint that ClusterReviewState already uses).
    @Published var nearestPeopleForFace: [NearestPersonItem] = []
    @Published var isLoadingNearestForFace: Bool = false
    private var nearestForFaceTask: Task<Void, Never>?

    // MARK: - Cluster-review handoff

    /// The face id the user arrived at via the cluster-review per-face
    /// flow. Drives the red highlight border in `FaceBoxView` (named
    /// faces still take precedence — the moment a tag lands the border
    /// flips to green) and the auto-advance behavior in `mutate`.
    /// Cleared whenever the lightbox switches assets.
    @Published var highlightedFaceId: String?

    /// Invoked after the user successfully tags the face matching
    /// `highlightedFaceId`. Set by `LightboxView` to advance to the
    /// next cluster asset (or close the lightbox if there are no more).
    /// Fires after a brief delay so the user sees the red→green border
    /// flash before the asset changes — otherwise the success state
    /// is invisible.
    var onHighlightFaceTagged: (() -> Void)?

    let client: APIClient?
    private var loadedAssetId: String?

    init(client: APIClient?) {
        self.client = client
    }

    /// Fetch faces for `assetId` from `GET /v1/assets/{id}/faces`. Skips the
    /// network call if the same asset's faces are already cached on this
    /// view model. Resets to an empty list on error so the lightbox doesn't
    /// keep showing stale boxes from the previous asset.
    func loadFaces(forAsset assetId: String) async {
        guard let client else { return }
        if loadedAssetId == assetId, !faces.isEmpty { return }
        loadedAssetId = assetId
        // Switching assets always closes the popover so it doesn't end
        // up anchored to a face that's no longer on screen, and clears
        // any cluster-review highlight from the previous asset (the new
        // asset's highlight, if any, is set after this method returns).
        deselectFace()
        highlightedFaceId = nil
        isLoading = true
        defer { isLoading = false }

        do {
            let response: FaceListResponse = try await client.get("/v1/assets/\(assetId)/faces")
            self.faces = response.faces
        } catch {
            // Non-fatal — face overlay just stays empty for this asset.
            self.faces = []
        }
    }

    /// Forget any cached faces. Called when the user toggles "Show faces"
    /// off so reopening with a different asset doesn't briefly flash old
    /// boxes from a stale state.
    func reset() {
        self.faces = []
        self.loadedAssetId = nil
        highlightedFaceId = nil
        deselectFace()
    }

    // MARK: - Selection

    func selectFace(_ faceId: String) {
        selectedFaceId = faceId
        personSearchQuery = ""
        personSearchResults = []
        nearestPeopleForFace = []
        mutationError = nil
        // Kick off the per-face nearest-people fetch so the popover
        // shows similarity-ranked suggestions before the user types
        // anything in the search box. Cancels any in-flight prior
        // request automatically.
        nearestForFaceTask?.cancel()
        nearestForFaceTask = Task { [weak self] in
            await self?.loadNearestPeopleForFace(faceId)
        }
    }

    func deselectFace() {
        selectedFaceId = nil
        personSearchTask?.cancel()
        personSearchTask = nil
        personSearchQuery = ""
        personSearchResults = []
        nearestForFaceTask?.cancel()
        nearestForFaceTask = nil
        nearestPeopleForFace = []
        isLoadingNearestForFace = false
    }

    // MARK: - Per-face nearest-people loader

    /// Fetch the top-N named people sorted by similarity to ``faceId``'s
    /// embedding. Mirrors what the web Lightbox does via
    /// `getNearestPeopleForFace`. Returns `[]` (and the popover falls
    /// back to search-only) if the face has no embedding or the call
    /// fails — the endpoint deliberately returns an empty list rather
    /// than 404 in that case so callers don't need an error path.
    private func loadNearestPeopleForFace(_ faceId: String) async {
        guard let client else { return }
        isLoadingNearestForFace = true
        defer { isLoadingNearestForFace = false }
        do {
            let people: [NearestPersonItem] = try await client.get(
                "/v1/faces/\(faceId)/nearest-people",
                query: ["limit": "8"]
            )
            // Drop the result if the user has already moved on to a
            // different face — otherwise we'd flash stale suggestions
            // for ~half a second after a fast click.
            guard selectedFaceId == faceId else { return }
            nearestPeopleForFace = people
        } catch {
            // Non-fatal: empty list is fine; the search field still works.
        }
    }

    // MARK: - Person search (debounced typeahead)

    /// Called from the popover's TextField on every keystroke. Debounces
    /// at 300ms — short enough to feel live, long enough that fast typists
    /// don't fire a request per character.
    func debouncedPersonSearch(query: String) {
        personSearchQuery = query
        personSearchTask?.cancel()
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            personSearchResults = []
            return
        }
        personSearchTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(300))
            guard !Task.isCancelled else { return }
            await self?.searchPeople(trimmed)
        }
    }

    private func searchPeople(_ query: String) async {
        guard let client else { return }
        isSearchingPeople = true
        defer { isSearchingPeople = false }
        do {
            let response: PersonListResponse = try await client.get(
                "/v1/people", query: ["q": query, "limit": "10"]
            )
            // Drop stale results if the query changed while we were waiting.
            if personSearchQuery.trimmingCharacters(in: .whitespacesAndNewlines) == query {
                personSearchResults = response.items
            }
        } catch {
            // Non-fatal: empty list is fine. Cancellation falls through here too.
        }
    }

    // MARK: - Mutations

    /// Assign `faceId` to `personId`. Always issues a `DELETE` first to
    /// clear any existing assignment, since the server returns 409 on
    /// reassign by design — making this method idempotent across both
    /// the new-assignment and reassign cases.
    func assignFace(_ faceId: String, toPersonId personId: String) async {
        await mutate(faceId: faceId) { client in
            try? await client.delete("/v1/faces/\(faceId)/assign")
            let _: FaceAssignResponse = try await client.post(
                "/v1/faces/\(faceId)/assign",
                body: FaceAssignRequest(personId: personId)
            )
        }
    }

    /// Create a new person with `name` and assign `faceId` to it. The
    /// server-side endpoint does both in one transaction. Same delete-
    /// first dance as `assignFace(toPersonId:)`.
    func assignFace(_ faceId: String, newPersonName name: String) async {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        await mutate(faceId: faceId) { client in
            try? await client.delete("/v1/faces/\(faceId)/assign")
            let _: FaceAssignResponse = try await client.post(
                "/v1/faces/\(faceId)/assign",
                body: FaceAssignRequest(newPersonName: trimmed)
            )
        }
    }

    /// Remove `faceId`'s person assignment entirely.
    func unassignFace(_ faceId: String) async {
        await mutate(faceId: faceId) { client in
            try await client.delete("/v1/faces/\(faceId)/assign")
        }
    }

    /// Run a mutation against the server, then refetch the asset's face
    /// list so the overlay reflects the new assignment. The popover is
    /// closed on success; on failure the error is shown inside it.
    ///
    /// **Cluster-review handoff:** if the mutated face matches
    /// ``highlightedFaceId``, fires ``onHighlightFaceTagged`` after a
    /// brief delay so the user sees the red→green border flash before
    /// the lightbox advances to the next cluster asset.
    private func mutate(
        faceId: String,
        op: (APIClient) async throws -> Void
    ) async {
        guard let client else { return }
        pendingMutation = true
        mutationError = nil
        defer { pendingMutation = false }
        do {
            try await op(client)
            let wasHighlight = (faceId == highlightedFaceId)
            // Force a re-fetch of this asset's faces.
            if let assetId = loadedAssetId {
                let prev = assetId
                self.faces = []
                self.loadedAssetId = nil
                await loadFaces(forAsset: prev)
            }
            deselectFace()
            if wasHighlight {
                let cb = onHighlightFaceTagged
                Task { @MainActor in
                    try? await Task.sleep(for: .milliseconds(350))
                    cb?()
                }
            }
        } catch {
            mutationError = describe(error)
        }
    }

    private func describe(_ error: Error) -> String {
        if case APIError.serverError(_, let message) = error {
            return message
        }
        return "\(error)"
    }
}

// MARK: - Face overlay layer

/// Draws bounding-box overlays for every face on the current lightbox
/// asset. Sits as a sibling layer in `LightboxView`'s `ZStack`, NOT
/// inside `AuthenticatedImageView`, so the existing image-loading view
/// stays untouched. Both layers receive the same parent frame from the
/// ZStack and apply the same aspect-fit math against the asset's natural
/// dimensions, so the overlay's coordinate space matches the rendered
/// image rect by construction.
///
/// In M4 the overlay becomes interactive — face boxes are tappable
/// hit targets that open an assignment popover. The wrapping `Color.clear`
/// stays non-hit-testable so clicks in the empty letterbox area still
/// reach the navigation arrows underneath.
struct FaceOverlayView: View {
    let faces: [FaceListItem]
    /// Original asset width / height in pixels, from `AssetDetail`. The
    /// aspect-fit math only needs the ratio, so the proxy being a scaled
    /// copy of the source doesn't matter as long as the scaler preserves
    /// the aspect ratio (which it does — `ProxyGenerator` uses
    /// `kCGImageSourceThumbnailMaxPixelSize` which is uniform-scale).
    let imageWidth: Int
    let imageHeight: Int
    @ObservedObject var vm: LightboxFacesViewModel

    var body: some View {
        GeometryReader { proxy in
            let imgRect = aspectFitRect(
                contentSize: CGSize(width: imageWidth, height: imageHeight),
                in: proxy.size
            )
            ZStack(alignment: .topLeading) {
                // Empty background — non-hit-testable so clicks in the
                // letterboxed area pass through to the lightbox controls.
                Color.clear
                    .allowsHitTesting(false)

                ForEach(faces) { face in
                    if let bb = face.boundingBox {
                        let boxW = CGFloat(bb.width) * imgRect.width
                        let boxH = CGFloat(bb.height) * imgRect.height
                        let boxX = imgRect.minX + CGFloat(bb.x) * imgRect.width
                        let boxY = imgRect.minY + CGFloat(bb.y) * imgRect.height
                        FaceBoxView(
                            person: face.person,
                            label: labelText(face.person),
                            isHighlighted: vm.highlightedFaceId == face.faceId
                        )
                            .frame(width: boxW, height: boxH)
                            .contentShape(Rectangle())
                            .onTapGesture {
                                vm.selectFace(face.faceId)
                            }
                            .popover(
                                isPresented: popoverBinding(for: face.faceId),
                                attachmentAnchor: .rect(.bounds),
                                arrowEdge: .top
                            ) {
                                FaceAssignmentPopover(vm: vm, face: face)
                            }
                            .position(x: boxX + boxW / 2, y: boxY + boxH / 2)
                    }
                }
            }
        }
    }

    /// Translate `vm.selectedFaceId == faceId` into a `Bool` binding
    /// suitable for `.popover(isPresented:)`. SwiftUI flips the binding
    /// to false when the user clicks outside the popover; we forward
    /// that to `vm.deselectFace()` so the model state stays in sync.
    private func popoverBinding(for faceId: String) -> Binding<Bool> {
        Binding(
            get: { vm.selectedFaceId == faceId },
            set: { isShown in
                if !isShown && vm.selectedFaceId == faceId {
                    vm.deselectFace()
                }
            }
        )
    }

    /// `nil` for unidentified or dismissed faces — those render with a
    /// gray border and no name label. Identified faces show their
    /// person's display name in a small chip below the box.
    private func labelText(_ person: FaceMatchedPerson?) -> String? {
        guard let person, !person.dismissed else { return nil }
        return person.displayName
    }
}

/// One face's bounding box + (optional) name label. Color and label
/// presence depend on the person attribution:
///
/// - Identified (assigned to a non-dismissed person): green border, name label below.
/// - Highlighted (came from cluster review, not yet tagged): red border.
/// - Unidentified (no person, or assigned to a dismissed person): gray border, no label.
///
/// "Identified" wins over "highlighted": once a cluster-review face is
/// tagged, the border flips green even before the lightbox auto-advances
/// — otherwise the success state is invisible.
///
/// The label is rendered as an overlay anchored to the bottom edge so it
/// always sits just below the box regardless of how the box is positioned
/// within the parent.
struct FaceBoxView: View {
    let person: FaceMatchedPerson?
    let label: String?
    let isHighlighted: Bool

    private var isIdentified: Bool {
        guard let person else { return false }
        return !person.dismissed
    }

    private var borderColor: Color {
        if isIdentified { return .green }
        if isHighlighted { return .red }
        return .gray
    }

    var body: some View {
        Rectangle()
            .stroke(borderColor, lineWidth: 2)
            .background(Color.clear)
            .overlay(alignment: .bottom) {
                if let label {
                    Text(label)
                        .font(.caption2)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .foregroundColor(.white)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(Color.black.opacity(0.7))
                        .cornerRadius(2)
                        .alignmentGuide(.bottom) { d in d[.top] }  // sit just below the box edge
                        .padding(.top, 2)
                }
            }
    }
}

// MARK: - Aspect-fit math

/// The centered aspect-fit rect for `contentSize` inside `frameSize`.
///
/// This is the same math `Image.resizable().aspectRatio(contentMode: .fit)`
/// uses internally to letterbox a content image into its frame. We compute
/// it ourselves so the face overlay layer can place absolute pixel boxes
/// in the same coordinate space as the rendered image without having to
/// inspect `AuthenticatedImageView`'s internals.
func aspectFitRect(contentSize: CGSize, in frameSize: CGSize) -> CGRect {
    guard contentSize.width > 0, contentSize.height > 0,
          frameSize.width > 0, frameSize.height > 0
    else { return .zero }

    let contentAspect = contentSize.width / contentSize.height
    let frameAspect = frameSize.width / frameSize.height

    if contentAspect > frameAspect {
        // Wider than the frame — letterbox top/bottom.
        let h = frameSize.width / contentAspect
        return CGRect(
            x: 0,
            y: (frameSize.height - h) / 2,
            width: frameSize.width,
            height: h
        )
    } else {
        // Taller than the frame — letterbox left/right.
        let w = frameSize.height * contentAspect
        return CGRect(
            x: (frameSize.width - w) / 2,
            y: 0,
            width: w,
            height: frameSize.height
        )
    }
}

// MARK: - Face assignment popover (M4)

/// Anchored to a tapped face box. Shows the current assignment (with an
/// unassign button), a debounced typeahead person search, and an option
/// to create a new person from whatever the user has typed. Server-side
/// the assign endpoint is `POST /v1/faces/{face_id}/assign`; this view's
/// view model handles the delete-then-post dance for reassignment so the
/// server's "no silent reassign" 409 stays out of the UI.
struct FaceAssignmentPopover: View {
    @ObservedObject var vm: LightboxFacesViewModel
    let face: FaceListItem

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            currentAssignmentRow
            // Show similarity-ranked suggestions when the user hasn't
            // started typing, so the most-likely candidates are one
            // click away. The search field below is the escape hatch
            // for when the right person isn't in the top-N.
            if showsNearestSuggestions {
                nearestSuggestionsSection
            }
            searchField
            resultsList
            createNewRow
            footer
        }
        .padding(12)
        .frame(width: 280)
    }

    /// True when the per-face nearest list should be visible. Hidden
    /// once the user starts typing — the search results take over
    /// the same screen real estate to avoid two competing lists.
    private var showsNearestSuggestions: Bool {
        let hasQuery = !vm.personSearchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if hasQuery { return false }
        return vm.isLoadingNearestForFace || !vm.nearestPeopleForFace.isEmpty
    }

    // MARK: - Sections

    @ViewBuilder
    private var currentAssignmentRow: some View {
        if let person = face.person {
            HStack(spacing: 6) {
                Image(systemName: person.dismissed ? "person.crop.circle.badge.xmark" : "person.crop.circle.fill")
                    .foregroundColor(person.dismissed ? .secondary : .green)
                VStack(alignment: .leading, spacing: 1) {
                    Text(person.dismissed ? "Dismissed" : person.displayName)
                        .font(.callout)
                        .lineLimit(1)
                    Text("Currently assigned")
                        .font(.caption2)
                        .foregroundColor(.secondary)
                }
                Spacer()
                Button {
                    Task { await vm.unassignFace(face.faceId) }
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.plain)
                .help("Remove this face from \(person.displayName)")
                .disabled(vm.pendingMutation)
            }
            Divider()
        }
    }

    /// "Looks like…" similarity-ranked suggestions row. Mirrors the
    /// cluster-card suggestions UI in `ClusterCardView` but the source
    /// is per-face, not per-cluster — these are the people whose
    /// centroids are closest to *this* face's embedding. Filters out
    /// the face's currently-assigned person to avoid offering "change
    /// to the same person" as a no-op suggestion.
    private var nearestSuggestionsSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text("Looks like…")
                    .font(.caption)
                    .foregroundColor(.secondary)
                if vm.isLoadingNearestForFace {
                    ProgressView().controlSize(.mini)
                }
                Spacer()
            }
            let currentPersonId = face.person?.personId
            let suggestions = vm.nearestPeopleForFace.filter { $0.personId != currentPersonId }
            if !suggestions.isEmpty {
                VStack(spacing: 2) {
                    ForEach(suggestions) { person in
                        Button {
                            Task { await vm.assignFace(face.faceId, toPersonId: person.personId) }
                        } label: {
                            HStack(spacing: 6) {
                                Image(systemName: "person.crop.circle.fill")
                                    .foregroundColor(.accentColor.opacity(0.7))
                                    .font(.caption)
                                Text(person.displayName)
                                    .font(.callout)
                                    .lineLimit(1)
                                Spacer()
                                Text("\(person.faceCount)")
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                            }
                            .padding(.horizontal, 6)
                            .padding(.vertical, 3)
                            .background(Color.accentColor.opacity(0.10))
                            .cornerRadius(4)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .disabled(vm.pendingMutation)
                    }
                }
            }
            Divider()
        }
    }

    private var searchField: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass")
                .foregroundColor(.secondary)
                .font(.caption)
            TextField("Person name…", text: Binding(
                get: { vm.personSearchQuery },
                set: { vm.debouncedPersonSearch(query: $0) }
            ))
            .textFieldStyle(.plain)
            .font(.callout)
            .onSubmit {
                submitFirstMatch()
            }
            if vm.isSearchingPeople {
                ProgressView()
                    .controlSize(.mini)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
        .background(Color.gray.opacity(0.12))
        .cornerRadius(6)
        .disabled(vm.pendingMutation)
    }

    @ViewBuilder
    private var resultsList: some View {
        if !vm.personSearchResults.isEmpty {
            VStack(spacing: 0) {
                ForEach(vm.personSearchResults) { person in
                    Button {
                        Task { await vm.assignFace(face.faceId, toPersonId: person.personId) }
                    } label: {
                        HStack(spacing: 8) {
                            FaceThumbnailView(
                                faceId: person.representativeFaceId,
                                client: vm.client
                            )
                            .frame(width: 24, height: 24)
                            .clipShape(Circle())
                            Text(person.displayName)
                                .font(.callout)
                                .lineLimit(1)
                            Spacer()
                            Text("\(person.faceCount)")
                                .font(.caption2)
                                .foregroundColor(.secondary)
                        }
                        .padding(.horizontal, 4)
                        .padding(.vertical, 3)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .disabled(vm.pendingMutation)
                }
            }
        }
    }

    @ViewBuilder
    private var createNewRow: some View {
        let trimmed = vm.personSearchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty,
           !vm.personSearchResults.contains(where: {
               $0.displayName.caseInsensitiveCompare(trimmed) == .orderedSame
           }) {
            if !vm.personSearchResults.isEmpty {
                Divider()
            }
            Button {
                Task { await vm.assignFace(face.faceId, newPersonName: trimmed) }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: "person.crop.circle.badge.plus")
                        .foregroundColor(.accentColor)
                    Text("Create \"\(trimmed)\"")
                        .font(.callout)
                    Spacer()
                }
                .padding(.horizontal, 4)
                .padding(.vertical, 3)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(vm.pendingMutation)
        }
    }

    @ViewBuilder
    private var footer: some View {
        if vm.pendingMutation {
            HStack(spacing: 6) {
                ProgressView()
                    .controlSize(.small)
                Text("Saving…")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
        if let error = vm.mutationError {
            Text(error)
                .font(.caption)
                .foregroundColor(.red)
                .lineLimit(3)
        }
    }

    // MARK: - Helpers

    /// Pressing return in the search field assigns to the first
    /// case-insensitive exact match if one exists, otherwise creates
    /// a new person from the search text.
    private func submitFirstMatch() {
        let trimmed = vm.personSearchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        if let exact = vm.personSearchResults.first(where: {
            $0.displayName.caseInsensitiveCompare(trimmed) == .orderedSame
        }) {
            Task { await vm.assignFace(face.faceId, toPersonId: exact.personId) }
        } else {
            Task { await vm.assignFace(face.faceId, newPersonName: trimmed) }
        }
    }
}
