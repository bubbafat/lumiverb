import SwiftUI

/// List of collections grouped into Mine / Shared with a "+" button.
public struct CollectionsListView: View {
    @ObservedObject public var collectionsState: CollectionsState
    public let client: APIClient?

    @State private var showCreateSheet = false

    public init(collectionsState: CollectionsState, client: APIClient?) {
        self.collectionsState = collectionsState
        self.client = client
    }

    public var body: some View {
        List {
            if !collectionsState.ownCollections.isEmpty {
                Section("Mine") {
                    ForEach(collectionsState.ownCollections) { col in
                        collectionRow(col)
                    }
                }
            }

            if !collectionsState.sharedCollections.isEmpty {
                Section("Shared") {
                    ForEach(collectionsState.sharedCollections) { col in
                        collectionRow(col)
                    }
                }
            }

            if collectionsState.collections.isEmpty && !collectionsState.isLoadingList {
                Text("No collections yet")
                    .foregroundColor(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .padding()
            }
        }
        .overlay {
            if collectionsState.isLoadingList && collectionsState.collections.isEmpty {
                ProgressView()
            }
        }
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button {
                    showCreateSheet = true
                } label: {
                    Label("New Collection", systemImage: "plus")
                }
            }
        }
        .sheet(isPresented: $showCreateSheet) {
            CreateCollectionSheet(collectionsState: collectionsState)
        }
        .task {
            await collectionsState.loadCollections()
        }
    }

    @ViewBuilder
    private func collectionRow(_ col: AssetCollection) -> some View {
        Button {
            Task { await collectionsState.openCollectionDetail(col) }
        } label: {
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text(col.name)
                        .lineLimit(1)
                    Text("\(col.assetCount) item\(col.assetCount == 1 ? "" : "s")")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
                if col.parsedVisibility == .shared {
                    Image(systemName: "person.2")
                        .font(.caption)
                        .foregroundColor(.secondary)
                } else if col.parsedVisibility == .public {
                    Image(systemName: "globe")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Create collection sheet

struct CreateCollectionSheet: View {
    @ObservedObject var collectionsState: CollectionsState
    @Environment(\.dismiss) private var dismiss

    @State private var name = ""

    var body: some View {
        VStack(spacing: 16) {
            Text("New Collection")
                .font(.headline)

            TextField("Name", text: $name)
                .textFieldStyle(.roundedBorder)

            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Create") {
                    Task {
                        _ = await collectionsState.createCollection(name: name)
                        dismiss()
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding()
        .frame(minWidth: 300)
    }
}
