import SwiftUI
import LumiverbKit

/// Settings tab: user info, server, cache management, logout.
struct iOSSettingsView: View {
    @ObservedObject var appState: iOSAppState

    var body: some View {
        List {
            Section("Account") {
                if let user = appState.currentUser {
                    Label(user.displayName, systemImage: "person.circle.fill")
                    Label(appState.serverURL, systemImage: "server.rack")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }

            Section("Storage") {
                Button("Clear Thumbnail Cache") {
                    IOSThumbnailDiskCache().removeAll()
                }
            }

            Section {
                Button("Log Out", role: .destructive) {
                    Task { await appState.logout() }
                }
            }
        }
    }
}
