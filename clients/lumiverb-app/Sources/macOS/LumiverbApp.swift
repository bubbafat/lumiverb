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
        MenuBarExtra("Lumiverb", systemImage: "photo.stack") {
            MenuBarView(appState: appState, scanState: scanState, openBrowseWindow: {
                // Show dock icon when browse window opens
                NSApp.setActivationPolicy(.regular)
                openWindow(id: "browse")
                NSApp.activate(ignoringOtherApps: true)
            })
        }
        .menuBarExtraStyle(.window)

        // Browse window
        Window("Lumiverb", id: "browse") {
            BrowseWindow(appState: appState)
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
