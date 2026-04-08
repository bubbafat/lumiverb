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
            Divider()
            Button("All") {
                onReEnrich(Set(EnrichmentOperation.allCases))
            }
        } label: {
            Label("Re-enrich", systemImage: "arrow.triangle.2.circlepath")
        }
    }
}
