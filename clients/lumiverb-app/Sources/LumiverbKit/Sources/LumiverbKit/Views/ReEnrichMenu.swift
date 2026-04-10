import SwiftUI

/// Reusable context/dropdown menu for re-enrichment operations.
///
/// Used in three places:
/// - Library sidebar (right-click library row)
/// - Directory tree (right-click folder row)
/// - Lightbox metadata sidebar (actions dropdown)
public struct ReEnrichMenu: View {
    public let onReEnrich: (Set<EnrichmentOperation>) -> Void
    /// When false, the transcription option is shown but greyed out so
    /// users can discover the feature without it doing anything until they
    /// enable whisper in Settings.
    public var whisperEnabled: Bool = false

    public init(
        onReEnrich: @escaping (Set<EnrichmentOperation>) -> Void,
        whisperEnabled: Bool = false
    ) {
        self.onReEnrich = onReEnrich
        self.whisperEnabled = whisperEnabled
    }

    public var body: some View {
        Menu {
            Button("Detect Faces") {
                onReEnrich([.faces])
            }
            Button("Generate Embeddings") {
                onReEnrich([.embeddings])
            }
            Button("Extract Text") {
                onReEnrich([.ocr])
            }
            Button("Generate Descriptions") {
                onReEnrich([.vision])
            }
            Button("Generate Preview") {
                onReEnrich([.videoPreview])
            }
            Button(transcriptLabel) {
                onReEnrich([.transcribe])
            }
            .disabled(!whisperEnabled)
            Divider()
            Button("All") {
                let ops: Set<EnrichmentOperation> = whisperEnabled
                    ? Set(EnrichmentOperation.allCases)
                    : Set(EnrichmentOperation.allCases).subtracting([.transcribe])
                onReEnrich(ops)
            }
        } label: {
            Label("Re-enrich", systemImage: "arrow.triangle.2.circlepath")
        }
    }

    private var transcriptLabel: String {
        whisperEnabled ? "Generate Transcripts" : "Generate Transcripts (enable in Settings)"
    }
}
