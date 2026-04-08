import SwiftUI
import LumiverbKit

/// Sidebar listing all libraries with selection and directory tree, plus
/// a top-level "People" entry that switches the detail panel into the
/// People browse view (Phase 6 M3 of ADR-014).
struct LibrarySidebar: View {
    let libraries: [Library]
    @ObservedObject var browseState: BrowseState
    @Binding var section: SidebarSection

    var body: some View {
        VStack(spacing: 0) {
            // Top-level "People" entry. Sits outside the libraries List
            // because that List uses `selectedLibraryId` as its selection
            // model and adding a non-library row would force the two
            // selections through a compound enum. A plain Button styled
            // like a sidebar row is the smaller change.
            Button {
                section = .people
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: "person.2.fill")
                        .frame(width: 16)
                    Text("People")
                    Spacer()
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(
                    section == .people
                        ? Color.accentColor.opacity(0.2)
                        : Color.clear
                )
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.top, 8)

            Divider()
                .padding(.top, 4)

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
            .onChange(of: browseState.selectedLibraryId) { _, newValue in
                // Selecting a library should always pop us back to the
                // library detail panel, even if we were just in People.
                if newValue != nil { section = .library }
            }

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
