import SwiftUI

/// Detail view for a single person — header with name + face count, then
/// a grid of every photo containing that person. Phase 6 M3 of ADR-014.
///
/// Tapping a photo opens the existing `LightboxView` overlay through
/// `BrowseState`, with the person's full asset id list installed as the
/// lightbox's prev/next navigation override so left/right arrows iterate
/// the person's faces instead of whatever the underlying library mode
/// happens to have loaded. The override is cleared when the lightbox
/// closes (in `BrowseState.closeLightbox`).
struct PersonDetailView: View {
    let person: PersonItem
    @ObservedObject var peopleState: PeopleState
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    @Environment(\.dismiss) private var dismiss

    @State private var showRenameSheet = false
    @State private var showMergeSheet = false
    @State private var showRestoreSheet = false
    @State private var confirmDelete = false

    private let columns = Array(
        repeating: GridItem(.flexible(), spacing: 2),
        count: 4
    )

    /// Whichever is freshest — the parameter we were pushed with, or the
    /// `peopleState.selectedPerson` if a successful rename / merge has
    /// updated it. Lets the header reflect a rename without re-pushing.
    private var current: PersonItem {
        if let sel = peopleState.selectedPerson, sel.personId == person.personId {
            return sel
        }
        return person
    }

    private var isPending: Bool {
        peopleState.pendingMutations.contains(person.personId)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header
                if let error = peopleState.mutationError {
                    HStack(spacing: 6) {
                        Image(systemName: "exclamationmark.octagon.fill")
                            .foregroundColor(.red)
                        Text(error)
                            .font(.callout)
                        Spacer()
                        Button("Dismiss") { peopleState.mutationError = nil }
                            .controlSize(.small)
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
                    .background(Color.red.opacity(0.12))
                }
                Divider()
                grid
            }
        }
        .navigationTitle(current.displayName)
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                if peopleState.mode == .active {
                    Button {
                        showRenameSheet = true
                    } label: {
                        Label("Rename", systemImage: "pencil")
                    }
                    .disabled(isPending)

                    Button {
                        peopleState.resetMergeSearch()
                        showMergeSheet = true
                    } label: {
                        Label("Merge…", systemImage: "arrow.triangle.merge")
                    }
                    .disabled(isPending)
                } else {
                    Button {
                        showRestoreSheet = true
                    } label: {
                        Label("Restore", systemImage: "arrow.uturn.backward.circle")
                    }
                    .disabled(isPending)
                }

                Button(role: .destructive) {
                    confirmDelete = true
                } label: {
                    Label("Delete", systemImage: "trash")
                }
                .disabled(isPending)
            }
        }
        .sheet(isPresented: $showRenameSheet) {
            RenamePersonSheet(
                person: current,
                peopleState: peopleState,
                isPresented: $showRenameSheet
            )
        }
        .sheet(isPresented: $showMergeSheet) {
            MergePersonSheet(
                source: current,
                peopleState: peopleState,
                client: client,
                isPresented: $showMergeSheet
            )
        }
        .sheet(isPresented: $showRestoreSheet) {
            RestorePersonSheet(
                person: current,
                peopleState: peopleState,
                isPresented: $showRestoreSheet
            )
        }
        .alert("Delete \(current.displayName)?", isPresented: $confirmDelete) {
            Button("Cancel", role: .cancel) {}
            Button("Delete", role: .destructive) {
                Task { await peopleState.deletePerson(current) }
            }
        } message: {
            Text("This removes the person and unassigns all their faces. Faces themselves are kept and can be reassigned.")
        }
        .onChange(of: peopleState.dismissDetailRequest) { _, shouldDismiss in
            // Set by the state object after a successful delete / merge /
            // undismiss so the navigation stack can pop back to the grid
            // without the mutation method needing a view reference.
            if shouldDismiss {
                peopleState.dismissDetailRequest = false
                dismiss()
            }
        }
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 16) {
            FaceThumbnailView(faceId: current.representativeFaceId, client: client)
                .frame(width: 80, height: 80)
                .background(Circle().fill(Color.gray.opacity(0.15)))
                .clipShape(Circle())
                .overlay(
                    Circle().stroke(Color.secondary.opacity(0.2), lineWidth: 1)
                )

            VStack(alignment: .leading, spacing: 4) {
                Text(current.displayName)
                    .font(.title2)
                Text("\(current.faceCount) photo\(current.faceCount == 1 ? "" : "s")")
                    .font(.subheadline)
                    .foregroundColor(.secondary)
            }
            Spacer()
            if isPending {
                ProgressView().controlSize(.small)
            }
        }
        .padding(16)
    }

    // MARK: - Grid

    @ViewBuilder
    private var grid: some View {
        if peopleState.personFaces.isEmpty && !peopleState.isLoadingFaces {
            VStack(spacing: 8) {
                Image(systemName: "photo.on.rectangle")
                    .font(.system(size: 32))
                    .foregroundColor(.secondary)
                Text("No photos for this person yet.")
                    .font(.callout)
                    .foregroundColor(.secondary)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 60)
        } else {
            LazyVGrid(columns: columns, spacing: 2) {
                ForEach(peopleState.personFaces) { face in
                    PersonFaceCellView(face: face, client: client)
                        .onTapGesture {
                            openLightbox(at: face)
                        }
                        .onAppear {
                            if let last = peopleState.personFaces.last,
                               last.faceId == face.faceId {
                                Task { await peopleState.loadNextFacesPage() }
                            }
                        }
                }
            }
            .padding(2)
        }

        if peopleState.isLoadingFaces {
            ProgressView()
                .padding()
        }

        if let error = peopleState.personFacesError {
            Text(error)
                .foregroundColor(.red)
                .font(.caption)
                .padding()
        }
    }

    private func openLightbox(at face: PersonFaceItem) {
        // Install the person's asset list as the lightbox prev/next
        // override BEFORE loading the detail, so the lightbox renders
        // with the right neighborhood from the first frame.
        browseState.displayedAssetIdsOverride = peopleState.personFaces.map(\.assetId)
        Task { await browseState.loadAssetDetail(assetId: face.assetId) }
    }
}

/// One cell in the per-person photo grid. Same look as `AssetCellView` but
/// keyed off `assetId` and without the AssetPageItem (which we don't have
/// here — `PersonFaceItem` exposes only face/asset ids and a rel_path).
private struct PersonFaceCellView: View {
    let face: PersonFaceItem
    let client: APIClient?

    var body: some View {
        AuthenticatedImageView(
            assetId: face.assetId,
            client: client,
            type: .thumbnail
        )
        .frame(minHeight: 120)
        .clipped()
        .background(Color.gray.opacity(0.1))
        .aspectRatio(1, contentMode: .fill)
        .cornerRadius(2)
        .contentShape(Rectangle())
    }
}

// MARK: - Rename sheet (M6)

/// Inline sheet for `PATCH /v1/people/{id}` with a single text field.
/// The Save button is disabled until the input is non-empty and actually
/// differs from the current name, so the round-trip-for-no-change case
/// can't happen by accident.
struct RenamePersonSheet: View {
    let person: PersonItem
    @ObservedObject var peopleState: PeopleState
    @Binding var isPresented: Bool

    @State private var newName: String

    init(person: PersonItem, peopleState: PeopleState, isPresented: Binding<Bool>) {
        self.person = person
        self.peopleState = peopleState
        self._isPresented = isPresented
        self._newName = State(initialValue: person.displayName)
    }

    private var trimmed: String {
        newName.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canSave: Bool {
        !trimmed.isEmpty && trimmed != person.displayName
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Rename Person")
                .font(.headline)

            TextField("Display name", text: $newName)
                .textFieldStyle(.roundedBorder)
                .onSubmit { if canSave { save() } }

            HStack {
                Spacer()
                Button("Cancel") { isPresented = false }
                    .keyboardShortcut(.cancelAction)
                Button("Save") { save() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!canSave)
            }
        }
        .padding(20)
        .frame(width: 360)
    }

    private func save() {
        Task {
            await peopleState.renamePerson(person, to: trimmed)
            isPresented = false
        }
    }
}

// MARK: - Merge sheet (M6)

/// Typeahead picker for the merge target. Mirrors the M4 face-assignment
/// popover's debounced search pattern: type to filter active people,
/// click one to merge `source` into them. The merge endpoint is
/// `POST /v1/people/{target}/merge` with the source in the body.
struct MergePersonSheet: View {
    let source: PersonItem
    @ObservedObject var peopleState: PeopleState
    let client: APIClient?
    @Binding var isPresented: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Merge \"\(source.displayName)\" into…")
                .font(.headline)
            Text("All \(source.faceCount) face\(source.faceCount == 1 ? "" : "s") will move to the chosen person, and \(source.displayName) will be deleted.")
                .font(.caption)
                .foregroundColor(.secondary)

            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .foregroundColor(.secondary)
                TextField("Search people…", text: Binding(
                    get: { peopleState.mergeSearchQuery },
                    set: { peopleState.debouncedMergeSearch(
                        query: $0,
                        excluding: source.personId
                    ) }
                ))
                .textFieldStyle(.plain)
                if peopleState.isSearchingMerge {
                    ProgressView().controlSize(.mini)
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(Color.gray.opacity(0.12))
            .cornerRadius(6)

            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(peopleState.mergeSearchResults) { person in
                        Button {
                            Task {
                                await peopleState.mergePerson(source, into: person)
                                isPresented = false
                            }
                        } label: {
                            HStack(spacing: 8) {
                                FaceThumbnailView(
                                    faceId: person.representativeFaceId,
                                    client: client
                                )
                                .frame(width: 32, height: 32)
                                .clipShape(Circle())
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(person.displayName)
                                        .font(.callout)
                                    Text("\(person.faceCount) photos")
                                        .font(.caption2)
                                        .foregroundColor(.secondary)
                                }
                                Spacer()
                            }
                            .padding(.horizontal, 6)
                            .padding(.vertical, 4)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .frame(maxHeight: 240)

            HStack {
                Spacer()
                Button("Cancel") { isPresented = false }
                    .keyboardShortcut(.cancelAction)
            }
        }
        .padding(20)
        .frame(width: 400, height: 420)
    }
}

// MARK: - Restore (undismiss) sheet (M6)

/// Restoring a dismissed person requires a fresh display name — the
/// server's UndismissRequest enforces it (the dismissed-person's stored
/// name is the placeholder "(dismissed)"). After success the person is
/// dropped from the dismissed list and the detail view pops back.
struct RestorePersonSheet: View {
    let person: PersonItem
    @ObservedObject var peopleState: PeopleState
    @Binding var isPresented: Bool

    @State private var newName: String = ""

    private var trimmed: String {
        newName.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Restore Person")
                .font(.headline)
            Text("Give this restored cluster a real display name. Their faces will become assignable from the lightbox again.")
                .font(.caption)
                .foregroundColor(.secondary)

            TextField("Display name", text: $newName)
                .textFieldStyle(.roundedBorder)
                .onSubmit { if !trimmed.isEmpty { save() } }

            HStack {
                Spacer()
                Button("Cancel") { isPresented = false }
                    .keyboardShortcut(.cancelAction)
                Button("Restore") { save() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(trimmed.isEmpty)
            }
        }
        .padding(20)
        .frame(width: 380)
    }

    private func save() {
        Task {
            await peopleState.undismissPerson(person, as: trimmed)
            isPresented = false
        }
    }
}
