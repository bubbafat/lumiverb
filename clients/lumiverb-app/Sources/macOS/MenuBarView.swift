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
            if scanState.isPaused {
                Image(systemName: "pause.circle.fill")
                    .foregroundColor(.secondary)
            } else if scanState.isScanning {
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
        }

        // Pause / Resume / Scan / Cancel — pause is a persistent toggle so
        // the controls are visible regardless of `isScanning`. Cancel only
        // makes sense mid-scan.
        HStack(spacing: 8) {
            if scanState.isPaused {
                Button("Resume Sync") { scanState.resumeSync() }
                    .controlSize(.small)
                    .keyboardShortcut("p")
            } else {
                Button("Pause Sync") { scanState.pauseSync() }
                    .controlSize(.small)
                    .keyboardShortcut("p")
            }

            if scanState.isScanning {
                Button("Cancel") { scanState.cancelScanning() }
                    .controlSize(.small)
            } else {
                Button("Sync Now") { scanState.scanAllLibraries() }
                    .controlSize(.small)
                    .keyboardShortcut("s")
                    .disabled(appState.libraries.isEmpty || scanState.isPaused)
            }
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
            Text("Last sync: \(lastScan, style: .relative) ago")
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
