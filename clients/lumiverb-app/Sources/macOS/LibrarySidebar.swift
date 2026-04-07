import SwiftUI
import LumiverbKit

/// Sidebar listing all libraries with selection and directory tree.
struct LibrarySidebar: View {
    let libraries: [Library]
    @ObservedObject var browseState: BrowseState

    var body: some View {
        VStack(spacing: 0) {
            // Library list (owns selection)
            List(selection: $browseState.selectedLibraryId) {
                Section("Libraries") {
                    ForEach(libraries) { lib in
                        Label(lib.name, systemImage: "folder.fill")
                            .tag(lib.libraryId)
                            .contextMenu {
                                Button("Open Source Location") {
                                    NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: lib.rootPath)
                                }
                                Divider()
                                ReEnrichMenu { ops in
                                    browseState.reEnrich(operations: ops)
                                }
                            }
                    }
                }
            }
            .listStyle(.sidebar)
            .frame(maxHeight: libraries.count <= 3 ? 120 : 200)

            // Directory tree (separate from list so context menus work)
            if browseState.selectedLibraryId != nil, !browseState.directories.isEmpty {
                Divider()
                ScrollView {
                    VStack(alignment: .leading, spacing: 0) {
                        Text("Folders")
                            .font(.caption)
                            .fontWeight(.semibold)
                            .foregroundColor(.secondary)
                            .textCase(.uppercase)
                            .padding(.horizontal, 12)
                            .padding(.top, 8)
                            .padding(.bottom, 4)

                        DirectoryTreeView(browseState: browseState)
                            .padding(.horizontal, 4)
                    }
                }
            }
        }
        .navigationTitle("Lumiverb")
    }
}
