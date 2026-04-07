import SwiftUI

struct SettingsView: View {
    @ObservedObject var appState: AppState
    @State private var serverURL: String = ""
    @State private var email: String = ""
    @State private var password: String = ""

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
                }

                if appState.isAuthenticated {
                    Section("Status") {
                        Label("Connected", systemImage: "checkmark.circle.fill")
                            .foregroundColor(.green)
                        if let user = appState.currentUser {
                            Text("Logged in as \(user.email) (\(user.role))")
                                .font(.caption)
                        }
                        Text("\(appState.libraries.count) library(ies)")
                            .font(.caption)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 400, height: 350)
        .padding()
    }
}
