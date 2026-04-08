import Foundation

// MARK: - Face bounding box

/// A face bounding box in normalized image coordinates (0...1).
///
/// **Two on-the-wire formats.** The server's `bounding_box_json` column is
/// a free-form dict that's whatever the detection provider wrote at face
/// submission time, and we have two providers in production:
///
/// - The Python InsightFace path (`src/client/workers/faces/insightface_provider.py`)
///   stores faces as `{"x": ..., "y": ..., "w": ..., "h": ...}` (top-left
///   origin + width/height).
/// - The Swift Vision path (`Sources/macOS/Enrich/FaceDetectionProvider.swift`,
///   via `FacesSubmitRequest.BoundingBox`) stores faces as
///   `{"x1": ..., "y1": ..., "x2": ..., "y2": ...}` (top-left + bottom-right).
///
/// The same library can contain faces from both providers depending on
/// when each asset was last enriched, so the read path has to handle either.
/// This struct's custom `init(from:)` accepts both shapes and normalizes
/// internally to `(x, y, width, height)`. The `x2` / `y2` accessors are
/// computed for callers that want the bottom-right corner directly.
public struct FaceBoundingBox: Decodable, Sendable, Equatable {
    /// Top-left x as fraction of image width, 0...1.
    public let x: Float
    /// Top-left y as fraction of image height, 0...1.
    public let y: Float
    /// Width as fraction of image width, 0...1.
    public let width: Float
    /// Height as fraction of image height, 0...1.
    public let height: Float

    public var x2: Float { x + width }
    public var y2: Float { y + height }

    public init(x: Float, y: Float, width: Float, height: Float) {
        self.x = x; self.y = y; self.width = width; self.height = height
    }

    private enum CodingKeys: String, CodingKey {
        case x, y, w, h
        case x1, y1, x2, y2
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)

        // Prefer the {x1, y1, x2, y2} shape if x1 is present.
        if let x1 = try container.decodeIfPresent(Float.self, forKey: .x1) {
            let y1 = try container.decode(Float.self, forKey: .y1)
            let x2 = try container.decode(Float.self, forKey: .x2)
            let y2 = try container.decode(Float.self, forKey: .y2)
            self.x = x1
            self.y = y1
            self.width = x2 - x1
            self.height = y2 - y1
            return
        }

        // Otherwise expect {x, y, w, h}.
        if let x = try container.decodeIfPresent(Float.self, forKey: .x) {
            self.x = x
            self.y = try container.decode(Float.self, forKey: .y)
            self.width = try container.decode(Float.self, forKey: .w)
            self.height = try container.decode(Float.self, forKey: .h)
            return
        }

        throw DecodingError.dataCorruptedError(
            forKey: .x,
            in: container,
            debugDescription: "FaceBoundingBox must have either {x1,y1,x2,y2} or {x,y,w,h}"
        )
    }
}

// MARK: - Face list (lightbox overlay)

/// Person attribution attached to a face in `GET /v1/assets/{id}/faces`.
///
/// The server projects this from the `people` table at read time:
///
///     {"person_id": p.person_id, "display_name": p.display_name, "dismissed": p.dismissed}
///
/// `dismissed` is included so the lightbox can render dismissed-cluster
/// faces in the same gray "unidentified" style as truly unassigned faces
/// instead of misleadingly showing the dismissed-person's display name.
public struct FaceMatchedPerson: Decodable, Sendable, Equatable {
    public let personId: String
    public let displayName: String
    public let dismissed: Bool
}

/// One detected face on an asset. Used by the lightbox face overlay.
public struct FaceListItem: Decodable, Identifiable, Sendable {
    public let faceId: String
    public let boundingBox: FaceBoundingBox?
    public let detectionConfidence: Float?
    public let person: FaceMatchedPerson?

    public var id: String { faceId }
}

/// Response from `GET /v1/assets/{asset_id}/faces`.
public struct FaceListResponse: Decodable, Sendable {
    public let faces: [FaceListItem]
}

// MARK: - Face assignment

/// Body for `POST /v1/faces/{face_id}/assign`.
///
/// Two mutually-exclusive modes — assign to an existing person by ID, or
/// create a new person on the fly with the given name and assign this
/// face to it. The server returns 422 if both or neither are set.
///
/// Note: the server returns 409 if the face already has a person. The
/// reassign flow must `DELETE /v1/faces/{face_id}/assign` first, then
/// `POST` again — the API rejects silent reassignment by design.
public struct FaceAssignRequest: Encodable, Sendable {
    public let personId: String?
    public let newPersonName: String?

    public init(personId: String) {
        self.personId = personId
        self.newPersonName = nil
    }

    public init(newPersonName: String) {
        self.personId = nil
        self.newPersonName = newPersonName
    }
}

/// Response from `POST /v1/faces/{face_id}/assign`. The server returns
/// the resolved person — either the existing one whose id was passed in,
/// or the newly-created one when `new_person_name` was used.
public struct FaceAssignResponse: Decodable, Sendable {
    public let personId: String
    public let displayName: String
}
