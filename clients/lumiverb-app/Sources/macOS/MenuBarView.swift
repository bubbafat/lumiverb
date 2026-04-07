import SwiftUI
import LumiverbKit

struct MenuBarView: View {
    @ObservedObject var appState: AppState
    @ObservedObject var scanState: ScanState
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
        .task {
            if !appState.serverURL.isEmpty && !appState.isAuthenticated {
                appState.configure(serverURL: appState.serverURL)
                await appState.tryRestoreSession()
            }
        }
        .onChange(of: appState.isAuthenticated) { _, isAuth in
            if isAuth {
                scanState.startWatching()
            } else {
                scanState.stopWatching()
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

        Divider()

        // Scan status
        scanStatusSection

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

        Button("Open Lumiverb") {
            openBrowseWindow?()
        }
        .keyboardShortcut("o")

        Divider()

        Button("Log Out") {
            scanState.stopWatching()
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

    // MARK: - Scan status

    @ViewBuilder
    private var scanStatusSection: some View {
        HStack {
            if scanState.isScanning {
                ProgressView()
                    .controlSize(.small)
            } else if scanState.isWatching {
                Image(systemName: "eye")
                    .foregroundColor(.blue)
            }
            Text(scanState.statusText)
                .font(.caption)
                .foregroundColor(.secondary)
        }

        if scanState.isScanning {
            // Only show progress bar during actual processing
            if scanState.totalFiles > 0 && scanState.phase == "processing" {
                ProgressView(
                    value: Double(scanState.processedFiles),
                    total: Double(scanState.totalFiles)
                )
                .progressViewStyle(.linear)
            }

            HStack(spacing: 8) {
                if scanState.isPaused {
                    Button("Resume") { scanState.resumeScanning() }
                        .controlSize(.small)
                } else {
                    Button("Pause") { scanState.pauseScanning() }
                        .controlSize(.small)
                        .keyboardShortcut("p")
                }
                Button("Cancel") { scanState.cancelScanning() }
                    .controlSize(.small)
            }
        } else {
            Button("Scan Now") { scanState.scanAllLibraries() }
                .controlSize(.small)
                .keyboardShortcut("s")
                .disabled(appState.libraries.isEmpty)
        }

        if scanState.errorCount > 0 {
            Text("\(scanState.errorCount) errors")
                .font(.caption2)
                .foregroundColor(.red)
            Text(scanState.lastError)
                .font(.caption2)
                .foregroundColor(.red)
                .lineLimit(3)
                .textSelection(.enabled)
            Button("Copy Error") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(scanState.lastError, forType: .string)
            }
            .controlSize(.small)
        }

        if let lastScan = scanState.lastScanDate {
            Text("Last scan: \(lastScan, style: .relative) ago")
                .font(.caption2)
                .foregroundColor(.secondary)
        }

        if let error = scanState.scanError {
            Text(error)
                .font(.caption2)
                .foregroundColor(.red)
        }
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
