import SwiftUI
import LumiverbKit

/// Face overlay for the iOS lightbox. Loads face boxes for the current
/// asset and renders them as tappable rectangles. Tapping opens an
/// assignment sheet that lets the user pick an existing person or
/// create a new one.
///
/// Layout: aspect-fit math identical to the macOS `FaceOverlayView`,
/// scaled to whatever frame the view is given. The view is meant to
/// be overlaid on top of the lightbox image with the same frame.
struct iOSFaceOverlayView: View {
    let assetId: String
    let imageWidth: Int
    let imageHeight: Int
    let client: APIClient?

    @StateObject private var vm = iOSFaceOverlayViewModel()

    var body: some View {
        GeometryReader { proxy in
            let imgRect = aspectFitRect(
                contentSize: CGSize(width: imageWidth, height: imageHeight),
                in: proxy.size
            )
            ZStack(alignment: .topLeading) {
                Color.clear.allowsHitTesting(false)

                ForEach(vm.faces) { face in
                    if let bb = face.boundingBox {
                        let boxW = CGFloat(bb.width) * imgRect.width
                        let boxH = CGFloat(bb.height) * imgRect.height
                        let boxX = imgRect.minX + CGFloat(bb.x) * imgRect.width
                        let boxY = imgRect.minY + CGFloat(bb.y) * imgRect.height
                        faceBox(face: face)
                            .frame(width: boxW, height: boxH)
                            .contentShape(Rectangle())
                            .onTapGesture { vm.selectedFace = face }
                            .position(x: boxX + boxW / 2, y: boxY + boxH / 2)
                    }
                }
            }
        }
        .task(id: assetId) {
            await vm.loadFaces(client: client, assetId: assetId)
        }
        .sheet(item: $vm.selectedFace) { face in
            iOSFaceAssignmentSheet(
                face: face,
                client: client,
                onMutated: {
                    // Refetch from the server so the face list reflects
                    // the new assignment. Local patching would require
                    // a public FaceListItem init, and a refetch is one
                    // network call so it's fine.
                    Task { await vm.loadFaces(client: client, assetId: assetId) }
                }
            )
        }
    }

    @ViewBuilder
    private func faceBox(face: FaceListItem) -> some View {
        let identified = face.person != nil && face.person?.dismissed != true
        let borderColor: Color = identified ? .green : .gray
        ZStack(alignment: .bottom) {
            Rectangle()
                .stroke(borderColor, lineWidth: 2)
            if let person = face.person, !person.dismissed {
                Text(person.displayName)
                    .font(.caption2.weight(.medium))
                    .lineLimit(1)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.green.opacity(0.85))
                    .foregroundColor(.white)
                    .clipShape(Capsule())
                    .offset(y: 14)
            }
        }
    }

    /// Compute the aspect-fit rect of `contentSize` inside `containerSize`.
    /// Mirrors the helper in the shared FaceOverlayView.
    private func aspectFitRect(contentSize: CGSize, in containerSize: CGSize) -> CGRect {
        let contentRatio = contentSize.width / contentSize.height
        let containerRatio = containerSize.width / containerSize.height
        var rect = CGRect.zero
        if contentRatio > containerRatio {
            rect.size.width = containerSize.width
            rect.size.height = containerSize.width / contentRatio
            rect.origin.x = 0
            rect.origin.y = (containerSize.height - rect.size.height) / 2
        } else {
            rect.size.height = containerSize.height
            rect.size.width = containerSize.height * contentRatio
            rect.origin.y = 0
            rect.origin.x = (containerSize.width - rect.size.width) / 2
        }
        return rect
    }
}

// MARK: - View model

@MainActor
final class iOSFaceOverlayViewModel: ObservableObject {
    @Published var faces: [FaceListItem] = []
    @Published var selectedFace: FaceListItem?
    @Published var isLoading = false

    func loadFaces(client: APIClient?, assetId: String) async {
        guard let client else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            let response: FaceListResponse = try await client.get(
                "/v1/assets/\(assetId)/faces"
            )
            faces = response.faces
        } catch {
            faces = []
        }
    }

}

// MARK: - Assignment sheet

/// Sheet shown when the user taps a face. Lets them assign to an
/// existing person, create a new person, or remove an existing
/// assignment.
struct iOSFaceAssignmentSheet: View {
    let face: FaceListItem
    let client: APIClient?
    let onMutated: () -> Void

    @Environment(\.dismiss) private var dismiss

    @State private var nearestPeople: [NearestPersonItem] = []
    @State private var isLoadingNearest = false
    @State private var newPersonName: String = ""
    @State private var isMutating = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            List {
                if let person = face.person, !person.dismissed {
                    Section("Currently Assigned") {
                        HStack {
                            Image(systemName: "person.crop.circle.fill")
                                .foregroundColor(.green)
                            Text(person.displayName)
                            Spacer()
                            Button("Remove") {
                                Task { await unassign() }
                            }
                            .disabled(isMutating)
                        }
                    }
                }

                if !nearestPeople.isEmpty {
                    Section("Looks Like…") {
                        ForEach(nearestPeople) { person in
                            Button {
                                Task { await assign(toPersonId: person.personId, name: person.displayName) }
                            } label: {
                                HStack {
                                    Image(systemName: "person.crop.circle")
                                        .foregroundColor(.accentColor)
                                    Text(person.displayName)
                                    Spacer()
                                    Text("\(person.faceCount)")
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                }
                            }
                            .disabled(isMutating)
                        }
                    }
                } else if isLoadingNearest {
                    Section {
                        HStack {
                            ProgressView().controlSize(.small)
                            Text("Looking for matches…")
                                .foregroundColor(.secondary)
                        }
                    }
                }

                Section("New Person") {
                    HStack {
                        TextField("Name", text: $newPersonName)
                            .textInputAutocapitalization(.words)
                            .disableAutocorrection(true)
                        Button("Add") {
                            Task { await assign(newName: newPersonName) }
                        }
                        .disabled(newPersonName.trimmingCharacters(in: .whitespaces).isEmpty || isMutating)
                    }
                }

                if let error {
                    Section {
                        Text(error)
                            .foregroundColor(.red)
                            .font(.caption)
                    }
                }
            }
            .navigationTitle("Tag Face")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                }
            }
            .task {
                await loadNearestPeople()
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func loadNearestPeople() async {
        guard let client else { return }
        isLoadingNearest = true
        defer { isLoadingNearest = false }
        do {
            // Server returns a bare list, not wrapped in an envelope.
            let people: [NearestPersonItem] = try await client.get(
                "/v1/faces/\(face.faceId)/nearest-people",
                query: ["limit": "5"]
            )
            // Filter out the currently-assigned person.
            let currentId = face.person?.personId
            nearestPeople = people.filter { $0.personId != currentId }
        } catch {
            nearestPeople = []
        }
    }

    private func assign(toPersonId personId: String, name: String) async {
        guard let client else { return }
        isMutating = true
        error = nil
        defer { isMutating = false }
        do {
            // If face already has a person, unassign first — the API
            // rejects silent reassignment.
            if face.person != nil {
                try await client.delete("/v1/faces/\(face.faceId)/assign")
            }
            let body = FaceAssignRequest(personId: personId)
            let _: FaceAssignResponse = try await client.post(
                "/v1/faces/\(face.faceId)/assign",
                body: body
            )
            onMutated()
            dismiss()
        } catch {
            self.error = "Assignment failed: \(error.localizedDescription)"
        }
    }

    private func assign(newName: String) async {
        let trimmed = newName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let client else { return }
        isMutating = true
        error = nil
        defer { isMutating = false }
        do {
            if face.person != nil {
                try await client.delete("/v1/faces/\(face.faceId)/assign")
            }
            let body = FaceAssignRequest(newPersonName: trimmed)
            let _: FaceAssignResponse = try await client.post(
                "/v1/faces/\(face.faceId)/assign",
                body: body
            )
            onMutated()
            dismiss()
        } catch {
            self.error = "Create failed: \(error.localizedDescription)"
        }
    }

    private func unassign() async {
        guard let client else { return }
        isMutating = true
        error = nil
        defer { isMutating = false }
        do {
            try await client.delete("/v1/faces/\(face.faceId)/assign")
            onMutated()
            dismiss()
        } catch {
            self.error = "Remove failed: \(error.localizedDescription)"
        }
    }
}
