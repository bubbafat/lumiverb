import Foundation

// MARK: - Cluster summary (cluster review view)

/// One cluster in the response from `GET /v1/faces/clusters` — the bounded
/// summary endpoint that powers the "name this person" cluster review view.
///
/// `faces` is a small *sample* (server cap: `faces_per_cluster`, default 6,
/// max 20) of representative faces for thumbnail display. To page through
/// every face in the cluster, call `GET /v1/faces/clusters/{i}/faces`
/// which returns the full list via `ClusterFacesResponse`.
public struct ClusterItem: Decodable, Identifiable, Sendable {
    public let clusterIndex: Int
    public let size: Int
    public let faces: [PersonFaceItem]

    public var id: Int { clusterIndex }
}

/// Response from `GET /v1/faces/clusters`. **Not** cursor-paginated — this
/// is a bounded summary view (max 50 clusters per response). Pagination
/// for individual cluster contents lives on `/clusters/{i}/faces`.
///
/// `truncated` is true when the underlying SQL hit its `max_faces` cap
/// (5,000 unassigned faces), meaning there are more faces in the database
/// than fit in this clustering pass. UI should show a "+ more" hint and
/// suggest the user name some clusters first to free up the budget.
///
/// `maxClusterSize` is the size of the largest cluster *before* the
/// per-response cluster cap, useful for showing aggregate stats even when
/// the cluster list itself is truncated.
public struct ClustersResponse: Decodable, Sendable {
    public let clusters: [ClusterItem]
    public let truncated: Bool
    public let maxClusterSize: Int
}

// MARK: - Cluster detail (full face list for one cluster)

/// Response from `GET /v1/faces/clusters/{cluster_index}/faces` — the full
/// face list for one cluster, cursor-paginated.
///
/// Used when the user expands a `ClusterCard` to "see all" faces, and by
/// the cluster-scoped lightbox so they can scroll through every face the
/// algorithm grouped together (handy for spotting wrong-cluster faces).
public struct ClusterFacesResponse: Decodable, Sendable {
    public let items: [PersonFaceItem]
    public let total: Int
    public let nextCursor: String?
}

// MARK: - Cluster mutations

/// Body for `POST /v1/faces/clusters/{cluster_index}/name`.
///
/// Two mutually-exclusive modes:
/// - **Create new person**: pass `displayName` only. The server creates a
///   new person with that name and assigns every face in the cluster to it.
/// - **Merge into existing**: pass `personId` only (and optionally an empty
///   `displayName`). The server assigns every face in the cluster to the
///   existing person and recomputes their centroid.
///
/// On success the server returns the resulting `PersonItem` (the new
/// person, or the existing one with an updated face count).
public struct ClusterNameRequest: Encodable, Sendable {
    public let displayName: String?
    public let personId: String?

    /// Create a new person from this cluster.
    public init(newPersonName: String) {
        self.displayName = newPersonName
        self.personId = nil
    }

    /// Merge this cluster into an existing person.
    public init(existingPersonId: String) {
        self.displayName = nil
        self.personId = existingPersonId
    }
}

/// Response from `POST /v1/faces/clusters/{cluster_index}/dismiss`.
///
/// `personId` is the ID of the *dismissed person* the server creates to
/// represent this rejected cluster — the cluster's faces get attached to
/// it so they don't keep showing up in future cluster runs. Pass this ID
/// to `DELETE /v1/people/{personId}` to *undo* a dismissal within the
/// 5-second toast window.
public struct ClusterDismissResult: Decodable, Sendable {
    public let personId: String
}
