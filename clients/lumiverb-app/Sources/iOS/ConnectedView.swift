import SwiftUI
import LumiverbKit

struct ConnectedView: View {
    @ObservedObject var appState: iOSAppState

    var body: some View {
        NavigationStack {
            List {
                Section("Status") {
                    Label("Connected", systemImage: "checkmark.circle.fill")
                        .foregroundColor(.green)
                    if let user = appState.currentUser {
                        Text(user.displayName)
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }

                Section("Libraries") {
                    if appState.libraries.isEmpty {
                        Text("No libraries")
                            .foregroundColor(.secondary)
                    } else {
                        ForEach(appState.libraries) { lib in
                            Label(lib.name, systemImage: "folder")
                        }
                    }
                }

                Section {
                    Button("Log Out", role: .destructive) {
                        Task { await appState.logout() }
                    }
                }
            }
            .navigationTitle("Lumiverb")
        }
    }
}
