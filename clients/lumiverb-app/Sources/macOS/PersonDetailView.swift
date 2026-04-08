import SwiftUI
import LumiverbKit

/// Detail view for a single person — header with name + face count, then
/// a grid of every photo containing that person. Phase 6 M3 of ADR-014.
///
/// Tapping a photo opens the existing `LightboxView` overlay through
/// `BrowseState`, with the person's full asset id list installed as the
/// lightbox's prev/next navigation override so left/right arrows iterate
/// the person's faces instead of whatever the underlying library mode
/// happens to have loaded. The override is cleared when the lightbox
/// closes (in `BrowseState.closeLightbox`).
struct PersonDetailView: View {
    let person: PersonItem
    @ObservedObject var peopleState: PeopleState
    @ObservedObject var browseState: BrowseState
    let client: APIClient?

    private let columns = Array(
        repeating: GridItem(.flexible(), spacing: 2),
        count: 4
    )

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header
                Divider()
                grid
            }
        }
        .navigationTitle(person.displayName)
    }

    // MARK: - Header

    private var header: some View {
        HStack(spacing: 16) {
            FaceThumbnailView(faceId: person.representativeFaceId, client: client)
                .frame(width: 80, height: 80)
                .background(Circle().fill(Color.gray.opacity(0.15)))
                .clipShape(Circle())
                .overlay(
                    Circle().stroke(Color.secondary.opacity(0.2), lineWidth: 1)
                )

            VStack(alignment: .leading, spacing: 4) {
                Text(person.displayName)
                    .font(.title2)
                Text("\(person.faceCount) photo\(person.faceCount == 1 ? "" : "s")")
                    .font(.subheadline)
                    .foregroundColor(.secondary)
            }
            Spacer()
        }
        .padding(16)
    }

    // MARK: - Grid

    @ViewBuilder
    private var grid: some View {
        if peopleState.personFaces.isEmpty && !peopleState.isLoadingFaces {
            VStack(spacing: 8) {
                Image(systemName: "photo.on.rectangle")
                    .font(.system(size: 32))
                    .foregroundColor(.secondary)
                Text("No photos for this person yet.")
                    .font(.callout)
                    .foregroundColor(.secondary)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 60)
        } else {
            LazyVGrid(columns: columns, spacing: 2) {
                ForEach(peopleState.personFaces) { face in
                    PersonFaceCellView(face: face, client: client)
                        .onTapGesture {
                            openLightbox(at: face)
                        }
                        .onAppear {
                            if let last = peopleState.personFaces.last,
                               last.faceId == face.faceId {
                                Task { await peopleState.loadNextFacesPage() }
                            }
                        }
                }
            }
            .padding(2)
        }

        if peopleState.isLoadingFaces {
            ProgressView()
                .padding()
        }

        if let error = peopleState.personFacesError {
            Text(error)
                .foregroundColor(.red)
                .font(.caption)
                .padding()
        }
    }

    private func openLightbox(at face: PersonFaceItem) {
        // Install the person's asset list as the lightbox prev/next
        // override BEFORE loading the detail, so the lightbox renders
        // with the right neighborhood from the first frame.
        browseState.displayedAssetIdsOverride = peopleState.personFaces.map(\.assetId)
        Task { await browseState.loadAssetDetail(assetId: face.assetId) }
    }
}

/// One cell in the per-person photo grid. Same look as `AssetCellView` but
/// keyed off `assetId` and without the AssetPageItem (which we don't have
/// here — `PersonFaceItem` exposes only face/asset ids and a rel_path).
private struct PersonFaceCellView: View {
    let face: PersonFaceItem
    let client: APIClient?

    var body: some View {
        AuthenticatedImageView(
            assetId: face.assetId,
            client: client,
            type: .thumbnail
        )
        .frame(minHeight: 120)
        .clipped()
        .background(Color.gray.opacity(0.1))
        .aspectRatio(1, contentMode: .fill)
        .cornerRadius(2)
        .contentShape(Rectangle())
    }
}
