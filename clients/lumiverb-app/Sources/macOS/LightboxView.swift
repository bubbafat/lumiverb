import SwiftUI
import LumiverbKit

/// Full-screen lightbox overlay showing proxy image + metadata sidebar.
struct LightboxView: View {
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    var body: some View {
        HStack(spacing: 0) {
            // Main image area
            ZStack {
                Color.black

                if browseState.isLoadingDetail {
                    ProgressView()
                        .tint(.white)
                } else if let assetId = browseState.selectedAssetId {
                    AuthenticatedImageView(
                        assetId: assetId,
                        client: client,
                        type: .proxy
                    )
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }

                // Navigation arrows
                HStack {
                    navigationButton(direction: -1, icon: "chevron.left")
                    Spacer()
                    navigationButton(direction: 1, icon: "chevron.right")
                }
                .padding(.horizontal, 8)

                // Close button
                VStack {
                    HStack {
                        Spacer()
                        Button {
                            browseState.closeLightbox()
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.title2)
                                .foregroundColor(.white.opacity(0.8))
                        }
                        .buttonStyle(.plain)
                        .padding(12)
                    }
                    Spacer()
                }
            }

            // Metadata sidebar
            if let detail = browseState.assetDetail {
                MetadataSidebar(
                    detail: detail,
                    onFindSimilar: {
                        Task { await browseState.findSimilar(assetId: detail.assetId) }
                    }
                )
                .frame(width: 300)
            }
        }
        .background(.black)
    }

    @ViewBuilder
    private func navigationButton(direction: Int, icon: String) -> some View {
        Button {
            browseState.navigateLightbox(direction: direction)
        } label: {
            Image(systemName: icon)
                .font(.title)
                .foregroundColor(.white.opacity(0.8))
                .padding(8)
                .background(.black.opacity(0.3))
                .clipShape(Circle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - Metadata Sidebar

struct MetadataSidebar: View {
    let detail: AssetDetail
    let onFindSimilar: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                // Filename
                Text(detail.filename)
                    .font(.headline)
                    .lineLimit(2)

                Divider()

                // Actions
                HStack(spacing: 12) {
                    Button {
                        onFindSimilar()
                    } label: {
                        Label("Find Similar", systemImage: "square.stack.3d.up")
                    }
                    .controlSize(.small)
                }

                // AI Description
                if let desc = detail.aiDescription, !desc.isEmpty {
                    metadataSection("Description") {
                        Text(desc)
                            .font(.callout)
                            .foregroundColor(.secondary)
                            .textSelection(.enabled)
                    }
                }

                // Tags
                if let tags = detail.aiTags, !tags.isEmpty {
                    metadataSection("Tags") {
                        FlowLayout(spacing: 4) {
                            ForEach(tags, id: \.self) { tag in
                                Text(tag)
                                    .font(.caption)
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 2)
                                    .background(Color.accentColor.opacity(0.15))
                                    .cornerRadius(4)
                            }
                        }
                    }
                }

                // Camera info
                if detail.cameraMake != nil || detail.iso != nil || detail.aperture != nil {
                    metadataSection("Camera") {
                        VStack(alignment: .leading, spacing: 4) {
                            if let camera = detail.cameraDescription {
                                metadataRow("Camera", camera)
                            }
                            if let lens = detail.lensModel {
                                metadataRow("Lens", lens)
                            }
                            if let iso = detail.iso {
                                metadataRow("ISO", "\(iso)")
                            }
                            if let exposure = detail.exposureDescription {
                                metadataRow("Exposure", exposure)
                            }
                            if let aperture = detail.aperture {
                                metadataRow("Aperture", String(format: "f/%.1f", aperture))
                            }
                            if let fl = detail.focalLength {
                                let flText: String = {
                                    var s = String(format: "%.0fmm", fl)
                                    if let fl35 = detail.focalLength35mm {
                                        s += String(format: " (%.0fmm eq)", fl35)
                                    }
                                    return s
                                }()
                                metadataRow("Focal Length", flText)
                            }
                        }
                    }
                }

                // File info
                metadataSection("File") {
                    VStack(alignment: .leading, spacing: 4) {
                        metadataRow("Path", detail.relPath)
                        metadataRow("Type", detail.mediaType)
                        if let dims = detail.dimensionsDescription {
                            metadataRow("Dimensions", dims)
                        }
                        if let duration = detail.durationSec {
                            metadataRow("Duration", formatDuration(duration))
                        }
                        if let takenAt = detail.takenAt {
                            metadataRow("Taken", takenAt)
                        }
                    }
                }

                // OCR Text
                if let ocr = detail.ocrText, !ocr.isEmpty {
                    metadataSection("OCR Text") {
                        Text(ocr)
                            .font(.caption)
                            .foregroundColor(.secondary)
                            .textSelection(.enabled)
                    }
                }

                // Note
                if let note = detail.note, !note.isEmpty {
                    metadataSection("Note") {
                        Text(note)
                            .font(.callout)
                            .foregroundColor(.secondary)
                            .textSelection(.enabled)
                    }
                }
            }
            .padding()
        }
        .background(Color(nsColor: .controlBackgroundColor))
    }

    @ViewBuilder
    private func metadataSection(_ title: String, @ViewBuilder content: () -> some View) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.caption)
                .fontWeight(.semibold)
                .foregroundColor(.secondary)
                .textCase(.uppercase)
            content()
        }
        Divider()
    }

    @ViewBuilder
    private func metadataRow(_ label: String, _ value: String) -> some View {
        HStack(alignment: .top) {
            Text(label)
                .font(.caption)
                .foregroundColor(.secondary)
                .frame(width: 80, alignment: .trailing)
            Text(value)
                .font(.caption)
                .textSelection(.enabled)
                .lineLimit(3)
        }
    }

    private func formatDuration(_ seconds: Double) -> String {
        let total = Int(seconds)
        let m = total / 60
        let s = total % 60
        if m >= 60 {
            let h = m / 60
            return String(format: "%d:%02d:%02d", h, m % 60, s)
        }
        return String(format: "%d:%02d", m, s)
    }
}

// MARK: - Flow Layout (for tags)

/// Simple horizontal flow layout that wraps to new lines.
struct FlowLayout: Layout {
    var spacing: CGFloat = 4

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let result = layout(proposal: proposal, subviews: subviews)
        return result.size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let result = layout(proposal: proposal, subviews: subviews)
        for (index, offset) in result.offsets.enumerated() {
            subviews[index].place(
                at: CGPoint(x: bounds.minX + offset.x, y: bounds.minY + offset.y),
                proposal: .unspecified
            )
        }
    }

    private func layout(proposal: ProposedViewSize, subviews: Subviews) -> (size: CGSize, offsets: [CGPoint]) {
        let maxWidth = proposal.width ?? .infinity
        var offsets: [CGPoint] = []
        var currentX: CGFloat = 0
        var currentY: CGFloat = 0
        var lineHeight: CGFloat = 0
        var maxX: CGFloat = 0

        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if currentX + size.width > maxWidth, currentX > 0 {
                currentX = 0
                currentY += lineHeight + spacing
                lineHeight = 0
            }
            offsets.append(CGPoint(x: currentX, y: currentY))
            lineHeight = max(lineHeight, size.height)
            currentX += size.width + spacing
            maxX = max(maxX, currentX)
        }

        return (CGSize(width: maxX, height: currentY + lineHeight), offsets)
    }
}
