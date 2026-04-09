import SwiftUI
import LumiverbKit

@main
struct LumiverbApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var appState = AppState()
    @StateObject private var scanState: ScanState
    @Environment(\.openWindow) private var openWindow

    init() {
        let state = AppState()
        _appState = StateObject(wrappedValue: state)
        _scanState = StateObject(wrappedValue: ScanState(appState: state))
    }

    var body: some Scene {
        MenuBarExtra {
            MenuBarView(appState: appState, scanState: scanState, openBrowseWindow: {
                // Show dock icon when browse window opens
                NSApp.setActivationPolicy(.regular)
                openWindow(id: "browse")
                NSApp.activate(ignoringOtherApps: true)
            })
        } label: {
            MenuBarLabel(scanState: scanState)
        }
        .menuBarExtraStyle(.window)

        // Browse window
        Window("Lumiverb", id: "browse") {
            BrowseWindow(appState: appState, scanState: scanState)
                .frame(minWidth: 800, minHeight: 500)
                .onDisappear {
                    // Return to accessory mode when browse window closes
                    NSApp.setActivationPolicy(.accessory)
                }
        }
        .defaultSize(width: 1200, height: 800)

        // Settings window (opened from menu bar)
        Settings {
            SettingsView(appState: appState)
        }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Hide dock icon — menu bar only (until browse window opens)
        NSApp.setActivationPolicy(.accessory)
    }
}

/// Three-state menu bar icon: paused / idle / active. macOS menu bar items
/// are template-rendered (monochrome) by default — `symbolRenderingMode`
/// and color modifiers don't survive into the menu bar appearance — so we
/// use three visually distinct SF symbols instead of relying on color:
///
/// - **paused**: `pause.rectangle.fill`
/// - **active** (currently scanning): `arrow.triangle.2.circlepath` with the
///   built-in symbol effect rotation
/// - **idle** (watching, healthy): `photo.stack`
///
/// The label is reactive — `@ObservedObject` on `ScanState` ensures the
/// icon updates whenever `isPaused` / `isScanning` change.
private struct MenuBarLabel: View {
    @ObservedObject var scanState: ScanState

    var body: some View {
        Image(systemName: iconName)
            .help(scanState.statusText)
    }

    private var iconName: String {
        if scanState.isPaused { return "pause.rectangle.fill" }
        if scanState.isScanning { return "arrow.triangle.2.circlepath" }
        return "photo.stack"
    }
}
