import SwiftUI
import LumiverbKit

@main
struct LumiverbiOSApp: App {
    @StateObject private var appState = iOSAppState()

    var body: some Scene {
        WindowGroup {
            if appState.isAuthenticated {
                ConnectedView(appState: appState)
            } else {
                LoginView(appState: appState)
            }
        }
    }
}
