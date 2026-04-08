import SwiftUI
import LumiverbKit

/// Cluster review panel — vertical list of unnamed face clusters with a
/// "name this person" inline form. Phase 6 M5 of ADR-014.
///
/// Each card surfaces the cluster's representative face crops, the top
/// few similar people (so frequent subjects are one click to merge),
/// and a free-form text field for naming a brand-new person. Dismissing
/// a cluster pops a 5-second undo toast at the bottom.
struct ClusterReviewView: View {
    @ObservedObject var state: ClusterReviewState
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    var body: some View {
        ZStack(alignment: .bottom) {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    header

                    if state.truncated {
                        truncatedBanner
                    }

                    if let error = state.error {
                        errorBanner(error)
                    }

                    if state.clusters.isEmpty && !state.isLoading {
                        emptyState
                    } else {
                        ForEach(state.clusters) { cluster in
                            ClusterCardView(
                                cluster: cluster,
                                state: state,
                                browseState: browseState,
                                client: client
                            )
                            .transition(.opacity.combined(with: .move(edge: .leading)))
                            .onAppear {
                                Task {
                                    await state.loadNearestPeople(forCluster: cluster.clusterIndex)
                                }
                            }
                        }
                    }

                    if state.isLoading {
                        ProgressView()
                            .padding()
                            .frame(maxWidth: .infinity)
                    }
                }
                .padding(20)
                .animation(.easeInOut(duration: 0.2), value: state.clusters.count)
            }

            if state.lastDismissedPersonId != nil {
                undoToast
                    .padding(.bottom, 24)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: state.lastDismissedPersonId)
        .onAppear {
            // Use unstructured Task so a transient view teardown doesn't
            // surface as a `cancelled` error (same lesson as PeopleView).
            Task { await state.loadIfNeeded() }
        }
    }

    // MARK: - Sections

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            Text("Cluster Review")
                .font(.largeTitle)
                .fontWeight(.bold)
            Spacer()
            if !state.clusters.isEmpty {
                Text("\(state.clusters.count) cluster\(state.clusters.count == 1 ? "" : "s")")
                    .font(.callout)
                    .foregroundColor(.secondary)
            }
            Button {
                Task { await state.loadClusters() }
            } label: {
                Label("Refresh", systemImage: "arrow.clockwise")
            }
            .controlSize(.small)
            .disabled(state.isLoading)
        }
    }

    private var truncatedBanner: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundColor(.yellow)
            Text("Showing the top clusters. Name some to free up budget for the rest.")
                .font(.callout)
            Spacer()
        }
        .padding(10)
        .background(Color.yellow.opacity(0.15))
        .cornerRadius(8)
    }

    private func errorBanner(_ message: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.octagon.fill")
                .foregroundColor(.red)
            Text(message)
                .font(.callout)
                .lineLimit(2)
            Spacer()
            Button("Dismiss") { state.error = nil }
                .controlSize(.small)
        }
        .padding(10)
        .background(Color.red.opacity(0.12))
        .cornerRadius(8)
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "checkmark.seal.fill")
                .font(.system(size: 40))
                .foregroundColor(.green)
            Text("All clusters reviewed")
                .font(.title3)
            Text("Run face detection on new photos to find more people to name.")
                .font(.caption)
                .foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 80)
    }

    private var undoToast: some View {
        HStack(spacing: 12) {
            Image(systemName: "trash.fill")
                .foregroundColor(.white.opacity(0.85))
            Text("Cluster dismissed")
                .foregroundColor(.white)
            Spacer().frame(width: 8)
            Button("Undo") {
                Task { await state.undoLastDismiss() }
            }
            .controlSize(.regular)
            .tint(.accentColor)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.black.opacity(0.85))
        .cornerRadius(10)
        .shadow(radius: 6)
    }
}

// MARK: - Cluster card

/// One cluster card: face-crop strip on the left, name field + suggested
/// people + dismiss on the right. The face crops are pulled from
/// `/v1/faces/{face_id}/crop` via the existing `FaceThumbnailView`
/// (which already handles the auth + caching path).
struct ClusterCardView: View {
    let cluster: ClusterItem
    @ObservedObject var state: ClusterReviewState
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    @State private var nameInput: String = ""

    private var isPending: Bool {
        state.pendingMutations.contains(cluster.clusterIndex)
    }

    private var suggestions: [NearestPersonItem] {
        state.nearestPeople[cluster.clusterIndex] ?? []
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 16) {
                facesStrip
                actionsColumn
            }
            if !suggestions.isEmpty {
                Divider()
                suggestionsRow
            }
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor))
        .cornerRadius(10)
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.secondary.opacity(0.2), lineWidth: 1)
        )
        .opacity(isPending ? 0.5 : 1.0)
        .disabled(isPending)
    }

    // MARK: - Faces strip

    private var facesStrip: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text("\(cluster.size) face\(cluster.size == 1 ? "" : "s")")
                    .font(.caption)
                    .foregroundColor(.secondary)
                Text("· click a face to tag individually")
                    .font(.caption2)
                    .foregroundColor(.secondary.opacity(0.7))
            }

            HStack(spacing: 6) {
                ForEach(cluster.faces.prefix(8)) { face in
                    Button {
                        openLightbox(for: face)
                    } label: {
                        FaceThumbnailView(faceId: face.faceId, client: client)
                            .frame(width: 64, height: 64)
                            .clipShape(RoundedRectangle(cornerRadius: 4))
                            .background(Color.gray.opacity(0.1))
                    }
                    .buttonStyle(.plain)
                    .help("Open this photo to tag just this face")
                }
                if cluster.size > cluster.faces.count {
                    Text("+\(cluster.size - cluster.faces.count)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .frame(width: 64, height: 64)
                        .background(Color.gray.opacity(0.1))
                        .cornerRadius(4)
                }
            }
        }
    }

    /// Open the existing lightbox on the face's owning asset, with the
    /// face overlay forced on and the assign popover auto-targeted at
    /// this exact face. Gives the user a per-face escape hatch out of
    /// "name the whole cluster" — useful when HDBSCAN merged multiple
    /// real identities into one cluster.
    private func openLightbox(for face: PersonFaceItem) {
        // Install the cluster's full asset list as the lightbox prev/next
        // override so left/right arrows iterate the cluster's faces.
        browseState.displayedAssetIdsOverride = cluster.faces.map(\.assetId)
        browseState.pendingHighlightFaceId = face.faceId
        Task { await browseState.loadAssetDetail(assetId: face.assetId) }
    }

    // MARK: - Actions column (name input + dismiss)

    private var actionsColumn: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "person.crop.circle.badge.plus")
                    .foregroundColor(.secondary)
                TextField("Name all \(cluster.size) faces…", text: $nameInput)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(submitName)
                Button("Tag all \(cluster.size)", action: submitName)
                    .keyboardShortcut(.return, modifiers: [])
                    .disabled(nameInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .help("Tag every face in this cluster as one person — for heterogeneous clusters use the per-face thumbnails on the left")
            }
            HStack {
                Spacer()
                if isPending {
                    ProgressView().controlSize(.small)
                }
                Button(role: .destructive) {
                    Task { await state.dismissCluster(cluster.clusterIndex) }
                } label: {
                    Label("Dismiss", systemImage: "trash")
                }
                .controlSize(.small)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Suggestions row

    private var suggestionsRow: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Looks like…")
                .font(.caption)
                .foregroundColor(.secondary)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(suggestions) { person in
                        Button {
                            Task {
                                await state.mergeCluster(
                                    cluster.clusterIndex,
                                    intoPersonId: person.personId
                                )
                            }
                        } label: {
                            HStack(spacing: 6) {
                                Image(systemName: "person.crop.circle")
                                    .foregroundColor(.secondary)
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(person.displayName)
                                        .font(.callout)
                                        .lineLimit(1)
                                    Text("\(person.faceCount) photos")
                                        .font(.caption2)
                                        .foregroundColor(.secondary)
                                }
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(Color.accentColor.opacity(0.12))
                            .cornerRadius(6)
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
    }

    private func submitName() {
        let trimmed = nameInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        Task {
            await state.nameCluster(cluster.clusterIndex, newPersonName: trimmed)
        }
    }
}
