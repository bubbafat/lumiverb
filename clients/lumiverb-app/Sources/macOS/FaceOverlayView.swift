import SwiftUI
import LumiverbKit

/// View model for the lightbox face overlay (M2 of ADR-014 Phase 6).
///
/// Owns the per-asset face list and the in-flight loading state. Hosted as
/// a `@StateObject` inside `LightboxView` so its lifetime matches the open
/// lightbox; the view triggers `loadFaces(forAsset:)` from a `.task(id:)`
/// keyed on the current asset's id, so navigation between assets cancels
/// the previous fetch and starts a new one automatically.
///
/// **State scope.** This is a small view-scoped state object — it does not
/// live on `BrowseState`. The same model will be reused in M4 (face
/// assignment in the lightbox) where it'll grow to handle assign/dismiss
/// mutations and cache invalidation. People-page and cluster-review state
/// (M3, M5) will get their own separate `@ObservedObject` since they're
/// not lightbox-scoped.
@MainActor
final class LightboxFacesViewModel: ObservableObject {
    @Published var faces: [FaceListItem] = []
    @Published var isLoading: Bool = false

    private let client: APIClient?
    private var loadedAssetId: String?

    init(client: APIClient?) {
        self.client = client
    }

    /// Fetch faces for `assetId` from `GET /v1/assets/{id}/faces`. Skips the
    /// network call if the same asset's faces are already cached on this
    /// view model. Resets to an empty list on error so the lightbox doesn't
    /// keep showing stale boxes from the previous asset.
    func loadFaces(forAsset assetId: String) async {
        guard let client else { return }
        if loadedAssetId == assetId, !faces.isEmpty { return }
        loadedAssetId = assetId
        isLoading = true
        defer { isLoading = false }

        do {
            let response: FaceListResponse = try await client.get("/v1/assets/\(assetId)/faces")
            self.faces = response.faces
        } catch {
            // Non-fatal — face overlay just stays empty for this asset.
            self.faces = []
        }
    }

    /// Forget any cached faces. Called when the user toggles "Show faces"
    /// off so reopening with a different asset doesn't briefly flash old
    /// boxes from a stale state.
    func reset() {
        self.faces = []
        self.loadedAssetId = nil
    }
}

// MARK: - Face overlay layer

/// Draws bounding-box overlays for every face on the current lightbox
/// asset. Sits as a sibling layer in `LightboxView`'s `ZStack`, NOT
/// inside `AuthenticatedImageView`, so the existing image-loading view
/// stays untouched. Both layers receive the same parent frame from the
/// ZStack and apply the same aspect-fit math against the asset's natural
/// dimensions, so the overlay's coordinate space matches the rendered
/// image rect by construction.
///
/// `allowsHitTesting(false)` for M2 — the overlay is read-only. Click
/// handling on individual boxes lands in M4.
struct FaceOverlayView: View {
    let faces: [FaceListItem]
    /// Original asset width / height in pixels, from `AssetDetail`. The
    /// aspect-fit math only needs the ratio, so the proxy being a scaled
    /// copy of the source doesn't matter as long as the scaler preserves
    /// the aspect ratio (which it does — `ProxyGenerator` uses
    /// `kCGImageSourceThumbnailMaxPixelSize` which is uniform-scale).
    let imageWidth: Int
    let imageHeight: Int

    var body: some View {
        GeometryReader { proxy in
            let imgRect = aspectFitRect(
                contentSize: CGSize(width: imageWidth, height: imageHeight),
                in: proxy.size
            )
            ZStack(alignment: .topLeading) {
                Color.clear
                ForEach(faces) { face in
                    if let bb = face.boundingBox {
                        let boxW = CGFloat(bb.width) * imgRect.width
                        let boxH = CGFloat(bb.height) * imgRect.height
                        let boxX = imgRect.minX + CGFloat(bb.x) * imgRect.width
                        let boxY = imgRect.minY + CGFloat(bb.y) * imgRect.height
                        FaceBoxView(person: face.person, label: labelText(face.person))
                            .frame(width: boxW, height: boxH)
                            .position(x: boxX + boxW / 2, y: boxY + boxH / 2)
                    }
                }
            }
        }
        .allowsHitTesting(false)
    }

    /// `nil` for unidentified or dismissed faces — those render with a
    /// gray border and no name label. Identified faces show their
    /// person's display name in a small chip below the box.
    private func labelText(_ person: FaceMatchedPerson?) -> String? {
        guard let person, !person.dismissed else { return nil }
        return person.displayName
    }
}

/// One face's bounding box + (optional) name label. Color and label
/// presence depend on the person attribution:
///
/// - Identified (assigned to a non-dismissed person): green border, name label below.
/// - Unidentified (no person, or assigned to a dismissed person): gray border, no label.
///
/// The label is rendered as an overlay anchored to the bottom edge so it
/// always sits just below the box regardless of how the box is positioned
/// within the parent.
struct FaceBoxView: View {
    let person: FaceMatchedPerson?
    let label: String?

    private var isIdentified: Bool {
        guard let person else { return false }
        return !person.dismissed
    }

    private var borderColor: Color {
        isIdentified ? Color.green : Color.gray
    }

    var body: some View {
        Rectangle()
            .stroke(borderColor, lineWidth: 2)
            .background(Color.clear)
            .overlay(alignment: .bottom) {
                if let label {
                    Text(label)
                        .font(.caption2)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .foregroundColor(.white)
                        .padding(.horizontal, 4)
                        .padding(.vertical, 1)
                        .background(Color.black.opacity(0.7))
                        .cornerRadius(2)
                        .alignmentGuide(.bottom) { d in d[.top] }  // sit just below the box edge
                        .padding(.top, 2)
                }
            }
    }
}

// MARK: - Aspect-fit math

/// The centered aspect-fit rect for `contentSize` inside `frameSize`.
///
/// This is the same math `Image.resizable().aspectRatio(contentMode: .fit)`
/// uses internally to letterbox a content image into its frame. We compute
/// it ourselves so the face overlay layer can place absolute pixel boxes
/// in the same coordinate space as the rendered image without having to
/// inspect `AuthenticatedImageView`'s internals.
func aspectFitRect(contentSize: CGSize, in frameSize: CGSize) -> CGRect {
    guard contentSize.width > 0, contentSize.height > 0,
          frameSize.width > 0, frameSize.height > 0
    else { return .zero }

    let contentAspect = contentSize.width / contentSize.height
    let frameAspect = frameSize.width / frameSize.height

    if contentAspect > frameAspect {
        // Wider than the frame — letterbox top/bottom.
        let h = frameSize.width / contentAspect
        return CGRect(
            x: 0,
            y: (frameSize.height - h) / 2,
            width: frameSize.width,
            height: h
        )
    } else {
        // Taller than the frame — letterbox left/right.
        let w = frameSize.height * contentAspect
        return CGRect(
            x: (frameSize.width - w) / 2,
            y: 0,
            width: w,
            height: frameSize.height
        )
    }
}
