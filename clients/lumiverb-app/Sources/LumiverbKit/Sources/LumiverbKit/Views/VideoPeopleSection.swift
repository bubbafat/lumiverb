import SwiftUI

/// The people seen in a video, in the lightbox's sidebar: each one's face,
/// name (tap to name them, or change it, with the photos' popover) and the
/// moments they're seen, each playing the video from there. A video's
/// faces have no boxes over the picture: each is from another frame.
struct VideoPeopleSection: View {
    @ObservedObject var vm: LightboxFacesViewModel
    /// Play the video from this many seconds.
    let onPlay: (Double) -> Void

    /// A long video can see someone many times: the first few, then how many more.
    private let momentsShown = 4

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(vm.faces) { face in
                HStack(alignment: .top, spacing: 8) {
                    FaceThumbnailView(faceId: face.faceId, client: vm.client)
                        .frame(width: 36, height: 36)
                        .background(Circle().fill(Color.gray.opacity(0.15)))
                        .clipShape(Circle())
                    VStack(alignment: .leading, spacing: 4) {
                        Button(name(of: face)) { vm.selectFace(face.faceId) }
                            .buttonStyle(.link)
                            .popover(isPresented: popoverBinding(for: face.faceId), arrowEdge: .leading) {
                                FaceAssignmentPopover(vm: vm, face: face)
                            }
                        let moments = FaceMoments.spans(face.seenMs)
                        HStack(spacing: 4) {
                            ForEach(moments.prefix(momentsShown), id: \.self) { moment in
                                Button(moment.label) { onPlay(moment.startSeconds) }
                                    .controlSize(.small)
                                    .monospacedDigit()
                            }
                            if moments.count > momentsShown {
                                Text("+\(moments.count - momentsShown) more")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }
                    }
                }
            }
        }
    }

    private func name(of face: FaceListItem) -> String {
        guard let person = face.person else { return "Who is this?" }
        return person.dismissed ? "Not a person" : person.displayName
    }

    private func popoverBinding(for faceId: String) -> Binding<Bool> {
        Binding(
            get: { vm.selectedFaceId == faceId },
            set: { isShown in
                if !isShown && vm.selectedFaceId == faceId { vm.deselectFace() }
            }
        )
    }
}
