import SwiftUI
import LumiverbKit
import AppKit

/// Modal sheet for creating a new library. Collects a display name and a
/// local folder (picked via `NSOpenPanel`), POSTs to `/v1/libraries`, then
/// reports the created library id to the caller so it can be selected and
/// scanning can kick off.
///
/// Keeps its own in-flight / error state so the sheet can stay open and
/// show the server's error message (409 on duplicate name is the common
/// one) without a toast-and-dismiss pattern losing the user's input.
struct NewLibrarySheet: View {
    @ObservedObject var appState: AppState

    /// Invoked on successful create with the new library id so the
    /// caller can select it and trigger a scan.
    let onCreated: (String) -> Void

    @Environment(\.dismiss) private var dismiss

    @State private var name: String = ""
    @State private var rootPath: String = ""
    @State private var isSubmitting = false
    @State private var errorMessage: String?

    private var canSubmit: Bool {
        !name.trimmingCharacters(in: .whitespaces).isEmpty
            && !rootPath.isEmpty
            && !isSubmitting
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("New Library")
                .font(.headline)

            VStack(alignment: .leading, spacing: 6) {
                Text("Name")
                    .font(.subheadline)
                    .foregroundColor(.secondary)
                TextField("My Photos", text: $name)
                    .textFieldStyle(.roundedBorder)
                    .disabled(isSubmitting)
            }

            VStack(alignment: .leading, spacing: 6) {
                Text("Source folder")
                    .font(.subheadline)
                    .foregroundColor(.secondary)
                HStack {
                    TextField("/Users/you/Pictures", text: $rootPath)
                        .textFieldStyle(.roundedBorder)
                        .disabled(true) // always pick via NSOpenPanel
                    Button("Choose…") { chooseFolder() }
                        .disabled(isSubmitting)
                }
            }

            if let errorMessage {
                Text(errorMessage)
                    .font(.callout)
                    .foregroundColor(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .disabled(isSubmitting)
                Button(isSubmitting ? "Creating…" : "Create") {
                    Task { await submit() }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(!canSubmit)
            }
        }
        .padding(20)
        .frame(minWidth: 420)
    }

    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.canCreateDirectories = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        panel.message = "Select the folder Lumiverb should index as this library."
        if panel.runModal() == .OK, let url = panel.url {
            rootPath = url.path
            // Autofill an empty name from the folder name for convenience.
            if name.trimmingCharacters(in: .whitespaces).isEmpty {
                name = url.lastPathComponent
            }
        }
    }

    private func submit() async {
        errorMessage = nil
        isSubmitting = true
        defer { isSubmitting = false }

        do {
            let created = try await appState.createLibrary(
                name: name.trimmingCharacters(in: .whitespaces),
                rootPath: rootPath
            )
            onCreated(created.libraryId)
            dismiss()
        } catch let error as APIError {
            switch error {
            case .serverError(_, let message):
                errorMessage = message
            case .networkError(let message):
                errorMessage = "Network error: \(message)"
            case .unauthorized(let message):
                errorMessage = "Unauthorized: \(message)"
            case .noToken:
                errorMessage = "Not signed in."
            default:
                errorMessage = "Failed to create library."
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
