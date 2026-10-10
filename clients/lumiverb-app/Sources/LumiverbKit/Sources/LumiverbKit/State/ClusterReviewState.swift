import SwiftUI

/// Observable state for the cluster review panel (Phase 6 M5 of ADR-014).
///
/// Powers the "name this person" flow over the bounded
/// `GET /v1/faces/clusters` summary endpoint. Each cluster card has its
/// own lazy nearest-people fetch (cached here so revisiting doesn't
/// re-hit the server) and its own optimistic removal on a successful
/// name / dismiss. Clusters are named by `clusterId` (their faces), so a
/// card keeps naming the same faces however the list is reloaded; one that
/// changed since answers 409 cluster_changed.
@MainActor
public final class ClusterReviewState: ObservableObject {

    // MARK: - Cluster list

    @Published public var clusters: [ClusterItem] = []
    @Published public var truncated: Bool = false
    @Published public var maxClusterSize: Int = 0
    /// Faces changed since the clusters were computed; upkeep regroups them.
    @Published public var pending: Bool = false
    @Published public var isLoading: Bool = false
    @Published public var error: String?

    // MARK: - Per-cluster suggestion cache

    /// Lazily-fetched nearest-people suggestions, keyed by `clusterId`.
    /// Cleared whenever `loadClusters()` runs, since named people change them.
    @Published public var nearestPeople: [String: [NearestPersonItem]] = [:]
    private var inFlightNearest: Set<String> = []

    // MARK: - Per-cluster mutation state

    /// Clusters currently being named/dismissed. Drives spinners
    /// and disables further actions on those cards.
    @Published public var pendingMutations: Set<String> = []

    /// Last dismiss result, for the undo toast. The server returns the
    /// dismissed-person id; deleting that person undoes the dismissal
    /// (Phase 6 M5 toast window — auto-clears after 5s).
    @Published public var lastDismissedPersonId: String?
    @Published public var lastDismissedClusterId: String?
    private var undoExpiryTask: Task<Void, Never>?

    public init(client: APIClient?) {
        self.client = client
    }

    public let client: APIClient?

    // MARK: - Loading

    func loadIfNeeded() async {
        if clusters.isEmpty, error == nil {
            await loadClusters()
        }
    }

    /// Fetch the clusters as upkeep last computed them (reading never
    /// recomputes; `pending` says faces changed since).
    func loadClusters() async {
        guard let client else { return }

        isLoading = true
        error = nil
        defer { isLoading = false }

        nearestPeople = [:]

        do {
            let response: ClustersResponse = try await client.get(
                "/v1/faces/clusters",
                query: [
                    "limit": "50",
                    "faces_per_cluster": "8",
                    "min_cluster_size": "3",
                ]
            )
            clusters = response.clusters
            truncated = response.truncated
            maxClusterSize = response.maxClusterSize
            pending = response.pending
        } catch {
            if Self.isCancellation(error) { return }
            self.error = "Failed to load clusters: \(error)"
        }
    }

    // MARK: - Nearest people (suggestions)

    /// Lazy fetch of suggested people for `clusterId`. Idempotent —
    /// safe to call from `onAppear` on every card; the in-flight set
    /// prevents duplicate parallel fetches for the same cluster.
    func loadNearestPeople(forCluster clusterId: String) async {
        guard let client else { return }
        if nearestPeople[clusterId] != nil { return }
        if inFlightNearest.contains(clusterId) { return }
        inFlightNearest.insert(clusterId)
        defer { inFlightNearest.remove(clusterId) }

        do {
            let people: [NearestPersonItem] = try await client.get(
                "/v1/faces/clusters/\(clusterId)/nearest-people",
                query: ["limit": "5"]
            )
            nearestPeople[clusterId] = people
        } catch {
            // Non-fatal — empty suggestion list is the fallback.
            if !Self.isCancellation(error) {
                nearestPeople[clusterId] = []
            }
        }
    }

    // MARK: - Mutations

    /// Create a new person from this whole cluster, then optimistically
    /// drop the card from the visible list.
    func nameCluster(_ clusterId: String, newPersonName name: String) async {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        await mutate(clusterId) { client in
            let _: PersonItem = try await client.post(
                "/v1/faces/clusters/\(clusterId)/name",
                body: ClusterNameRequest(newPersonName: trimmed)
            )
        }
    }

    /// Merge this whole cluster into an existing person.
    func mergeCluster(_ clusterId: String, intoPersonId personId: String) async {
        await mutate(clusterId) { client in
            let _: PersonItem = try await client.post(
                "/v1/faces/clusters/\(clusterId)/name",
                body: ClusterNameRequest(existingPersonId: personId)
            )
        }
    }

    /// Dismiss the cluster as not-a-person (or noise). Captures the new
    /// dismissed-person id so the undo toast can DELETE it inside the
    /// 5-second window.
    func dismissCluster(_ clusterId: String) async {
        guard let client else { return }
        pendingMutations.insert(clusterId)
        defer { pendingMutations.remove(clusterId) }

        do {
            let result: ClusterDismissResult = try await client.post(
                "/v1/faces/clusters/\(clusterId)/dismiss"
            )
            removeCluster(clusterId)
            startUndoWindow(personId: result.personId, clusterId: clusterId)
        } catch {
            if Self.isCancellation(error) { return }
            self.error = "Failed to dismiss cluster: \(error)"
        }
    }

    /// Undo the most recent dismissal by deleting the dismissed-person
    /// the server created. Only valid inside the 5-second window — after
    /// it expires the toast disappears and this is unreachable.
    func undoLastDismiss() async {
        guard let client, let personId = lastDismissedPersonId else { return }
        cancelUndoWindow()
        do {
            try await client.delete("/v1/people/\(personId)")
            // Reload — the faces come back as a cluster once upkeep
            // computes them again.
            await loadClusters()
        } catch {
            if Self.isCancellation(error) { return }
            self.error = "Failed to undo dismiss: \(error)"
        }
    }

    private func startUndoWindow(personId: String, clusterId: String) {
        cancelUndoWindow()
        lastDismissedPersonId = personId
        lastDismissedClusterId = clusterId
        undoExpiryTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(5))
            guard !Task.isCancelled else { return }
            await MainActor.run {
                self?.lastDismissedPersonId = nil
                self?.lastDismissedClusterId = nil
            }
        }
    }

    private func cancelUndoWindow() {
        undoExpiryTask?.cancel()
        undoExpiryTask = nil
        lastDismissedPersonId = nil
        lastDismissedClusterId = nil
    }

    // MARK: - Helpers

    /// Run a name/merge mutation, then optimistically drop the card.
    /// Errors land on `self.error` so the user can retry from a banner.
    private func mutate(
        _ clusterId: String,
        op: (APIClient) async throws -> Void
    ) async {
        guard let client else { return }
        pendingMutations.insert(clusterId)
        defer { pendingMutations.remove(clusterId) }
        do {
            try await op(client)
            removeCluster(clusterId)
        } catch {
            if Self.isCancellation(error) { return }
            self.error = "Failed to update cluster: \(error)"
        }
    }

    private func removeCluster(_ clusterId: String) {
        clusters.removeAll { $0.clusterId == clusterId }
        nearestPeople.removeValue(forKey: clusterId)
    }

    private static func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        if let urlErr = error as? URLError, urlErr.code == .cancelled { return true }
        if case APIError.networkError(let msg) = error,
           msg.lowercased().contains("cancel") {
            return true
        }
        return false
    }
}
