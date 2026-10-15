import SwiftUI
import LumiverbKit

@main
struct LumiverbApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var appState = AppState()
    @Environment(\.openWindow) private var openWindow

    var body: some Scene {
        // The menu bar's icon is fixed: this app scans nothing (the
        // server's scheduler scans every library), so there's no sync
        // state to show there.
        MenuBarExtra("Lumiverb", systemImage: "photo.stack") {
            MenuBarView(appState: appState, openBrowseWindow: {
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
                // ADR-015 M1: install the macOS cache bundle and scroll
                // accessor as environment values so views moved into
                // LumiverbKit (starting with AuthenticatedImageView) can
                // resolve them without importing AppKit. M2 will move
                // the grid views and have them consume `\.scrollAccessor`
                // directly; for now this is wired but unused by the grids.
                .environment(\.cacheBundle, appState.cacheBundle)
                .environment(\.scrollAccessor, appState.scrollAccessor)
                .onDisappear {
                    // Return to accessory mode when browse window closes
                    NSApp.setActivationPolicy(.accessory)
                }
        }
        .defaultSize(width: 1200, height: 800)

        // Settings window (opened from menu bar)
        Settings {
            SettingsView(appState: appState)
                .environment(\.cacheBundle, appState.cacheBundle)
        }
    }
}

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        // Hide dock icon — menu bar only (until browse window opens)
        NSApp.setActivationPolicy(.accessory)
    }
}
