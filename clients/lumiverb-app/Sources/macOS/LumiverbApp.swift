import SwiftUI
import LumiverbKit

@main
struct LumiverbApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var appState = AppState()

    var body: some Scene {
        MenuBarExtra("Lumiverb", systemImage: "photo.stack") {
            MenuBarView(appState: appState)
        }
        .menuBarExtraStyle(.window)

        // Settings window (opened from menu bar)
        Settings {
            SettingsView(appState: appState)
        }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Hide dock icon — menu bar only
        NSApp.setActivationPolicy(.accessory)
    }
}
