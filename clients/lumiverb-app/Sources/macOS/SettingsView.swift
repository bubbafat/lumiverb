import SwiftUI

struct SettingsView: View {
    @ObservedObject var appState: AppState
    @State private var serverURL: String = ""
    @State private var email: String = ""
    @State private var password: String = ""
    @State private var visionApiUrl: String = ""
    @State private var visionApiKey: String = ""
    @State private var visionModelId: String = ""

    var body: some View {
        Form {
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

                    Section {
                        TextField("API URL", text: $visionApiUrl, prompt: Text("http://localhost:1234/v1"))
                            .textFieldStyle(.roundedBorder)
                        SecureField("API Key", text: $visionApiKey, prompt: Text("Optional"))
                            .textFieldStyle(.roundedBorder)
                        TextField("Model ID", text: $visionModelId, prompt: Text("Auto-detected if blank"))
                            .textFieldStyle(.roundedBorder)

                        if !appState.tenantVisionApiUrl.isEmpty {
                            Text("Tenant default: \(appState.tenantVisionApiUrl)")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                        if !appState.tenantVisionModelId.isEmpty {
                            Text("Tenant model: \(appState.tenantVisionModelId)")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }

                        HStack {
                            Button("Save") {
                                appState.visionApiUrl = visionApiUrl
                                appState.visionApiKey = visionApiKey
                                appState.visionModelId = visionModelId
                                appState.saveVisionConfig()
                            }

                            if appState.isVisionConfigured {
                                Label("Configured", systemImage: "checkmark.circle.fill")
                                    .foregroundColor(.green)
                                    .font(.caption)
                            }
                        }
                    } header: {
                        Text("Vision API")
                    } footer: {
                        Text("OpenAI-compatible endpoint for image descriptions. Leave blank to use tenant defaults.")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 400, height: 500)
        .padding()
        .onAppear {
            visionApiUrl = appState.visionApiUrl
            visionApiKey = appState.visionApiKey
            visionModelId = appState.visionModelId
        }
    }
}
