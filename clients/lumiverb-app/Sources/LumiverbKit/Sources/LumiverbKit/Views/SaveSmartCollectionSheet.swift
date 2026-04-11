import SwiftUI

/// Sheet for naming and saving a smart collection from the current
/// browse filters and search query.
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
            Text("Save as Smart Collection")
                .font(.headline)

            Text("This collection updates automatically as matching photos change.")
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
            searchQuery: browseState.mode == .search ? browseState.searchQuery : nil
        )
        let savedQuery = SavedQueryV2(
            filters: leafFilters,
            sort: browseState.filters.sortField,
            direction: browseState.filters.sortDirection
        )

        let request = CreateCollectionRequest(
            name: name.trimmingCharacters(in: .whitespaces),
            type: .smart,
            savedQuery: savedQuery
        )

        Task {
            do {
                _ = try await collectionsState.createSmartCollection(request: request)
                dismiss()
            } catch {
                errorMessage = error.localizedDescription
                isSaving = false
            }
        }
    }
}
