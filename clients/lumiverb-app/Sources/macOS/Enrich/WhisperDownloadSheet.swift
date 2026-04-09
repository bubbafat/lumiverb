import SwiftUI

/// Modal sheet that surfaces `WhisperModelManager.downloadState` while a
/// model is downloading. Auto-dismisses on `.idle`. The user can cancel
/// mid-download or acknowledge a completion / failure / cancellation.
///
/// Presentation pattern: SettingsView binds `whisperShowDownloadSheet` and
/// passes the shared manager. Once the manager transitions to a terminal
/// state and the user clicks Done, the manager state is reset and the
/// sheet closes.
struct WhisperDownloadSheet: View {
    @ObservedObject var manager: WhisperModelManager
    let onDismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            header

            Divider()

            content

            Spacer()

            HStack {
                Spacer()
                buttonRow
            }
        }
        .padding(24)
        .frame(width: 460, height: 240)
    }

    @ViewBuilder
    private var header: some View {
        switch manager.downloadState {
        case .idle:
            Label("Whisper Model", systemImage: "waveform")
                .font(.headline)
        case .downloading(let size, _, _):
            Label("Downloading whisper-\(size.displayName)", systemImage: "arrow.down.circle")
                .font(.headline)
        case .completed(let size):
            Label("whisper-\(size.displayName) ready", systemImage: "checkmark.circle.fill")
                .foregroundColor(.green)
                .font(.headline)
        case .failed(let size, _):
            Label("whisper-\(size.displayName) failed", systemImage: "xmark.octagon.fill")
                .foregroundColor(.red)
                .font(.headline)
        case .cancelled(let size):
            Label("whisper-\(size.displayName) cancelled", systemImage: "minus.circle")
                .foregroundColor(.orange)
                .font(.headline)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch manager.downloadState {
        case .idle:
            Text("No download in progress.")
                .foregroundColor(.secondary)

        case .downloading(_, let received, let expected):
            VStack(alignment: .leading, spacing: 8) {
                ProgressView(value: progressFraction(received: received, expected: expected))
                HStack {
                    Text("\(formatBytes(received)) of \(formatBytes(expected))")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    Spacer()
                    Text(percentString(received: received, expected: expected))
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .monospacedDigit()
                }
                Text("This is a one-time setup. The model is downloaded once and reused for all future transcription runs.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

        case .completed(let size):
            VStack(alignment: .leading, spacing: 8) {
                Text("Model installed at:")
                    .font(.caption)
                Text(WhisperModelManager.modelURL(for: size).path)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .textSelection(.enabled)
                Text("You're ready to run “Re-enrich → Generate Transcripts” on your videos.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

        case .failed(_, let message):
            VStack(alignment: .leading, spacing: 8) {
                Text("Download failed:")
                    .font(.caption)
                Text(message)
                    .font(.caption)
                    .foregroundColor(.red)
                    .textSelection(.enabled)
                Text("Check your network connection and try again.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

        case .cancelled:
            Text("Download cancelled. Click Save in Settings to retry.")
                .font(.caption)
                .foregroundColor(.secondary)
        }
    }

    @ViewBuilder
    private var buttonRow: some View {
        switch manager.downloadState {
        case .idle:
            Button("Done") { onDismiss() }
                .keyboardShortcut(.defaultAction)

        case .downloading:
            Button("Cancel", role: .cancel) {
                manager.cancelDownload()
            }
            .keyboardShortcut(.cancelAction)

        case .completed, .failed, .cancelled:
            Button("Done") {
                manager.acknowledgeTerminalState()
                onDismiss()
            }
            .keyboardShortcut(.defaultAction)
        }
    }

    // MARK: - Formatting helpers

    private func progressFraction(received: Int64, expected: Int64) -> Double {
        guard expected > 0 else { return 0 }
        return min(1.0, Double(received) / Double(expected))
    }

    private func percentString(received: Int64, expected: Int64) -> String {
        let pct = Int(progressFraction(received: received, expected: expected) * 100)
        return "\(pct)%"
    }

    private func formatBytes(_ bytes: Int64) -> String {
        let formatter = ByteCountFormatter()
        formatter.allowedUnits = [.useMB, .useGB]
        formatter.countStyle = .file
        return formatter.string(fromByteCount: bytes)
    }
}
