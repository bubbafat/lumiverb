import SwiftUI

struct SettingsView: View {
    @ObservedObject var appState: AppState
    @State private var serverURL: String = ""
    @State private var email: String = ""
    @State private var password: String = ""
    @State private var visionApiUrl: String = ""
    @State private var visionApiKey: String = ""
    @State private var visionModelId: String = ""
    @State private var availableModels: [String] = []
    @State private var isTesting = false
    @State private var visionTestError: String?
    @State private var visionTestSuccess = false

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
                            .onChange(of: visionApiUrl) { _, _ in
                                // Reset test state when URL changes
                                visionTestSuccess = false
                                visionTestError = nil
                                availableModels = []
                                visionModelId = ""
                            }

                        SecureField("API Key", text: $visionApiKey, prompt: Text("Optional"))
                            .textFieldStyle(.roundedBorder)

                        HStack {
                            Button(isTesting ? "Testing..." : "Test Connection") {
                                testConnection()
                            }
                            .disabled(resolvedUrl.isEmpty || isTesting)

                            if visionTestSuccess {
                                Label("Connected", systemImage: "checkmark.circle.fill")
                                    .foregroundColor(.green)
                                    .font(.caption)
                            }
                        }

                        if let error = visionTestError {
                            Text(error)
                                .foregroundColor(.red)
                                .font(.caption)
                                .textSelection(.enabled)
                        }

                        if availableModels.count > 1 {
                            Picker("Model", selection: $visionModelId) {
                                ForEach(availableModels, id: \.self) { model in
                                    Text(model).tag(model)
                                }
                            }
                        } else if !visionModelId.isEmpty {
                            HStack {
                                Text("Model:")
                                    .foregroundColor(.secondary)
                                Text(visionModelId)
                            }
                            .font(.caption)
                        }

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
                            .disabled(resolvedUrl.isEmpty || visionModelId.isEmpty)

                            if appState.isVisionConfigured {
                                Label("Configured", systemImage: "checkmark.circle.fill")
                                    .foregroundColor(.green)
                                    .font(.caption)
                            }
                        }
                    } header: {
                        Text("Vision API")
                    } footer: {
                        Text("OpenAI-compatible endpoint for image descriptions. Enter the URL and test to discover available models.")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 400, height: 550)
        .padding()
        .onAppear {
            visionApiUrl = appState.visionApiUrl
            visionApiKey = appState.visionApiKey
            visionModelId = appState.visionModelId
        }
    }

    /// The URL to use for testing — local override or tenant default.
    private var resolvedUrl: String {
        visionApiUrl.isEmpty ? appState.tenantVisionApiUrl : visionApiUrl
    }

    private func testConnection() {
        let url = resolvedUrl
        guard !url.isEmpty else { return }

        isTesting = true
        visionTestError = nil
        visionTestSuccess = false

        Task {
            do {
                let models = try await VisionModelDiscovery.fetchModels(
                    apiURL: url,
                    apiKey: visionApiKey
                )
                availableModels = models
                if visionModelId.isEmpty, let first = models.first {
                    visionModelId = first
                }
                visionTestSuccess = true

                // Auto-save if we discovered models
                if !visionModelId.isEmpty {
                    appState.visionApiUrl = visionApiUrl
                    appState.visionApiKey = visionApiKey
                    appState.visionModelId = visionModelId
                    appState.saveVisionConfig()
                }
            } catch {
                visionTestError = "\(error)"
            }
            isTesting = false
        }
    }
}
