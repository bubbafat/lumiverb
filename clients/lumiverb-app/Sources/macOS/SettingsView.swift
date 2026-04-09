import SwiftUI

struct SettingsView: View {
    @ObservedObject var appState: AppState
    @StateObject private var whisperManager = WhisperModelManager.shared
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
    @State private var whisperEnabled: Bool = false
    @State private var whisperSliderIndex: Double = 2  // small (default)
    @State private var whisperLanguage: String = ""
    @State private var whisperBinaryPath: String = ""
    @State private var whisperShowAdvanced: Bool = false
    @State private var whisperShowDownloadSheet: Bool = false
    @State private var whisperShowDisableConfirm: Bool = false

    private static let whisperModelOrder: [WhisperModelManager.ModelSize] =
        [.tiny, .base, .small, .medium, .largeV3]

    private var selectedWhisperModelSize: WhisperModelManager.ModelSize {
        let i = max(0, min(Self.whisperModelOrder.count - 1, Int(whisperSliderIndex.rounded())))
        return Self.whisperModelOrder[i]
    }

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

                    whisperSection
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 400, height: 700)
        .padding()
        .onAppear {
            visionApiUrl = appState.visionApiUrl
            visionApiKey = appState.visionApiKey
            visionModelId = appState.visionModelId
            whisperEnabled = appState.whisperEnabled
            whisperLanguage = appState.whisperLanguage
            whisperBinaryPath = appState.whisperBinaryPath
            // Restore the slider position from the saved string size.
            if let idx = Self.whisperModelOrder.firstIndex(where: { $0.rawValue == appState.whisperModelSize }) {
                whisperSliderIndex = Double(idx)
            }
        }
        .sheet(isPresented: $whisperShowDownloadSheet) {
            WhisperDownloadSheet(
                manager: whisperManager,
                onDismiss: { whisperShowDownloadSheet = false },
            )
        }
        .confirmationDialog(
            "Disable whisper transcription?",
            isPresented: $whisperShowDisableConfirm,
            titleVisibility: .visible,
        ) {
            Button("Cancel", role: .cancel) {
                // Restore the toggle on cancel.
                whisperEnabled = true
            }
            Button("Disable & Keep Models") {
                appState.whisperEnabled = false
                appState.saveWhisperConfig()
            }
            Button("Disable & Delete Models", role: .destructive) {
                appState.whisperEnabled = false
                appState.saveWhisperConfig()
                WhisperModelManager.cleanup(keep: nil)
            }
        } message: {
            let mb = WhisperModelManager.totalInstalledBytes() / (1024 * 1024)
            if mb > 0 {
                Text("Downloaded models use about \(mb) MB of disk space.")
            } else {
                Text("No models on disk; nothing to clean up.")
            }
        }
    }

    // MARK: - Whisper section

    @ViewBuilder
    private var whisperSection: some View {
        Section {
            Toggle("Enable whisper transcription", isOn: $whisperEnabled)
                .onChange(of: whisperEnabled) { oldValue, newValue in
                    if !newValue && oldValue {
                        // The toggle just flipped off — defer the actual
                        // disable to the confirmation dialog. Restore the
                        // toggle visually until the user picks a path.
                        whisperEnabled = true
                        whisperShowDisableConfirm = true
                    }
                }

            Group {
                // Quality slider
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Text("Faster")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        Slider(
                            value: $whisperSliderIndex,
                            in: 0...Double(Self.whisperModelOrder.count - 1),
                            step: 1,
                        )
                        Text("Higher quality")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        Image(systemName: "info.circle")
                            .foregroundColor(.secondary)
                            .help(Self.modelSizeTooltip)
                    }
                    HStack {
                        Text("Model: \(selectedWhisperModelSize.displayName) (~\(selectedWhisperModelSize.approximateSizeMB) MB, \(selectedWhisperModelSize.qualityHint))")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        Spacer()
                    }
                }

                // Missing-model warning banner — surfaced WITHOUT auto-
                // triggering a download. The user has to click "Download
                // Now" explicitly.
                if appState.isWhisperEnabledButModelMissing {
                    HStack(spacing: 8) {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundColor(.orange)
                        VStack(alignment: .leading) {
                            Text("Model not downloaded")
                                .font(.caption)
                                .fontWeight(.semibold)
                            Text("Whisper is enabled but ggml-\(appState.whisperModelSize).bin isn't on disk yet.")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                        Spacer()
                        Button("Download Now") {
                            kickOffWhisperDownload()
                        }
                        .controlSize(.small)
                    }
                    .padding(.vertical, 4)
                }

                // Advanced disclosure: binary path override + language hint
                DisclosureGroup("Advanced", isExpanded: $whisperShowAdvanced) {
                    TextField(
                        "Language",
                        text: $whisperLanguage,
                        prompt: Text("auto-detect (e.g. en, es, fr)"),
                    )
                    .textFieldStyle(.roundedBorder)

                    TextField(
                        "Binary path override",
                        text: $whisperBinaryPath,
                        prompt: Text("auto-discover Homebrew whisper-cpp"),
                    )
                    .textFieldStyle(.roundedBorder)
                }
                .font(.caption)

                HStack {
                    Button("Save") { saveWhisperSettings() }
                    if appState.isWhisperConfigured {
                        Label("Configured", systemImage: "checkmark.circle.fill")
                            .foregroundColor(.green)
                            .font(.caption)
                    }
                    Spacer()
                }
            }
            .disabled(!whisperEnabled)
            .opacity(whisperEnabled ? 1.0 : 0.5)
        } header: {
            Text("Whisper Transcription")
        } footer: {
            Text("Speech-to-text for video transcripts. Requires whisper-cpp from Homebrew (`brew install whisper-cpp`). Models download automatically when you save. Disabled by default.")
                .font(.caption)
                .foregroundColor(.secondary)
        }
    }

    /// Apply enable / slider / language / binary changes to AppState, persist
    /// them, and kick off a download if the chosen model isn't already on
    /// disk and whisper is enabled.
    ///
    /// Critical: this is the ONLY path that turns whisper on. The disable
    /// path goes through the confirmation dialog and writes
    /// `appState.whisperEnabled = false` directly.
    private func saveWhisperSettings() {
        let chosen = selectedWhisperModelSize
        let oldSize = appState.whisperModelSize
        appState.whisperEnabled = whisperEnabled
        appState.whisperModelSize = chosen.rawValue
        appState.whisperLanguage = whisperLanguage
        appState.whisperBinaryPath = whisperBinaryPath
        appState.saveWhisperConfig()

        if !whisperEnabled { return }
        // Need to download if the model file isn't on disk OR the user
        // changed the slider to a size we don't have yet.
        let needsDownload = !WhisperModelManager.isModelInstalled(chosen)
        if needsDownload || oldSize != chosen.rawValue {
            kickOffWhisperDownload()
        }
    }

    private func kickOffWhisperDownload() {
        whisperShowDownloadSheet = true
        whisperManager.startDownload(selectedWhisperModelSize)
    }

    private static let modelSizeTooltip: String = """
    tiny     ~75 MB   fastest, roughest
    base     ~150 MB  fast
    small    ~500 MB  balanced (recommended)
    medium   ~1.5 GB  more accurate, slower
    large-v3 ~3.0 GB  best quality, slowest
    """

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
