import SwiftUI
import LumiverbKit

/// Sidebar listing all libraries with selection and directory tree, plus
/// a top-level "People" entry that switches the detail panel into the
/// People browse view (Phase 6 M3 of ADR-014).
struct LibrarySidebar: View {
    let libraries: [Library]
    @ObservedObject var browseState: BrowseState
    @ObservedObject var appState: AppState
    @ObservedObject var scanState: ScanState
    @Binding var section: SidebarSection

    /// Presents the `NewLibrarySheet`. Scoped here because the sheet needs
    /// to be anchored on a view that survives across a selection change.
    @State private var showNewLibrarySheet = false

    /// Library the user opened settings for. Nil = sheet closed. Stored by
    /// value so renaming/re-rooting a library mid-sheet doesn't fight with
    /// the library list refreshing underneath us.
    @State private var settingsLibrary: Library?

    var body: some View {
        VStack(spacing: 0) {
            // Top-level section entries. Sit outside the libraries List
            // because that List uses `selectedLibraryId` as its selection
            // model and adding non-library rows would force the two
            // selections through a compound enum. Plain Buttons styled
            // like sidebar rows are the smaller change.
            //
            // "Library" is a dedicated affordance to return to the
            // library browse pane from People/Review without clicking a
            // library row — it used to work via a `.simultaneousGesture`
            // on each library row, but that was eating clicks on the
            // `Label`'s text on macOS (NSTableView's click path doesn't
            // compose with SwiftUI gestures on interactive child views).
            VStack(spacing: 0) {
                sidebarRow(
                    label: "Library",
                    icon: "photo.on.rectangle.angled",
                    isActive: section == .library
                ) { section = .library }

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

            // Library list (owns selection). Sorted favorites-first,
            // then non-favorites; both groups alpha by name. Favorites
            // get a trailing star to match the menu bar dropdown's
            // `star.fill` icon.
            List(selection: $browseState.selectedLibraryId) {
                Section {
                    ForEach(sortedLibraries) { lib in
                        // Plain HStack instead of Label so we can put a
                        // trailing star. Static layout — no gesture
                        // modifiers — so NSTableView's native click
                        // handling for `List(selection:)` continues to
                        // work. ⚠️  Do NOT add `.simultaneousGesture`
                        // or `.onTapGesture` here — both eat clicks on
                        // the row text on macOS. The List handles the
                        // click natively; `.onChange(of: selectedLibraryId)`
                        // below restores `section = .library` on any
                        // real selection change. The "same library
                        // tapped while in People" case is handled by
                        // the dedicated "Library" entry at the top of
                        // the sidebar.
                        HStack(spacing: 6) {
                            Image(systemName: "folder.fill")
                                .frame(width: 16)
                            Text(lib.name)
                            Spacer(minLength: 4)
                            if appState.isFavoriteLibrary(lib.libraryId) {
                                Image(systemName: "star.fill")
                                    .font(.caption)
                                    .foregroundColor(.yellow)
                            }
                        }
                            .tag(lib.libraryId)
                            .contextMenu {
                                Button(
                                    appState.isFavoriteLibrary(lib.libraryId)
                                        ? "Remove from Favorites"
                                        : "Add to Favorites"
                                ) {
                                    appState.toggleFavoriteLibrary(lib.libraryId)
                                }
                                Divider()
                                Button("Library Settings…") {
                                    settingsLibrary = lib
                                }
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
                } header: {
                    HStack(spacing: 4) {
                        Text("Libraries")
                        Spacer()
                        Button {
                            showNewLibrarySheet = true
                        } label: {
                            Image(systemName: "plus")
                                .font(.caption)
                        }
                        .buttonStyle(.borderless)
                        .help("Add a new library")
                    }
                }
            }
            .listStyle(.sidebar)
            .frame(maxHeight: libraries.count <= 3 ? 120 : 200)
            .sheet(isPresented: $showNewLibrarySheet) {
                NewLibrarySheet(appState: appState) { newLibraryId in
                    // Select the new library immediately and restart the
                    // scanner so the LibraryWatcher picks up the new root
                    // path (it captures paths at watch time; stopping and
                    // starting rebinds it). Then kick a manual scan so the
                    // user sees immediate progress instead of waiting for
                    // a filesystem event.
                    browseState.selectedLibraryId = newLibraryId
                    section = .library
                    if scanState.isWatching {
                        scanState.stopWatching()
                        scanState.startWatching()
                    }
                    scanState.scanAllLibraries()
                }
            }
            .sheet(item: $settingsLibrary) { lib in
                LibrarySettingsSheet(appState: appState, library: lib) { changed in
                    // Renaming or re-rooting a library invalidates any cached
                    // directory tree for the currently selected library, and
                    // changes the LibraryWatcher's set of root paths. Restart
                    // the watcher (captured at watch time — see startWatching)
                    // and ask BrowseState to re-load directories if this is
                    // the selected one.
                    guard changed else { return }
                    if scanState.isWatching {
                        scanState.stopWatching()
                        scanState.startWatching()
                    }
                    if browseState.selectedLibraryId == lib.libraryId {
                        Task { await browseState.loadRootDirectories() }
                    }
                }
            }
            .onChange(of: browseState.selectedLibraryId) { _, newValue in
                // Belt-and-braces: a real selection change also pops us back.
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

    /// Libraries sorted favorites-first, then non-favorites; both groups
    /// alpha by name (case-insensitive, locale-aware so accented names
    /// like "École" sort sensibly). Recomputed on every render — driven
    /// by `appState.favoriteLibraryIds` (`@Published`) so toggling a
    /// favorite via the context menu reorders the sidebar live.
    private var sortedLibraries: [Library] {
        let favoriteIds = appState.favoriteLibraryIds
        let alphaCmp: (Library, Library) -> Bool = {
            $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending
        }
        let favorites = libraries
            .filter { favoriteIds.contains($0.libraryId) }
            .sorted(by: alphaCmp)
        let others = libraries
            .filter { !favoriteIds.contains($0.libraryId) }
            .sorted(by: alphaCmp)
        return favorites + others
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
