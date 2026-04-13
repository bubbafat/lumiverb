import SwiftUI
import PhotosUI
import LumiverbKit

/// Search tab with face circles, search bar, and results.
/// Matches Google Photos search layout: face row at top, search bar,
/// recent searches, then results grouped by relevance.
struct SearchTab: View {
    @ObservedObject var appState: iOSAppState
    @ObservedObject var browseState: BrowseState
    @ObservedObject var peopleState: PeopleState

    // M7: similar-by-image state
    @State private var imageSimilarPickedItem: PhotosPickerItem?
    @State private var imageSimilarSource: UIImage?
    @State private var imageSimilarHits: [SimilarHit] = []
    @State private var imageSimilarLoading = false
    @State private var imageSimilarError: String?

    var body: some View {
        VStack(spacing: 0) {
            if imageSimilarSource != nil || imageSimilarLoading || !imageSimilarHits.isEmpty {
                imageSimilarContent
            } else if browseState.mode == .search || browseState.isSearching {
                searchResultsContent
            } else if browseState.filters.personId != nil {
                personFilterContent
            } else if case .similar(let sourceId) = browseState.mode {
                similarResultsContent(sourceId: sourceId)
            } else {
                searchHomeContent
            }
        }
        .navigationTitle("Search")
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                PhotosPicker(
                    selection: $imageSimilarPickedItem,
                    matching: .images,
                    photoLibrary: .shared()
                ) {
                    Image(systemName: "photo.badge.magnifyingglass")
                }
            }
        }
        .onChange(of: imageSimilarPickedItem) { _, newItem in
            guard let newItem else { return }
            Task { await runImageSimilarSearch(item: newItem) }
        }
        .searchable(
            text: $browseState.searchQuery,
            placement: .navigationBarDrawer(displayMode: .always),
            prompt: "Search photos"
        )
        .onSubmit(of: .search) {
            browseState.personSuggestions = []
            Task { await browseState.performSearch() }
        }
        .onChange(of: browseState.searchQuery) { _, newValue in
            if newValue.isEmpty {
                if browseState.committedSearchQuery.isEmpty {
                    browseState.clearSearch()
                }
                browseState.personSuggestions = []
            } else {
                browseState.debouncedPersonSearch(query: newValue)
            }
        }
        .searchSuggestions {
            personSuggestions
        }
        // Lightbox is presented from MainTabView at the top level so
        // it works from any tab. No fullScreenCover here.
    }

    // MARK: - Search home (no active search)

    @ViewBuilder
    private var searchHomeContent: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                // Face circles row
                if !peopleState.people.isEmpty {
                    faceCirclesRow
                }

                if browseState.isSearching {
                    ProgressView("Searching...")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .padding(.top, 60)
                }
            }
            .padding(.top, 8)
        }
        .task {
            await peopleState.loadIfNeeded()
        }
    }

    // MARK: - Face circles

    private var faceCirclesRow: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("People")
                .font(.headline)
                .padding(.horizontal)

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 16) {
                    ForEach(peopleState.people) { person in
                        Button {
                            browseState.filterByPerson(person)
                        } label: {
                            VStack(spacing: 6) {
                                FaceThumbnailView(
                                    faceId: person.representativeFaceId,
                                    client: appState.client
                                )
                                .frame(width: 64, height: 64)
                                .clipShape(Circle())

                                Text(person.displayName)
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                                    .lineLimit(1)
                                    .frame(width: 64)
                            }
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal)
            }
        }
    }

    // MARK: - Search results

    @ViewBuilder
    private var searchResultsContent: some View {
        VStack(spacing: 0) {
            FilterChicletBar(browseState: browseState)
            if browseState.isSearching {
                ProgressView("Searching...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if browseState.searchResults.isEmpty {
                ContentUnavailableView.search(text: browseState.committedSearchQuery)
            } else {
                SearchResultsGrid(browseState: browseState, client: appState.client) {
                    EmptyView()
                }
                .refreshable {
                    await browseState.executeSearch()
                }
            }
        }
    }

    // MARK: - Person filter results

    /// Shows the asset grid filtered by a person (tapped from face circles).
    /// `filterByPerson` sets mode to `.library` and adds a person filter,
    /// so we reuse the standard grid views.
    @ViewBuilder
    private var personFilterContent: some View {
        VStack(spacing: 0) {
            FilterChicletBar(browseState: browseState)
            if browseState.assets.isEmpty && !browseState.isLoadingAssets {
                ContentUnavailableView(
                    "No Photos",
                    systemImage: "person.crop.rectangle",
                    description: Text("No photos found for this person")
                )
            } else {
                MediaGridView(browseState: browseState, client: appState.client) {
                    EmptyView()
                }
            }
        }
    }

    @ViewBuilder
    private func similarResultsContent(sourceId: String) -> some View {
        if browseState.isFindingSimilar {
            ProgressView("Finding similar...")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if browseState.similarResults.isEmpty {
            ContentUnavailableView(
                "No Similar Photos",
                systemImage: "square.stack.3d.up"
            )
        } else {
            SimilarResultsGrid(
                browseState: browseState,
                sourceAssetId: sourceId,
                client: appState.client
            ) {
                EmptyView()
            }
        }
    }

    // MARK: - Person suggestions

    @ViewBuilder
    private var personSuggestions: some View {
        if !browseState.personSuggestions.isEmpty {
            Section("People") {
                ForEach(browseState.personSuggestions) { person in
                    Button {
                        browseState.filterByPerson(person)
                    } label: {
                        HStack(spacing: 8) {
                            FaceThumbnailView(
                                faceId: person.representativeFaceId,
                                client: appState.client
                            )
                            .frame(width: 28, height: 28)
                            .clipShape(Circle())
                            VStack(alignment: .leading, spacing: 1) {
                                Text(person.displayName)
                                Text("\(person.faceCount) photos")
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: - M7: similar-by-image

    @ViewBuilder
    private var imageSimilarContent: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 12) {
                    if let source = imageSimilarSource {
                        Image(uiImage: source)
                            .resizable()
                            .aspectRatio(contentMode: .fill)
                            .frame(width: 80, height: 80)
                            .clipShape(RoundedRectangle(cornerRadius: 8))
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Similar to")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        Text(imageSimilarLoading
                             ? "Searching..."
                             : "\(imageSimilarHits.count) result\(imageSimilarHits.count == 1 ? "" : "s")")
                            .font(.subheadline.weight(.medium))
                    }
                    Spacer()
                    Button {
                        clearImageSimilar()
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.title2)
                            .foregroundColor(.secondary)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 8)

                if let error = imageSimilarError {
                    Text(error)
                        .font(.caption)
                        .foregroundColor(.red)
                        .padding(.horizontal, 16)
                }

                if imageSimilarLoading {
                    ProgressView()
                        .frame(maxWidth: .infinity)
                        .padding(.top, 40)
                } else if imageSimilarHits.isEmpty && imageSimilarSource != nil {
                    ContentUnavailableView(
                        "No Similar Photos",
                        systemImage: "square.stack.3d.up",
                        description: Text("Nothing close enough was found")
                    )
                    .padding(.top, 40)
                } else {
                    let columns = [
                        GridItem(.flexible(), spacing: 2),
                        GridItem(.flexible(), spacing: 2),
                        GridItem(.flexible(), spacing: 2),
                    ]
                    LazyVGrid(columns: columns, spacing: 2) {
                        ForEach(imageSimilarHits) { hit in
                            Color.clear
                                .aspectRatio(1, contentMode: .fit)
                                .overlay(
                                    AuthenticatedImageView(
                                        assetId: hit.assetId,
                                        client: appState.client,
                                        type: .thumbnail
                                    )
                                )
                                .clipped()
                                .onTapGesture {
                                    Task { await browseState.loadAssetDetail(assetId: hit.assetId) }
                                }
                        }
                    }
                    .padding(.horizontal, 2)
                }
            }
        }
    }

    private func clearImageSimilar() {
        imageSimilarPickedItem = nil
        imageSimilarSource = nil
        imageSimilarHits = []
        imageSimilarError = nil
        imageSimilarLoading = false
    }

    /// Loads the picked photo, embeds it locally with Apple Vision
    /// feature prints, and POSTs the vector to /v1/similar/search-by-vector.
    ///
    /// We embed client-side rather than relying on `search-by-image`'s
    /// server CLIP because the macOS app indexes libraries with
    /// `apple_vision` feature prints (model_id `apple_vision`, model
    /// version `feature_print_v1`). The server's CLIP model lives in a
    /// different vector space and would never match the indexed assets
    /// — so the *scene* path has to embed here, in the same space the
    /// library was indexed in.
    ///
    /// **Hybrid mode**: alongside the scene vector we ship a downscaled
    /// JPEG so the server can run face detection and ArcFace embedding,
    /// then RRF-fuse the identity hits with the scene cosine results.
    /// This is the fix for "the photo of my daughter doesn't find other
    /// photos of my daughter" — Apple Vision feature prints encode
    /// scene-level signal, not identity, and ArcFace is purpose-built
    /// for the identity case. The downscale to ~768px keeps upload
    /// bandwidth bounded while staying well above the face-detection
    /// minimum face size.
    private func runImageSimilarSearch(item: PhotosPickerItem) async {
        guard let client = appState.client else { return }
        // Pick a library: prefer the currently selected one, fall back
        // to the first available. The endpoint requires a library_id.
        let libraryId: String
        if let selected = browseState.selectedLibraryId {
            libraryId = selected
        } else if let first = appState.libraries.first {
            libraryId = first.libraryId
        } else {
            imageSimilarError = "No library available"
            return
        }

        imageSimilarLoading = true
        imageSimilarHits = []
        imageSimilarError = nil
        defer { imageSimilarLoading = false }

        do {
            guard let data = try await item.loadTransferable(type: Data.self),
                  let uiImage = UIImage(data: data) else {
                imageSimilarError = "Couldn't load image"
                return
            }
            // Show a downscaled preview at the top of the results.
            // The full-resolution data is what we feed to Vision —
            // VNGenerateImageFeaturePrintRequest does its own
            // resampling internally.
            imageSimilarSource = downscaleImage(uiImage, maxDimension: 512)

            // Embed on the iOS device — must use Apple Vision feature
            // prints to match what the macOS app indexed with.
            let vector = try iOSFeaturePrintEmbedder.embed(imageData: data)

            // Build the hybrid upload bytes: downscale to 768px max
            // edge and re-encode at 0.85 JPEG quality. The server only
            // needs enough resolution for InsightFace (which itself
            // resizes to 640x640 internally), so anything larger is
            // pure bandwidth waste.
            let uploadImage = downscaleImage(uiImage, maxDimension: 768)
            let imageB64 = uploadImage.jpegData(compressionQuality: 0.85)?
                .base64EncodedString()

            let request = VectorSimilarityRequest(
                libraryId: libraryId,
                vector: vector,
                modelId: iOSFeaturePrintEmbedder.modelId,
                modelVersion: iOSFeaturePrintEmbedder.modelVersion,
                limit: 30,
                imageB64: imageB64
            )
            let response: ImageSimilarityResponse = try await client.post(
                "/v1/similar/search-by-vector",
                body: request
            )
            imageSimilarHits = response.hits
        } catch {
            imageSimilarError = "Search failed: \(error.localizedDescription)"
        }
    }

    private func downscaleImage(_ image: UIImage, maxDimension: CGFloat) -> UIImage {
        let size = image.size
        let longEdge = max(size.width, size.height)
        guard longEdge > maxDimension else { return image }
        let scale = maxDimension / longEdge
        let newSize = CGSize(width: size.width * scale, height: size.height * scale)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let renderer = UIGraphicsImageRenderer(size: newSize, format: format)
        return renderer.image { _ in
            image.draw(in: CGRect(origin: .zero, size: newSize))
        }
    }
}
