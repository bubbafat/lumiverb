import SwiftUI

/// Detail view for a single collection: header + asset grid.
public struct CollectionDetailView: View {
    @ObservedObject public var collectionsState: CollectionsState
    @ObservedObject public var browseState: BrowseState
    public let client: APIClient?

    @State private var showRenameSheet = false
    @State private var showDeleteConfirm = false

    public init(
        collectionsState: CollectionsState,
        browseState: BrowseState,
        client: APIClient?
    ) {
        self.collectionsState = collectionsState
        self.browseState = browseState
        self.client = client
    }

    public var body: some View {
        VStack(spacing: 0) {
            if let col = collectionsState.openCollection {
                // Header
                collectionHeader(col)

                Divider()

                // Asset grid
                if collectionsState.collectionAssets.isEmpty && !collectionsState.isLoadingAssets {
                    VStack(spacing: 8) {
                        Image(systemName: "photo.on.rectangle")
                            .font(.largeTitle)
                            .foregroundColor(.secondary)
                        Text("No assets in this collection")
                            .foregroundColor(.secondary)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    collectionAssetGrid
                }
            }
        }
        .overlay {
            if collectionsState.isLoadingAssets && collectionsState.collectionAssets.isEmpty {
                ProgressView()
            }
        }
        .sheet(isPresented: $showRenameSheet) {
            if let col = collectionsState.openCollection {
                RenameCollectionSheet(
                    collectionsState: collectionsState,
                    collectionId: col.collectionId,
                    currentName: col.name
                )
            }
        }
        .confirmationDialog(
            "Delete this collection?",
            isPresented: $showDeleteConfirm,
            titleVisibility: .visible
        ) {
            Button("Delete", role: .destructive) {
                if let id = collectionsState.openCollection?.collectionId {
                    Task { await collectionsState.deleteCollection(id: id) }
                }
            }
        } message: {
            Text("Assets in the collection won't be deleted from your library.")
        }
    }

    @ViewBuilder
    private func collectionHeader(_ col: AssetCollection) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(col.name)
                    .font(.title3)
                    .fontWeight(.semibold)
                HStack(spacing: 4) {
                    if col.isSmart {
                        Image(systemName: "wand.and.stars")
                            .font(.caption)
                            .foregroundColor(.purple)
                    }
                    Text("\(col.assetCount) item\(col.assetCount == 1 ? "" : "s")")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                if col.isSmart, let sq = col.savedQuery {
                    smartQuerySummary(sq)
                }
            }

            Spacer()

            if col.isOwn {
                Menu {
                    Button("Rename...") { showRenameSheet = true }
                    Button("Delete...", role: .destructive) { showDeleteConfirm = true }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
            }

            Button("Back") {
                collectionsState.closeDetail()
            }
            .controlSize(.small)
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
        .background(.bar)
    }

    @ViewBuilder
    private func smartQuerySummary(_ sq: SavedQuery) -> some View {
        let labels = Self.formatSavedQuery(sq)
        if !labels.isEmpty {
            HStack(spacing: 4) {
                ForEach(labels, id: \.self) { label in
                    Text(label)
                        .font(.caption2)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Color.purple.opacity(0.15))
                        .cornerRadius(8)
                        .foregroundColor(.purple)
                }
            }
        }
    }

    static func formatSavedQuery(_ sq: SavedQuery) -> [String] {
        var labels: [String] = []
        if let q = sq.q, !q.isEmpty {
            labels.append("Search: \"\(q)\"")
        }
        for (key, wrapped) in sq.filters {
            let val = wrapped.value
            switch key {
            case "camera_make": labels.append("Camera: \(val)")
            case "camera_model": labels.append("Model: \(val)")
            case "lens_model": labels.append("Lens: \(val)")
            case "media_type":
                if "\(val)" == "image" { labels.append("Photos") }
                else if "\(val)" == "video" { labels.append("Videos") }
            case "favorite":
                if "\(val)" == "true" || "\(val)" == "1" { labels.append("Favorites") }
            case "star_min": labels.append("\(val)+ stars")
            case "color": labels.append("Color: \(val)")
            case "tag": labels.append("Tag: \(val)")
            case "has_gps":
                if "\(val)" == "true" || "\(val)" == "1" { labels.append("Has GPS") }
            case "has_faces":
                if "\(val)" == "true" || "\(val)" == "1" { labels.append("Has faces") }
            case "has_rating":
                if "\(val)" == "true" || "\(val)" == "1" { labels.append("Has rating") }
            case "has_color":
                if "\(val)" == "true" || "\(val)" == "1" { labels.append("Has color") }
            case "iso_min": labels.append("ISO \(val)+")
            case "person_id": labels.append("Person filter")
            default: break
            }
        }
        return labels
    }

    private var collectionAssetGrid: some View {
        GeometryReader { geo in
            let layout = MediaLayout.compute(
                aspectRatios: collectionsState.collectionAssets.map { $0.aspectRatio },
                containerWidth: geo.size.width - MediaGridLayoutConstants.spacing * 2,
                targetRowHeight: MediaGridLayoutConstants.targetRowHeight,
                spacing: MediaGridLayoutConstants.spacing
            )

            ScrollView {
                LazyVStack(alignment: .leading, spacing: MediaGridLayoutConstants.spacing) {
                    ForEach(Array(layout.rows.enumerated()), id: \.offset) { _, row in
                        collectionAssetRow(row: row, layout: layout)
                    }

                    if collectionsState.isLoadingAssets {
                        ProgressView()
                            .padding()
                            .frame(maxWidth: .infinity)
                    }
                }
                .padding(MediaGridLayoutConstants.spacing)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    @ViewBuilder
    private func collectionAssetRow(row: [Int], layout: MediaLayout) -> some View {
        let rowHeight = row.first.map { layout.frames[$0].height } ?? MediaGridLayoutConstants.targetRowHeight
        HStack(spacing: MediaGridLayoutConstants.spacing) {
            ForEach(row, id: \.self) { index in
                let asset = collectionsState.collectionAssets[index]
                let size = layout.frames[index]
                collectionAssetCell(asset: asset)
                    .frame(width: size.width, height: size.height)
                    .clipped()
                    .onTapGesture {
                        browseState.focusedIndex = index
                        Task { await browseState.loadAssetDetail(assetId: asset.assetId) }
                    }
                    .contextMenu {
                        if let col = collectionsState.openCollection, col.isOwn {
                            Button("Remove from Collection", role: .destructive) {
                                Task {
                                    _ = await collectionsState.removeAssets(
                                        collectionId: col.collectionId,
                                        assetIds: [asset.assetId]
                                    )
                                }
                            }
                        }
                        AssetRatingContextMenu(assetId: asset.assetId, client: client)
                    }
                    .onAppear {
                        if index >= collectionsState.collectionAssets.count - 20 {
                            Task { await collectionsState.loadNextPage() }
                        }
                    }
            }
        }
        .frame(height: rowHeight)
    }

    @ViewBuilder
    private func collectionAssetCell(asset: CollectionAsset) -> some View {
        ZStack(alignment: .bottomLeading) {
            AuthenticatedImageView(
                assetId: asset.assetId,
                client: client,
                type: .thumbnail
            )
            .background(Color.gray.opacity(0.1))
            .clipped()

            if asset.isVideo {
                Image(systemName: "play.fill")
                    .font(.title2)
                    .foregroundColor(.white.opacity(0.9))
                    .shadow(color: .black.opacity(0.5), radius: 4)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .cornerRadius(2)
        .contentShape(Rectangle())
    }
}

// MARK: - Rename sheet

struct RenameCollectionSheet: View {
    @ObservedObject var collectionsState: CollectionsState
    let collectionId: String
    let currentName: String
    @Environment(\.dismiss) private var dismiss

    @State private var name: String = ""

    var body: some View {
        VStack(spacing: 16) {
            Text("Rename Collection")
                .font(.headline)

            TextField("Name", text: $name)
                .textFieldStyle(.roundedBorder)

            HStack {
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Rename") {
                    Task {
                        await collectionsState.renameCollection(id: collectionId, name: name)
                        dismiss()
                    }
                }
                .keyboardShortcut(.defaultAction)
                .disabled(name.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding()
        .frame(minWidth: 300)
        .onAppear { name = currentName }
    }
}
