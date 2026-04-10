import SwiftUI

/// Sheet for adding assets to one or more collections.
/// Shows existing collections with multi-select + a "New collection..." row.
public struct AddToCollectionSheet: View {
    @ObservedObject public var collectionsState: CollectionsState
    public let assetIds: [String]
    @Environment(\.dismiss) private var dismiss

    @State private var selectedIds: Set<String> = []
    @State private var showCreate = false
    @State private var newName = ""
    @State private var isAdding = false

    public init(collectionsState: CollectionsState, assetIds: [String]) {
        self.collectionsState = collectionsState
        self.assetIds = assetIds
    }

    public var body: some View {
        VStack(spacing: 0) {
            Text("Add to Collection")
                .font(.headline)
                .padding()

            List {
                ForEach(collectionsState.ownCollections) { col in
                    Button {
                        if selectedIds.contains(col.collectionId) {
                            selectedIds.remove(col.collectionId)
                        } else {
                            selectedIds.insert(col.collectionId)
                        }
                    } label: {
                        HStack {
                            Image(systemName: selectedIds.contains(col.collectionId) ? "checkmark.circle.fill" : "circle")
                                .foregroundColor(selectedIds.contains(col.collectionId) ? .accentColor : .secondary)
                            Text(col.name)
                            Spacer()
                            Text("\(col.assetCount)")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                }

                Button {
                    showCreate = true
                } label: {
                    Label("New Collection...", systemImage: "plus")
                }
            }

            Divider()

            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Add") {
                    isAdding = true
                    Task {
                        for colId in selectedIds {
                            _ = await collectionsState.addAssets(collectionId: colId, assetIds: assetIds)
                        }
                        isAdding = false
                        dismiss()
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(selectedIds.isEmpty || isAdding)
            }
            .padding()
        }
        .frame(minWidth: 350, minHeight: 300)
        .sheet(isPresented: $showCreate) {
            inlineCreateSheet
        }
        .task {
            if collectionsState.collections.isEmpty {
                await collectionsState.loadCollections()
            }
        }
    }

    private var inlineCreateSheet: some View {
        VStack(spacing: 16) {
            Text("New Collection")
                .font(.headline)

            TextField("Name", text: $newName)
                .textFieldStyle(.roundedBorder)

            HStack {
                Button("Cancel") {
                    showCreate = false
                    newName = ""
                }
                Spacer()
                Button("Create") {
                    Task {
                        if let created = await collectionsState.createCollection(name: newName) {
                            selectedIds.insert(created.collectionId)
                        }
                        showCreate = false
                        newName = ""
                    }
                }
                .disabled(newName.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding()
        .frame(minWidth: 280)
    }
}
