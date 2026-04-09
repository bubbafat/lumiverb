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
            // Top-level entries. Sit outside the libraries List because
            // that List uses `selectedLibraryId` as its selection model
            // and adding non-library rows would force the two selections
            // through a compound enum. Plain Buttons styled like sidebar
            // rows are the smaller change.
            VStack(spacing: 0) {
                sidebarRow(
                    label: "People",
                    icon: "person.2.fill",
                    isActive: section == .people
                ) { section = .people }

                sidebarRow(
                    label: "Review Clusters",
                    icon: "person.crop.rectangle.stack",
                    isActive: section == .review
                ) { section = .review }
            }
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
                                ReEnrichMenu(
                                    onReEnrich: { ops in
                                        browseState.reEnrich(operations: ops)
                                    },
                                    whisperEnabled: browseState.whisperEnabled,
                                )
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

    /// One non-library sidebar row (People, Review Clusters, …). Styled
    /// to roughly match the macOS `.listStyle(.sidebar)` row metrics so
    /// the top entries don't visually clash with the library list below.
    @ViewBuilder
    private func sidebarRow(
        label: String,
        icon: String,
        isActive: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .frame(width: 16)
                Text(label)
                Spacer()
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(
                isActive ? Color.accentColor.opacity(0.2) : Color.clear
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}
