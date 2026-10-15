import SwiftUI
import LumiverbKit

struct MenuBarView: View {
    @ObservedObject var appState: AppState
    var openBrowseWindow: (() -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if appState.isAuthenticated {
                authenticatedView
            } else {
                unauthenticatedView
            }
        }
        .padding()
        .frame(width: 300)
        // The app's one session restore: the browse window opens from
        // here, and uses the session this makes.
        .task {
            if !appState.serverURL.isEmpty && !appState.isAuthenticated {
                appState.configure(serverURL: appState.serverURL)
                await appState.tryRestoreSession()
            }
        }
    }

    // MARK: - Authenticated

    @ViewBuilder
    private var authenticatedView: some View {
        HStack {
            Image(systemName: "checkmark.circle.fill")
                .foregroundColor(.green)
            Text("Connected")
                .font(.headline)
        }

        if let user = appState.currentUser {
            Text(user.displayName)
                .font(.caption)
                .foregroundColor(.secondary)
        }

        // Favorites: only render the section if the user has actually
        // favorited something. The previous design always listed every
        // library here as static dead text — visually noisy and didn't
        // scale past a handful of libraries. Favoriting is opt-in via the
        // sidebar context menu.
        let favorites = appState.favoriteLibraries
        if !favorites.isEmpty {
            Divider()
            ForEach(favorites) { lib in
                Button {
                    // Open the browse window scoped to this library.
                    // BrowseWindow consumes pendingSelectedLibraryId on
                    // appear / onChange and clears it.
                    appState.pendingSelectedLibraryId = lib.libraryId
                    openBrowseWindow?()
                } label: {
                    Label(lib.name, systemImage: "star.fill")
                }
            }
        }

        Divider()

        Button("Open Lumiverb") {
            openBrowseWindow?()
        }
        .keyboardShortcut("o")

        Divider()

        Button("Log Out") {
            Task { await appState.logout() }
        }

        SettingsLink {
            Text("Settings...")
        }

        Divider()

        Button("Quit Lumiverb") {
            NSApplication.shared.terminate(nil)
        }
        .keyboardShortcut("q")
    }

    // MARK: - Unauthenticated

    @ViewBuilder
    private var unauthenticatedView: some View {
        if appState.serverURL.isEmpty {
            Text("Not configured")
                .font(.headline)
            Text("Open Settings to configure your server URL.")
                .font(.caption)
                .foregroundColor(.secondary)
        } else if appState.isConnecting {
            HStack {
                ProgressView()
                    .controlSize(.small)
                Text("Connecting...")
            }
        } else {
            HStack {
                Image(systemName: "xmark.circle.fill")
                    .foregroundColor(.red)
                Text("Not connected")
                    .font(.headline)
            }

            if let error = appState.connectionError {
                Text(error)
                    .font(.caption)
                    .foregroundColor(.red)
                    .textSelection(.enabled)
                Button("Copy Error") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(error, forType: .string)
                }
                .controlSize(.small)
            }
        }

        Divider()

        SettingsLink {
            Text("Settings...")
        }

        Divider()

        Button("Quit Lumiverb") {
            NSApplication.shared.terminate(nil)
        }
        .keyboardShortcut("q")
    }
}
