import SwiftUI
import LumiverbKit

/// Sidebar listing all libraries with selection and directory tree.
struct LibrarySidebar: View {
    let libraries: [Library]
    @ObservedObject var browseState: BrowseState

    var body: some View {
        List(selection: $browseState.selectedLibraryId) {
            Section("Libraries") {
                ForEach(libraries) { lib in
                    Label(lib.name, systemImage: "folder.fill")
                        .tag(lib.libraryId)
                }
            }

            // Directory tree for the selected library
            if browseState.selectedLibraryId != nil, !browseState.directories.isEmpty {
                Section("Folders") {
                    DirectoryTreeView(browseState: browseState)
                }
            }
        }
        .listStyle(.sidebar)
        .navigationTitle("Lumiverb")
    }
}
