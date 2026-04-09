import SwiftUI
import LumiverbKit

/// Reusable context/dropdown menu for re-enrichment operations.
///
/// Used in three places:
/// - Library sidebar (right-click library row)
/// - Directory tree (right-click folder row)
/// - Lightbox metadata sidebar (actions dropdown)
struct ReEnrichMenu: View {
    let onReEnrich: (Set<EnrichmentOperation>) -> Void
    /// When false, the transcription option is shown but greyed out so
    /// users can discover the feature without it doing anything until they
    /// enable whisper in Settings.
    var whisperEnabled: Bool = false

    var body: some View {
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
