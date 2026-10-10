import SwiftUI

/// Sheet for naming and saving the current browse filters and search as a
/// project (`from_search`): it holds the clips that match when saved and
/// doesn't follow the search afterwards.
public struct SaveSmartCollectionSheet: View {
    @ObservedObject public var browseState: BrowseState
    @ObservedObject public var collectionsState: CollectionsState
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""
    @State private var isSaving = false
    @State private var errorMessage: String?

    public init(browseState: BrowseState, collectionsState: CollectionsState) {
        self.browseState = browseState
        self.collectionsState = collectionsState
    }

    public var body: some View {
        VStack(spacing: 16) {
            Text("Save Search as Collection")
                .font(.headline)

            Text("Holds the photos that match now.")
                .font(.caption)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)

            TextField("Collection name", text: $name)
                .textFieldStyle(.roundedBorder)

            if let errorMessage {
                Text(errorMessage)
                    .font(.caption)
                    .foregroundColor(.red)
            }

            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Save") {
                    save()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty || isSaving)
            }
        }
        .padding()
        .frame(minWidth: 350)
    }

    private func save() {
        isSaving = true
        errorMessage = nil

        // Build the saved query from current filter state using filter algebra
        let leafFilters = browseState.filters.toLeafFilters(
            libraryId: browseState.selectedLibraryId,
            pathPrefix: browseState.selectedPath,
            searchQuery: browseState.mode == .search ? browseState.committedSearchQuery : nil
        )
        let search = SavedQueryV2(
            filters: leafFilters,
            sort: browseState.filters.sortField,
            direction: browseState.filters.sortDirection
        )
        let trimmedName = name.trimmingCharacters(in: .whitespaces)

        Task {
            do {
                _ = try await collectionsState.createFromSearch(name: trimmedName, search: search)
                dismiss()
            } catch {
                errorMessage = error.localizedDescription
                isSaving = false
            }
        }
    }
}
