import SwiftUI
import ServiceManagement

struct SettingsView: View {
    @ObservedObject var appState: AppState
    @State private var serverURL: String = ""
    @State private var startAtLogin: Bool = false
    @State private var startAtLoginError: String?
    @State private var email: String = ""
    @State private var password: String = ""

    var body: some View {
        Form {
            Section("General") {
                // `.switch` toggleStyle (instead of the default `.checkbox`
                // that `Form` picks on macOS) so the on-state is filled
                // green and stays colored regardless of window focus.
                // The default checkbox loses its tint when the Settings
                // window blurs, which made the toggle's state easy to
                // misread.
                Toggle("Start Lumiverb at login", isOn: $startAtLogin)
                    .toggleStyle(.switch)
                    .tint(.green)
                    .onAppear { startAtLogin = SMAppService.mainApp.status == .enabled }
                    .onChange(of: startAtLogin) { _, newValue in
                        applyStartAtLogin(newValue)
                    }
                if let error = startAtLoginError {
                    Text(error)
                        .font(.caption)
                        .foregroundColor(.red)
                }
                Text("Lumiverb runs in the menu bar. Enable this to have it start automatically when you log in.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            Section("Server") {
                TextField("Server URL", text: $serverURL, prompt: Text("https://app.lumiverb.io"))
                    .textFieldStyle(.roundedBorder)
                    .onAppear { serverURL = appState.serverURL }
            }

            if !serverURL.isEmpty {
                Section("Login") {
                    TextField("Email", text: $email)
                        .textFieldStyle(.roundedBorder)
                        .textContentType(.emailAddress)
                    SecureField("Password", text: $password)
                        .textFieldStyle(.roundedBorder)
                        .textContentType(.password)

                    Button(appState.isConnecting ? "Connecting..." : "Connect") {
                        appState.configure(serverURL: serverURL)
                        Task {
                            await appState.login(email: email, password: password)
                            if appState.isAuthenticated {
                                password = ""
                            }
                        }
                    }
                    .disabled(email.isEmpty || password.isEmpty || appState.isConnecting)
                }

                if let error = appState.connectionError {
                    Text(error)
                        .foregroundColor(.red)
                        .font(.caption)
                        .textSelection(.enabled)
                    Button("Copy Error") {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(error, forType: .string)
                    }
                    .controlSize(.small)
                }

                if appState.isAuthenticated {
                    Section("Status") {
                        Label("Connected", systemImage: "checkmark.circle.fill")
                            .foregroundColor(.green)
                        if let user = appState.currentUser {
                            Text("Logged in as \(user.displayName) (\(user.role))")
                                .font(.caption)
                        }
                        Text("\(appState.libraries.count) library(ies)")
                            .font(.caption)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 400, height: 480)
        .padding()
    }

    /// Register or unregister the app as a login item via `SMAppService`.
    /// `SMAppService.mainApp` requires the app to be in `/Applications`
    /// for the login-item registration to actually fire on next reboot —
    /// dev builds running from DerivedData will accept the call but won't
    /// re-launch on login. We surface any thrown error inline rather than
    /// silently rolling back the toggle, so it's clear when this is the
    /// reason it didn't take effect.
    private func applyStartAtLogin(_ enabled: Bool) {
        startAtLoginError = nil
        do {
            if enabled {
                try SMAppService.mainApp.register()
            } else {
                try SMAppService.mainApp.unregister()
            }
        } catch {
            startAtLoginError = "Couldn't \(enabled ? "enable" : "disable") start at login: \(error.localizedDescription)"
            // Roll the toggle back to the actual system state so the UI
            // doesn't lie about what's persisted.
            startAtLogin = SMAppService.mainApp.status == .enabled
        }
    }
}
