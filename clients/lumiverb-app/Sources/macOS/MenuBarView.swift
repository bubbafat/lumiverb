import SwiftUI
import LumiverbKit

struct MenuBarView: View {
    @ObservedObject var appState: AppState

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
            Text(user.email)
                .font(.caption)
                .foregroundColor(.secondary)
        }

        Divider()

        if appState.libraries.isEmpty {
            Text("No libraries")
                .foregroundColor(.secondary)
        } else {
            ForEach(appState.libraries) { lib in
                Label(lib.name, systemImage: "folder")
                    .font(.body)
            }
        }

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
