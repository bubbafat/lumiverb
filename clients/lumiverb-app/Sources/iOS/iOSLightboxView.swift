import SwiftUI
import LumiverbKit

/// iOS lightbox matching Google Photos: full-screen image with top bar
/// (back, date, favorite, menu) and bottom bar (Share, Add to, Trash).
/// Swipe up to reveal metadata details below the image.
struct iOSLightboxView: View {
    @ObservedObject var browseState: BrowseState
    let client: APIClient?
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            VStack(spacing: 0) {
                topBar
                Spacer(minLength: 0)
                imageArea
                Spacer(minLength: 0)
                bottomBar
            }
        }
        .statusBar(hidden: true)
        .gesture(
            DragGesture(minimumDistance: 50)
                .onEnded { value in
                    let horizontal = value.translation.width
                    let vertical = value.translation.height
                    // Horizontal swipe for prev/next
                    if abs(horizontal) > abs(vertical) {
                        if horizontal < -50 {
                            browseState.navigateLightbox(direction: 1)
                        } else if horizontal > 50 {
                            browseState.navigateLightbox(direction: -1)
                        }
                    }
                    // Vertical swipe up to show details
                    if vertical < -100 {
                        showDetails = true
                    }
                    // Vertical swipe down to dismiss
                    if vertical > 100 {
                        browseState.closeLightbox()
                        dismiss()
                    }
                }
        )
        .sheet(isPresented: $showDetails) {
            detailsSheet
        }
    }

    @State private var showDetails = false

    // MARK: - Top bar

    private var topBar: some View {
        HStack {
            Button {
                browseState.closeLightbox()
                dismiss()
            } label: {
                Image(systemName: "chevron.left")
                    .font(.title3)
                    .foregroundColor(.white)
            }

            Spacer()

            if let detail = browseState.assetDetail, let takenAt = detail.takenAt {
                VStack(spacing: 2) {
                    Text(formattedDate(takenAt))
                        .font(.subheadline.weight(.medium))
                    Text(formattedTime(takenAt))
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                .foregroundColor(.white)
            }

            Spacer()

            HStack(spacing: 16) {
                Button {
                    showFaces.toggle()
                } label: {
                    Image(systemName: showFaces ? "face.smiling.inverse" : "face.smiling")
                        .foregroundColor(showFaces ? .accentColor : .white)
                }

                Button {
                    var updated = browseState.currentRating
                    updated.favorite.toggle()
                    browseState.currentRating = updated
                    browseState.updateCurrentRating(
                        RatingUpdateBody(favorite: updated.favorite)
                    )
                } label: {
                    Image(systemName: browseState.currentRating.favorite ? "star.fill" : "star")
                        .foregroundColor(browseState.currentRating.favorite ? .yellow : .white)
                }

                Button { showDetails = true } label: {
                    Image(systemName: "ellipsis")
                        .foregroundColor(.white)
                }
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    // MARK: - Image area

    @State private var showFaces = false

    @ViewBuilder
    private var imageArea: some View {
        if let assetId = browseState.selectedAssetId {
            ZStack {
                AuthenticatedImageView(
                    assetId: assetId,
                    client: client,
                    type: .proxy
                )
                .aspectRatio(contentMode: .fit)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .clipped()

                if showFaces,
                   let detail = browseState.assetDetail,
                   let width = detail.width,
                   let height = detail.height {
                    iOSFaceOverlayView(
                        assetId: assetId,
                        imageWidth: width,
                        imageHeight: height,
                        client: client
                    )
                }
            }
        } else {
            Color.clear
        }
    }

    // MARK: - Bottom bar

    @State private var shareItem: ShareableImage?
    @State private var isPreparingShare = false

    private var bottomBar: some View {
        HStack(spacing: 0) {
            bottomButton(
                "Share",
                systemImage: "square.and.arrow.up",
                isLoading: isPreparingShare
            ) {
                Task { await prepareShare() }
            }
            bottomButton("Add to", systemImage: "plus.rectangle.on.folder") {
                // TODO: add to collection
            }
            bottomButton("Trash", systemImage: "trash") {
                // TODO: trash asset
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .sheet(item: $shareItem) { item in
            ShareSheet(items: [item.url])
                .ignoresSafeArea()
        }
    }

    /// Fetches the proxy bytes for the current asset, writes them to a
    /// temp file, then surfaces the file URL via the share sheet so the
    /// user can save to camera roll or send anywhere.
    private func prepareShare() async {
        guard let assetId = browseState.selectedAssetId, !isPreparingShare else { return }
        isPreparingShare = true
        defer { isPreparingShare = false }

        do {
            guard let data = try await client?.getData("/v1/assets/\(assetId)/proxy") else {
                return
            }
            // Build a sensible filename — strip the path, fall back to assetId.
            let filename: String
            if let detail = browseState.assetDetail {
                filename = (detail.relPath as NSString).lastPathComponent
            } else {
                filename = "\(assetId).jpg"
            }
            let url = FileManager.default.temporaryDirectory
                .appendingPathComponent(filename)
            try data.write(to: url, options: .atomic)
            shareItem = ShareableImage(url: url)
        } catch {
            // Swallow — the share button just doesn't open. Logging
            // here would be noise; users can retry.
        }
    }

    private func bottomButton(
        _ label: String,
        systemImage: String,
        isLoading: Bool = false,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            VStack(spacing: 4) {
                if isLoading {
                    ProgressView()
                        .controlSize(.small)
                        .frame(height: 22)
                } else {
                    Image(systemName: systemImage)
                        .font(.title3)
                }
                Text(label)
                    .font(.caption2)
            }
            .foregroundColor(.white)
            .frame(maxWidth: .infinity)
        }
    }

    // MARK: - Details sheet (pull-up)

    private var detailsSheet: some View {
        NavigationStack {
            List {
                if let detail = browseState.assetDetail {
                    Section {
                        if let takenAt = detail.takenAt {
                            Label(formatFullDateTime(takenAt), systemImage: "calendar")
                        }
                        if let desc = detail.aiDescription, !desc.isEmpty {
                            Text(desc)
                                .font(.subheadline)
                                .foregroundColor(.secondary)
                        }
                    }

                    Section("Details") {
                        if let dims = detail.dimensionsDescription {
                            Label(dims, systemImage: "aspectratio")
                        }
                        if let camera = detail.cameraDescription {
                            Label(camera, systemImage: "camera")
                        }
                        if let lens = detail.lensModel {
                            Label(lens, systemImage: "camera.aperture")
                        }
                        if let iso = detail.iso {
                            Label("ISO \(iso)", systemImage: "dial.low")
                        }
                        if let aperture = detail.aperture {
                            Label("f/\(String(format: "%.1f", aperture))", systemImage: "f.circle")
                        }
                        if let exposure = detail.exposureDescription {
                            Label(exposure, systemImage: "timer")
                        }
                        if let focal = detail.focalLength {
                            Label("\(String(format: "%.0f", focal))mm", systemImage: "scope")
                        }
                    }

                    if let tags = detail.aiTags, !tags.isEmpty {
                        Section("Tags") {
                            FlowLayout(tags: tags) { tag in
                                Text(tag)
                                    .font(.caption)
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 5)
                                    .background(Color.gray.opacity(0.2))
                                    .cornerRadius(12)
                            }
                        }
                    }

                    if let ocrText = detail.ocrText, !ocrText.isEmpty {
                        Section("Text in Image") {
                            Text(ocrText)
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }

                    Section {
                        Label(detail.filename, systemImage: "doc")
                        Label(detail.relPath, systemImage: "folder")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                } else if browseState.isLoadingDetail {
                    ProgressView()
                        .frame(maxWidth: .infinity)
                }
            }
            .navigationTitle("Details")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { showDetails = false }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    // MARK: - Date formatting

    private func formattedDate(_ iso: String) -> String {
        guard let date = parseISO(iso) else { return iso }
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .none
        return f.string(from: date)
    }

    private func formattedTime(_ iso: String) -> String {
        guard let date = parseISO(iso) else { return "" }
        let f = DateFormatter()
        f.dateStyle = .none
        f.timeStyle = .short
        return f.string(from: date)
    }

    private func formatFullDateTime(_ iso: String) -> String {
        guard let date = parseISO(iso) else { return iso }
        let f = DateFormatter()
        f.dateStyle = .long
        f.timeStyle = .short
        return f.string(from: date)
    }

    private func parseISO(_ iso: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f.date(from: iso) ?? ISO8601DateFormatter().date(from: iso)
    }
}

// MARK: - Flow layout for tags

private struct FlowLayout<Data: RandomAccessCollection, Content: View>: View
where Data.Element: Hashable {
    let tags: Data
    let content: (Data.Element) -> Content

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 60))], spacing: 6) {
            ForEach(Array(tags), id: \.self) { tag in
                content(tag)
            }
        }
    }
}

// MARK: - Share sheet plumbing

/// Identifiable wrapper around a temp-file URL so SwiftUI's `.sheet(item:)`
/// dismisses the share sheet when set back to nil.
private struct ShareableImage: Identifiable {
    let url: URL
    var id: String { url.path }
}

/// Wraps `UIActivityViewController` so SwiftUI can present it as a sheet.
private struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]

    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }

    func updateUIViewController(_ controller: UIActivityViewController, context: Context) {}
}
