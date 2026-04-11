import SwiftUI

struct LoginView: View {
    @ObservedObject var appState: iOSAppState
    @State private var serverURL: String = ""
    @State private var email: String = ""
    @State private var password: String = ""

    var body: some View {
        NavigationStack {
            Form {
                Section("Server") {
                    TextField("Server URL", text: $serverURL, prompt: Text("https://app.lumiverb.io"))
                        .textContentType(.URL)
                        .autocapitalization(.none)
                        .keyboardType(.URL)
                        .foregroundColor(serverURL.isEmpty ? .secondary : .primary)
                }

                Section("Credentials") {
                    TextField("Email", text: $email)
                        .textContentType(.emailAddress)
                        .autocapitalization(.none)
                        .keyboardType(.emailAddress)
                    SecureField("Password", text: $password)
                        .textContentType(.password)
                }

                Section {
                    Button(action: {
                        appState.configure(serverURL: serverURL)
                        Task {
                            await appState.login(email: email, password: password)
                        }
                    }) {
                        if appState.isConnecting {
                            ProgressView()
                                .frame(maxWidth: .infinity)
                        } else {
                            Text("Connect")
                                .frame(maxWidth: .infinity)
                        }
                    }
                    .disabled(serverURL.isEmpty || email.isEmpty || password.isEmpty || appState.isConnecting)
                }

                if let error = appState.connectionError {
                    Section {
                        Text(error)
                            .foregroundColor(.red)
                    }
                }
            }
            .navigationTitle("Lumiverb")
            .onAppear {
                serverURL = appState.serverURL
            }
            .task {
                if !appState.serverURL.isEmpty {
                    appState.configure(serverURL: appState.serverURL)
                    await appState.tryRestoreSession()
                }
            }
        }
    }
}
