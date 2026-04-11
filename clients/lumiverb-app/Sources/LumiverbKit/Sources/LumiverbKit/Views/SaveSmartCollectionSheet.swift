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

        // Build the saved query from current browse state
        var filterDict: [String: Any] = [:]
        let params = browseState.filters.queryParams
        for (key, value) in params {
            // Skip sort defaults
            if key == "sort" && value == "taken_at" { continue }
            if key == "dir" && value == "desc" { continue }
            // Convert numeric strings back to numbers
            if let intVal = Int(value) {
                filterDict[key] = intVal
            } else if let doubleVal = Double(value), value.contains(".") {
                filterDict[key] = doubleVal
            } else if value == "true" {
                filterDict[key] = true
            } else if value == "false" {
                filterDict[key] = false
            } else {
                filterDict[key] = value
            }
        }

        let savedQuery = SavedQuery(
            q: browseState.mode == .search ? browseState.searchQuery : nil,
            filters: filterDict,
            libraryId: browseState.selectedLibraryId
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
