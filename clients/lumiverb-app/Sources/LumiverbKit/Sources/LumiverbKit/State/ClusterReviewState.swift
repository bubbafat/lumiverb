import SwiftUI

/// Observable state for the cluster review panel (Phase 6 M5 of ADR-014).
///
/// Powers the "name this person" flow over the bounded
/// `GET /v1/faces/clusters` summary endpoint. Each cluster card has its
/// own lazy nearest-people fetch (cached here so revisiting doesn't
/// re-hit the server) and its own optimistic removal on a successful
/// name / dismiss — the server's cluster cache preserves indices across
/// rapid mutations so the unnamed cards don't shuffle as the user works
/// down the list.
@MainActor
public final class ClusterReviewState: ObservableObject {

    // MARK: - Cluster list

    @Published public var clusters: [ClusterItem] = []
    @Published public var truncated: Bool = false
    @Published public var maxClusterSize: Int = 0
    @Published public var isLoading: Bool = false
    @Published public var error: String?

    // MARK: - Per-cluster suggestion cache

    /// Lazily-fetched nearest-people suggestions, keyed by `clusterIndex`.
    /// Cleared whenever `loadClusters()` runs because cluster indices
    /// rebind to new content after a recompute.
    @Published public var nearestPeople: [Int: [NearestPersonItem]] = [:]
    private var inFlightNearest: Set<Int> = []

    // MARK: - Per-cluster mutation state

    /// Cluster indices currently being named/dismissed. Drives spinners
    /// and disables further actions on those cards.
    @Published public var pendingMutations: Set<Int> = []

    /// Last dismiss result, for the undo toast. The server returns the
    /// dismissed-person id; deleting that person undoes the dismissal
    /// (Phase 6 M5 toast window — auto-clears after 5s).
    @Published public var lastDismissedPersonId: String?
    @Published public var lastDismissedClusterIndex: Int?
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

    /// Force a fresh fetch — triggers server-side recompute if the cache
    /// has been marked dirty by recent mutations. Resets per-cluster
    /// caches because indices rebind to new content.
    func loadClusters() async {
        guard let client else { return }

        isLoading = true
        error = nil
        defer { isLoading = false }

        // Reset stale per-cluster state — indices won't survive a recompute.
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
        } catch {
            if Self.isCancellation(error) { return }
            self.error = "Failed to load clusters: \(error)"
        }
    }

    // MARK: - Nearest people (suggestions)

    /// Lazy fetch of suggested people for `clusterIndex`. Idempotent —
    /// safe to call from `onAppear` on every card; the in-flight set
    /// prevents duplicate parallel fetches for the same index.
    func loadNearestPeople(forCluster clusterIndex: Int) async {
        guard let client else { return }
        if nearestPeople[clusterIndex] != nil { return }
        if inFlightNearest.contains(clusterIndex) { return }
        inFlightNearest.insert(clusterIndex)
        defer { inFlightNearest.remove(clusterIndex) }

        do {
            let people: [NearestPersonItem] = try await client.get(
                "/v1/faces/clusters/\(clusterIndex)/nearest-people",
                query: ["limit": "5"]
            )
            nearestPeople[clusterIndex] = people
        } catch {
            // Non-fatal — empty suggestion list is the fallback.
            if !Self.isCancellation(error) {
                nearestPeople[clusterIndex] = []
            }
        }
    }

    // MARK: - Mutations

    /// Create a new person from this whole cluster, then optimistically
    /// drop the card from the visible list.
    func nameCluster(_ clusterIndex: Int, newPersonName name: String) async {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        await mutate(clusterIndex) { client in
            let _: PersonItem = try await client.post(
                "/v1/faces/clusters/\(clusterIndex)/name",
                body: ClusterNameRequest(newPersonName: trimmed)
            )
        }
    }

    /// Merge this whole cluster into an existing person.
    func mergeCluster(_ clusterIndex: Int, intoPersonId personId: String) async {
        await mutate(clusterIndex) { client in
            let _: PersonItem = try await client.post(
                "/v1/faces/clusters/\(clusterIndex)/name",
                body: ClusterNameRequest(existingPersonId: personId)
            )
        }
    }

    /// Dismiss the cluster as not-a-person (or noise). Captures the new
    /// dismissed-person id so the undo toast can DELETE it inside the
    /// 5-second window.
    func dismissCluster(_ clusterIndex: Int) async {
        guard let client else { return }
        pendingMutations.insert(clusterIndex)
        defer { pendingMutations.remove(clusterIndex) }

        do {
            let result: ClusterDismissResult = try await client.post(
                "/v1/faces/clusters/\(clusterIndex)/dismiss"
            )
            removeCluster(clusterIndex)
            startUndoWindow(personId: result.personId, clusterIndex: clusterIndex)
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
            // Reload — the unassigned faces will reform a cluster (likely
            // at a new index after the cache recomputes).
            await loadClusters()
        } catch {
            if Self.isCancellation(error) { return }
            self.error = "Failed to undo dismiss: \(error)"
        }
    }

    private func startUndoWindow(personId: String, clusterIndex: Int) {
        cancelUndoWindow()
        lastDismissedPersonId = personId
        lastDismissedClusterIndex = clusterIndex
        undoExpiryTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(5))
            guard !Task.isCancelled else { return }
            await MainActor.run {
                self?.lastDismissedPersonId = nil
                self?.lastDismissedClusterIndex = nil
            }
        }
    }

    private func cancelUndoWindow() {
        undoExpiryTask?.cancel()
        undoExpiryTask = nil
        lastDismissedPersonId = nil
        lastDismissedClusterIndex = nil
    }

    // MARK: - Helpers

    /// Run a name/merge mutation, then optimistically drop the card.
    /// Errors land on `self.error` so the user can retry from a banner.
    private func mutate(
        _ clusterIndex: Int,
        op: (APIClient) async throws -> Void
    ) async {
        guard let client else { return }
        pendingMutations.insert(clusterIndex)
        defer { pendingMutations.remove(clusterIndex) }
        do {
            try await op(client)
            removeCluster(clusterIndex)
        } catch {
            if Self.isCancellation(error) { return }
            self.error = "Failed to update cluster: \(error)"
        }
    }

    private func removeCluster(_ clusterIndex: Int) {
        clusters.removeAll { $0.clusterIndex == clusterIndex }
        nearestPeople.removeValue(forKey: clusterIndex)
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
