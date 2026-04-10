import SwiftUI
import AVKit

/// Full-screen lightbox overlay showing proxy image + metadata sidebar.
public struct LightboxView: View {
    @ObservedObject public var browseState: BrowseState
    public let client: APIClient?

    /// Persisted across sessions and across asset navigation. Matches the
    /// web UI's `lv_show_faces` localStorage key so users get the same
    /// preference on both clients (the keys are independent storage but
    /// the naming convention is shared, which makes future sync easier).
    @AppStorage("lv_show_faces") private var showFaces: Bool = false

    @StateObject private var facesVM: LightboxFacesViewModel

    public init(browseState: BrowseState, client: APIClient?) {
        self.browseState = browseState
        self.client = client
        let vm = LightboxFacesViewModel(client: client)
        // Wire the highlight-tagged callback to the navigation state.
        // After the user successfully tags a face that came from the
        // cluster-review handoff (red border + auto-opened popover),
        // advance to the next cluster asset so the cluster visibly
        // "moves along". If there's no next asset, close the lightbox
        // entirely. Mirrors the web Lightbox auto-advance behavior.
        vm.onHighlightFaceTagged = { [browseState] in
            if browseState.hasNextAsset {
                browseState.navigateLightbox(direction: 1)
            } else {
                browseState.closeLightbox()
            }
        }
        _facesVM = StateObject(wrappedValue: vm)
    }

    public var body: some View {
        HStack(spacing: 0) {
            // Main image area
            ZStack {
                Color.black

                if browseState.isLoadingDetail {
                    ProgressView()
                        .tint(.white)
                } else if let detail = browseState.assetDetail {
                    if detail.isVideo {
                        LightboxVideoPlayerView(
                            detail: detail,
                            libraryRootPath: browseState.selectedLibraryRootPath,
                            client: client
                        )
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    } else {
                        AuthenticatedImageView(
                            assetId: detail.assetId,
                            client: client,
                            type: .proxy
                        )
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                }

                // Face overlay layer — sibling of the image so both inherit
                // the same parent frame from the ZStack and the aspect-fit
                // math here matches the image's actual rendered rect.
                // Stills only — videos can have face data but the player
                // doesn't have a single frame to overlay against.
                if showFaces,
                   let detail = browseState.assetDetail,
                   !detail.isVideo,
                   let w = detail.width, let h = detail.height,
                   !facesVM.faces.isEmpty {
                    FaceOverlayView(
                        faces: facesVM.faces,
                        imageWidth: w,
                        imageHeight: h,
                        vm: facesVM
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
                    showFaces: $showFaces,
                    rating: $browseState.currentRating,
                    onRatingChange: { body in
                        browseState.updateCurrentRating(body)
                    },
                    onFindSimilar: {
                        Task { await browseState.findSimilar(assetId: detail.assetId) }
                    },
                    onReEnrich: { ops in
                        browseState.reEnrichAsset(assetId: detail.assetId, operations: ops)
                    },
                    onRevealInFinder: {
                        #if os(macOS)
                        if let rootPath = browseState.selectedLibraryRootPath {
                            let fullPath = (rootPath as NSString).appendingPathComponent(detail.relPath)
                            NSWorkspace.shared.activateFileViewerSelecting(
                                [URL(fileURLWithPath: fullPath)]
                            )
                        }
                        #endif
                    },
                    onOpenInPlayer: detail.isVideo ? {
                        #if os(macOS)
                        if let rootPath = browseState.selectedLibraryRootPath {
                            let fullPath = (rootPath as NSString).appendingPathComponent(detail.relPath)
                            let url = URL(fileURLWithPath: fullPath)
                            NSWorkspace.shared.open(url)
                        }
                        #endif
                    } : nil,
                    whisperEnabled: browseState.whisperEnabled,
                )
                .frame(width: 300)
            }
        }
        .background(.black)
        // Re-fetch faces whenever the visible asset changes — but only when
        // the toggle is on, so toggling off mid-browse doesn't keep paying
        // for face requests. The vm short-circuits if it already has the
        // current asset's faces cached.
        .task(id: showFacesTaskKey) {
            if showFaces, let assetId = browseState.assetDetail?.assetId {
                await facesVM.loadFaces(forAsset: assetId)
                // Cluster review hands us a `pendingHighlightFaceId`
                // when the user clicks a face crop. After the faces
                // load, mark that face as highlighted (red border) and
                // auto-open the assign popover on it so the per-face
                // tagging path is one click instead of "open lightbox
                // → press d → click face → assign". Cleared on consume
                // so navigating away doesn't keep popping the same
                // popover. The highlight stays on `facesVM` (not on
                // `browseState`) so the FaceBoxView color logic can
                // observe it directly.
                if let pending = browseState.pendingHighlightFaceId,
                   facesVM.faces.contains(where: { $0.faceId == pending }) {
                    facesVM.highlightedFaceId = pending
                    facesVM.selectFace(pending)
                    browseState.pendingHighlightFaceId = nil
                }
            } else if !showFaces {
                facesVM.reset()
            }
        }
        // Force the face overlay on whenever the cluster review hands
        // us a highlighted face — without this the user opens the
        // lightbox to a photo with no clickable hit target and falls
        // back to the bulk "name everything" path.
        .onChange(of: browseState.pendingHighlightFaceId) { _, newValue in
            if newValue != nil && !showFaces {
                showFaces = true
            }
        }
        #if os(macOS)
        // Rating keyboard shortcuts (Lightroom convention):
        // 1-5 set stars, 0 clears, F toggles favorite.
        .onKeyPress(characters: .init(charactersIn: "012345")) { press in
            let ch = press.characters
            if let digit = ch.first?.wholeNumberValue, digit >= 0 && digit <= 5 {
                browseState.currentRating.stars = digit
                browseState.updateCurrentRating(RatingUpdateBody(stars: digit))
                return .handled
            }
            return .ignored
        }
        .onKeyPress(characters: .init(charactersIn: "fF")) { _ in
            let newFav = !browseState.currentRating.favorite
            browseState.currentRating.favorite = newFav
            browseState.updateCurrentRating(RatingUpdateBody(favorite: newFav))
            return .handled
        }
        #endif
    }

    /// Composite key so `.task(id:)` re-runs both when the user toggles the
    /// face overlay on/off AND when navigation moves to a new asset.
    private var showFacesTaskKey: String {
        "\(showFaces)|\(browseState.assetDetail?.assetId ?? "")"
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
    @Binding var showFaces: Bool
    @Binding var rating: Rating
    let onRatingChange: (RatingUpdateBody) -> Void
    let onFindSimilar: () -> Void
    let onReEnrich: (Set<EnrichmentOperation>) -> Void
    let onRevealInFinder: () -> Void
    let onOpenInPlayer: (() -> Void)?
    var whisperEnabled: Bool = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                // Filename
                Text(detail.filename)
                    .font(.headline)
                    .lineLimit(2)

                // Rating
                RatingEditorView(rating: $rating, onChange: onRatingChange)

                Divider()

                // Actions
                HStack(spacing: 12) {
                    Button {
                        onFindSimilar()
                    } label: {
                        Label("Find Similar", systemImage: "square.stack.3d.up")
                    }
                    .controlSize(.small)

                    Button {
                        onRevealInFinder()
                    } label: {
                        Label("Reveal in Finder", systemImage: "folder")
                    }
                    .controlSize(.small)

                    // Show / hide face bounding box overlay. The keyboard
                    // shortcut `d` mirrors the web UI's lightbox shortcut
                    // (the `f` key is taken by Favorite via Lightroom
                    // convention). Stills only — videos can't currently
                    // host an overlay.
                    if !detail.isVideo {
                        Toggle(isOn: $showFaces) {
                            Label("Show Faces", systemImage: "face.dashed")
                        }
                        .toggleStyle(.button)
                        .controlSize(.small)
                        .keyboardShortcut("d", modifiers: [])
                        .help("Show face bounding boxes (D)")
                    }

                    ReEnrichMenu(onReEnrich: onReEnrich, whisperEnabled: whisperEnabled)
                        .controlSize(.small)

                    if let onOpenInPlayer {
                        Button {
                            onOpenInPlayer()
                        } label: {
                            Label("Open in Player", systemImage: "play.rectangle")
                        }
                        .controlSize(.small)
                    }
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
        #if canImport(AppKit)
        .background(Color(nsColor: .controlBackgroundColor))
        #elseif canImport(UIKit)
        .background(Color(uiColor: .secondarySystemBackground))
        #endif
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
