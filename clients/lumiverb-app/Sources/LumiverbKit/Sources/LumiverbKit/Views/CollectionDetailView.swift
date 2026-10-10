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

                if browseState.isSelecting {
                    SelectionToolbarView(browseState: browseState, client: client)
                }

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
        .onDisappear {
            // Clear shared selection so it doesn't leak into other tabs.
            if browseState.isSelecting {
                browseState.clearSelection()
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
                    collectionId: col.projectId,
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
                if let id = collectionsState.openCollection?.projectId {
                    Task { await collectionsState.deleteCollection(id: id) }
                }
            }
        } message: {
            Text("Assets in the collection won't be deleted from your library.")
        }
    }

    @ViewBuilder
    private func collectionHeader(_ col: Project) -> some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(col.name)
                    .font(.title3)
                    .fontWeight(.semibold)
                Text("\(col.assetCount) item\(col.assetCount == 1 ? "" : "s")")
                    .font(.caption)
                    .foregroundColor(.secondary)
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
    private var collectionAssetGrid: some View {
        #if os(iOS)
        DateGroupedGrid(
            browseState: browseState,
            items: collectionsState.collectionAssets,
            client: client,
            dateString: { $0.takenAt },
            assetId: { $0.assetId },
            isVideo: { $0.isVideo },
            isLoading: collectionsState.isLoadingAssets,
            onTap: { asset in
                if let idx = collectionsState.collectionAssets.firstIndex(where: { $0.assetId == asset.assetId }) {
                    browseState.focusedIndex = idx
                }
                Task { await browseState.loadAssetDetail(assetId: asset.assetId) }
            },
            onLastItemAppear: { _ in
                Task { await collectionsState.loadNextPage() }
            }
        )
        #else
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
        #endif
    }

    #if os(iOS)
    /// Thin wrapper over the shared `AssetGridCell`.
    @ViewBuilder
    private func iosCollectionCell(
        asset: ProjectAsset,
        isSelected: Bool
    ) -> some View {
        AssetGridCell(
            assetId: asset.assetId,
            isVideo: asset.isVideo,
            isSelected: isSelected,
            client: client,
            onToggleSelect: { browseState.toggleSelection(assetId: asset.assetId) }
        )
    }
    #endif

    @ViewBuilder
    private func collectionAssetRow(row: [Int], layout: MediaLayout) -> some View {
        let rowHeight = row.first.map { layout.frames[$0].height } ?? MediaGridLayoutConstants.targetRowHeight
        HStack(spacing: MediaGridLayoutConstants.spacing) {
            ForEach(row, id: \.self) { index in
                let asset = collectionsState.collectionAssets[index]
                let size = layout.frames[index]
                let isSelected = browseState.selectedAssetIds.contains(asset.assetId)
                collectionAssetCell(asset: asset, isSelected: isSelected, size: size)
                    .onTapGesture {
                        if browseState.isSelecting {
                            browseState.toggleSelection(assetId: asset.assetId)
                        } else {
                            browseState.focusedIndex = index
                            Task { await browseState.loadAssetDetail(assetId: asset.assetId) }
                        }
                    }
                    .contextMenu {
                        if let col = collectionsState.openCollection, col.isOwn {
                            Button("Remove from Collection", role: .destructive) {
                                Task {
                                    _ = await collectionsState.removeAssets(
                                        collectionId: col.projectId,
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

    /// Same frame-first + overlay pattern as MediaGridView's
    /// `assetCellWithOverlays` so the selection indicators are
    /// positioned relative to the cell's actual frame and don't get
    /// clipped when the image's `.aspectRatio(.fill)` overflows.
    @ViewBuilder
    private func collectionAssetCell(
        asset: ProjectAsset,
        isSelected: Bool,
        size: CGSize
    ) -> some View {
        AuthenticatedImageView(
            assetId: asset.assetId,
            client: client,
            type: .thumbnail
        )
        .background(Color.gray.opacity(0.1))
        .frame(width: size.width, height: size.height)
        .clipped()
        .cornerRadius(2)
        .overlay {
            if asset.isVideo {
                Image(systemName: "play.fill")
                    .font(.title2)
                    .foregroundColor(.white.opacity(0.9))
                    .shadow(color: .black.opacity(0.5), radius: 4)
            }
        }
        .overlay(alignment: .topLeading) {
            Button {
                browseState.toggleSelection(assetId: asset.assetId)
            } label: {
                Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                    .font(.title3)
                    .foregroundColor(isSelected ? .accentColor : .white.opacity(0.85))
                    .shadow(color: .black.opacity(0.5), radius: 2)
                    .padding(10)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .overlay {
            if isSelected {
                RoundedRectangle(cornerRadius: 2)
                    .strokeBorder(Color.accentColor, lineWidth: 3)
            }
        }
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
