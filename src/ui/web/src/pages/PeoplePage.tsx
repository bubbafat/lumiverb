import { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { Link } from "react-router-dom";
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import type { AssetPageItem } from "../api/types";
import { Lightbox } from "../components/Lightbox";
import {
  listPeople,
  getClusters,
  listClusterFaces,
  nameCluster,
  dismissCluster,
  deletePerson,
  getNearestPeople,
  searchPeople,
  getApiKey,
} from "../api/client";
import type { PersonItem, ClusterItem, PersonFaceItem } from "../api/client";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";

function InfiniteScrollSentinel({
  hasNextPage,
  isFetchingNextPage,
  fetchNextPage,
}: {
  hasNextPage?: boolean;
  isFetchingNextPage: boolean;
  fetchNextPage: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!hasNextPage || isFetchingNextPage) return;
    const el = ref.current;
    if (!el) return;

    const observer = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) fetchNextPage(); },
      { rootMargin: "200px" },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  if (!hasNextPage) return null;

  return (
    <div ref={ref} className="mt-4 flex justify-center py-4">
      {isFetchingNextPage && (
        <div className="h-6 w-6 animate-spin rounded-full border-2 border-gray-600 border-t-white" />
      )}
    </div>
  );
}

/** Fetch a face crop thumbnail with auth. Falls back to null if no crop available. */
function useFaceCrop(faceId: string): { url: string | null; isLoading: boolean } {
  const [url, setUrl] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    if (!faceId) { setIsLoading(false); return; }
    setIsLoading(true);
    let objectUrl: string | null = null;
    let cancelled = false;

    const key = getApiKey();
    const headers: HeadersInit = key ? { Authorization: `Bearer ${key}` } : {};

    fetch(`/v1/faces/${faceId}/crop`, { headers })
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) return; // no crop available — stay null
        const blob = await res.blob();
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .finally(() => { if (!cancelled) setIsLoading(false); });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setUrl(null);
    };
  }, [faceId]);

  return { url, isLoading };
}

function PersonCard({ person }: { person: PersonItem }) {
  const { url, isLoading } = useAuthenticatedImage(
    person.representative_asset_id ?? "",
    "thumbnail",
    { enabled: !!person.representative_asset_id },
  );

  return (
    <Link
      to={`/people/${person.person_id}`}
      className="group flex flex-col items-center gap-2 rounded-xl bg-gray-800/50 p-4 transition-colors hover:bg-gray-700/50"
    >
      <div className="h-24 w-24 overflow-hidden rounded-full bg-gray-700">
        {isLoading ? (
          <div className="h-full w-full animate-pulse bg-gray-600" />
        ) : url ? (
          <img
            src={url}
            alt={person.display_name}
            className="h-full w-full object-cover"
          />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-2xl text-gray-500">
            {person.display_name.charAt(0).toUpperCase()}
          </div>
        )}
      </div>
      <span className="text-sm font-medium text-gray-200 group-hover:text-white">
        {person.display_name}
      </span>
      <span className="text-xs text-gray-500">
        {person.face_count} {person.face_count === 1 ? "photo" : "photos"}
      </span>
    </Link>
  );
}

function ClusterFaceThumbnail({ face, onClick }: { face: PersonFaceItem; onClick?: () => void }) {
  // Prefer server-generated face crop; fall back to full asset thumbnail with bounding box overlay
  const crop = useFaceCrop(face.face_id);
  const hasCrop = !!crop.url;
  const fallback = useAuthenticatedImage(face.asset_id, "thumbnail", { enabled: !hasCrop && !crop.isLoading });

  const url = crop.url ?? fallback.url;
  const isLoading = crop.isLoading || (!hasCrop && fallback.isLoading);
  const box = face.bounding_box;

  return (
    <button
      type="button"
      onClick={onClick}
      className={`relative aspect-square overflow-hidden rounded-lg bg-gray-800 w-full focus:outline-none focus:ring-2 focus:ring-indigo-500 ${hasCrop ? "ring-2 ring-red-500/60" : ""}`}
    >
      {isLoading ? (
        <div className="h-full w-full animate-pulse bg-gray-700" />
      ) : url ? (
        <>
          <img src={url} alt={face.rel_path ?? ""} className="h-full w-full object-cover" />
          {/* If showing full thumbnail (no crop), overlay bounding box in red to highlight the clustered face */}
          {!hasCrop && box && (
            <div
              className="absolute border-2 border-red-500 rounded pointer-events-none"
              style={{
                left: `${box.x * 100}%`,
                top: `${box.y * 100}%`,
                width: `${box.w * 100}%`,
                height: `${box.h * 100}%`,
              }}
            />
          )}
        </>
      ) : (
        <div className="flex h-full w-full items-center justify-center text-gray-600 text-xs">
          No image
        </div>
      )}
    </button>
  );
}

function ClusterCard({
  cluster,
  people,
  fading,
  onProcessed,
  onDismissStarted,
  onDismissComplete,
}: {
  cluster: ClusterItem;
  people: PersonItem[];
  fading: false | "locked" | "fading";
  onProcessed: (clusterIndex: number) => void;
  onDismissStarted: (clusterIndex: number) => void;
  onDismissComplete: (clusterIndex: number, personId: string) => void;
}) {
  const [mode, setMode] = useState<"idle" | "name" | "assign">("idle");
  const [expanded, setExpanded] = useState(false);
  const [newName, setNewName] = useState("");
  const [assignSearch, setAssignSearch] = useState("");
  const [searchResults, setSearchResults] = useState<PersonItem[]>([]);
  const [searching, setSearching] = useState(false);

  // Fetch nearest people when assign mode opens
  const nearestQuery = useQuery({
    queryKey: ["nearest-people", cluster.cluster_index],
    queryFn: () => getNearestPeople(cluster.cluster_index, 5),
    enabled: mode === "assign",
    staleTime: Infinity,
  });

  // Debounced search for assign mode
  useEffect(() => {
    if (mode !== "assign" || !assignSearch.trim()) {
      setSearchResults([]);
      return;
    }
    setSearching(true);
    const timer = setTimeout(async () => {
      try {
        const res = await searchPeople(assignSearch.trim());
        setSearchResults(res.items);
      } catch { /* ignore */ }
      setSearching(false);
    }, 250);
    return () => clearTimeout(timer);
  }, [assignSearch, mode]);

  const allFacesQuery = useInfiniteQuery({
    queryKey: ["cluster-faces", cluster.cluster_index],
    queryFn: ({ pageParam }) => listClusterFaces(cluster.cluster_index, pageParam, 50),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: expanded,
  });

  const allFaces = allFacesQuery.data?.pages.flatMap((p) => p.items) ?? [];
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);

  const displayFaces = expanded ? allFaces : cluster.faces;

  // Build minimal AssetPageItem[] for Lightbox from display faces
  const lightboxAssets: AssetPageItem[] = useMemo(
    () =>
      displayFaces.map((f) => ({
        asset_id: f.asset_id,
        rel_path: f.rel_path ?? "",
        file_size: 0,
        file_mtime: null,
        sha256: null,
        media_type: "image",
        width: null,
        height: null,
        taken_at: null,
        status: "active",
        duration_sec: null,
        camera_make: null,
        camera_model: null,
        iso: null,
        aperture: null,
        focal_length: null,
        focal_length_35mm: null,
        lens_model: null,
        flash_fired: null,
        gps_lat: null,
        gps_lon: null,
        face_count: 1,
        created_at: null,
      })),
    [displayFaces],
  );

  const nameMutation = useMutation({
    mutationFn: (name: string) =>
      nameCluster(cluster.cluster_index, { displayName: name }),
    onSuccess: () => {
      onProcessed(cluster.cluster_index);
    },
  });

  const assignMutation = useMutation({
    mutationFn: (personId: string) =>
      nameCluster(cluster.cluster_index, { personId, displayName: "" }),
    onSuccess: () => {
      onProcessed(cluster.cluster_index);
    },
  });

  const dismissMutation = useMutation({
    mutationFn: () => {
      onDismissStarted(cluster.cluster_index);
      return dismissCluster(cluster.cluster_index);
    },
    onSuccess: (data) => {
      onDismissComplete(cluster.cluster_index, data.person_id);
    },
  });

  return (
    <div
      className={`rounded-xl border border-gray-700 bg-gray-800/30 p-4 transition-all ${expanded ? "col-span-full" : ""} ${fading === "locked" ? "pointer-events-none grayscale opacity-60 duration-150" : fading === "fading" ? "pointer-events-none scale-95 opacity-0 duration-300" : "opacity-100 duration-300"}`}
    >
      <div className="mb-3 flex items-center justify-between">
        <button
          type="button"
          onClick={() => setExpanded(!expanded)}
          className="text-sm font-medium text-gray-300 hover:text-white"
        >
          {cluster.size} {cluster.size === 1 ? "photo" : "photos"}
          {!expanded && cluster.size > cluster.faces.length && (
            <span className="ml-1 text-xs text-gray-500">— click to see all</span>
          )}
          {expanded && (
            <span className="ml-1 text-xs text-gray-500">— click to collapse</span>
          )}
        </button>
      </div>

      {/* Face thumbnails */}
      <div className={`mb-3 grid gap-1.5 ${expanded ? "grid-cols-4 sm:grid-cols-6 md:grid-cols-8 lg:grid-cols-10" : "grid-cols-3"}`}>
        {displayFaces.map((face, i) => (
          <ClusterFaceThumbnail key={face.face_id} face={face} onClick={() => setLightboxIndex(i)} />
        ))}
        {expanded && allFacesQuery.isLoading && (
          Array.from({ length: 6 }).map((_, i) => (
            <div key={`skel-${i}`} className="aspect-square animate-pulse rounded-lg bg-gray-700" />
          ))
        )}
      </div>

      {expanded && (
        <InfiniteScrollSentinel
          hasNextPage={allFacesQuery.hasNextPage}
          isFetchingNextPage={allFacesQuery.isFetchingNextPage}
          fetchNextPage={allFacesQuery.fetchNextPage}
        />
      )}

      {/* Actions */}
      {mode === "idle" && (
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => setMode("name")}
            className="flex-1 rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500"
          >
            Name this person
          </button>
          {people.length > 0 && (
            <button
              type="button"
              onClick={() => setMode("assign")}
              className="flex-1 rounded-lg border border-gray-600 px-3 py-1.5 text-xs font-medium text-gray-300 hover:bg-gray-700"
            >
              This is...
            </button>
          )}
          <button
            type="button"
            onClick={() => dismissMutation.mutate()}
            disabled={dismissMutation.isPending}
            className="rounded-lg border border-gray-600 px-3 py-1.5 text-xs font-medium text-gray-400 hover:text-red-400 hover:border-red-800"
            title="Dismiss — hide this cluster permanently"
          >
            {dismissMutation.isPending ? "..." : "Dismiss"}
          </button>
        </div>
      )}

      {mode === "name" && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (newName.trim()) nameMutation.mutate(newName.trim());
          }}
          className="flex gap-2"
        >
          <input
            type="text"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="Enter name..."
            className="flex-1 rounded-lg border border-gray-600 bg-gray-800 px-3 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none"
            autoFocus
          />
          <button
            type="submit"
            disabled={nameMutation.isPending || !newName.trim()}
            className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {nameMutation.isPending ? "..." : "Save"}
          </button>
          <button
            type="button"
            onClick={() => { setMode("idle"); setNewName(""); }}
            className="rounded-lg border border-gray-600 px-2 py-1.5 text-xs text-gray-400 hover:text-white"
          >
            Cancel
          </button>
        </form>
      )}

      {mode === "assign" && (
        <div className="space-y-2">
          {/* Top 5 nearest suggestions */}
          {nearestQuery.isLoading && (
            <div className="flex gap-2">
              {Array.from({ length: 3 }).map((_, i) => (
                <div key={i} className="h-7 w-20 animate-pulse rounded-lg bg-gray-700" />
              ))}
            </div>
          )}
          {nearestQuery.data && nearestQuery.data.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {nearestQuery.data.map((np) => (
                <button
                  key={np.person_id}
                  type="button"
                  onClick={() => assignMutation.mutate(np.person_id)}
                  disabled={assignMutation.isPending}
                  className="rounded-lg bg-gray-700 px-2.5 py-1 text-xs text-gray-200 hover:bg-indigo-600 hover:text-white disabled:opacity-50 transition-colors"
                >
                  {np.display_name} <span className="text-gray-500">({np.face_count})</span>
                </button>
              ))}
            </div>
          )}
          {/* Search for more */}
          <div className="flex gap-2">
            <div className="relative flex-1">
              <input
                type="text"
                value={assignSearch}
                onChange={(e) => setAssignSearch(e.target.value)}
                placeholder="Search by name..."
                className="w-full rounded-lg border border-gray-600 bg-gray-800 px-3 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none"
                autoFocus
              />
              {assignSearch.trim() && searchResults.length > 0 && (
                <div className="absolute left-0 right-0 top-full z-10 mt-1 max-h-40 overflow-y-auto rounded-lg border border-gray-600 bg-gray-800 shadow-lg">
                  {searchResults.map((p) => (
                    <button
                      key={p.person_id}
                      type="button"
                      onClick={() => assignMutation.mutate(p.person_id)}
                      disabled={assignMutation.isPending}
                      className="block w-full px-3 py-1.5 text-left text-xs text-gray-200 hover:bg-indigo-600 hover:text-white disabled:opacity-50"
                    >
                      {p.display_name} <span className="text-gray-500">({p.face_count})</span>
                    </button>
                  ))}
                </div>
              )}
              {assignSearch.trim() && searching && (
                <div className="absolute right-2 top-1/2 -translate-y-1/2">
                  <div className="h-3 w-3 animate-spin rounded-full border border-gray-600 border-t-white" />
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={() => { setMode("idle"); setAssignSearch(""); setSearchResults([]); }}
              className="rounded-lg border border-gray-600 px-2 py-1.5 text-xs text-gray-400 hover:text-white"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {(nameMutation.isError || assignMutation.isError || dismissMutation.isError) && (
        <p className="mt-2 text-xs text-red-400">
          {(nameMutation.error || assignMutation.error || dismissMutation.error)?.message ?? "Failed"}
        </p>
      )}

      {lightboxIndex !== null && lightboxAssets[lightboxIndex] && (
        <Lightbox
          asset={lightboxAssets[lightboxIndex]}
          assets={lightboxAssets}
          onClose={() => setLightboxIndex(null)}
          onNavigate={(i) => setLightboxIndex(i)}
          hasMore={expanded && allFacesQuery.hasNextPage}
          highlightFaceId={displayFaces[lightboxIndex]?.face_id}
        />
      )}
    </div>
  );
}

export default function PeoplePage() {
  const queryClient = useQueryClient();
  const [clustersExpanded, setClustersExpanded] = useState(true);
  // Track removed cluster indices for optimistic updates — prevents
  // named/dismissed clusters from re-appearing until next manual refresh.
  const [removedIndices, setRemovedIndices] = useState<Set<number>>(new Set());
  // Track clusters currently fading out: "locked" (desaturated) → "fading" (fade out) → removed
  const [fadingIndices, setFadingIndices] = useState<Map<number, "locked" | "fading">>(new Map());
  // Undo state for dismissed clusters — personId is null until API responds
  const [undoState, setUndoState] = useState<{ clusterIndex: number; personId: string | null; timer: ReturnType<typeof setTimeout> } | null>(null);

  const peopleQuery = useInfiniteQuery({
    queryKey: ["people"],
    queryFn: ({ pageParam }) => listPeople(pageParam, 50),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const clustersQuery = useQuery({
    queryKey: ["face-clusters"],
    queryFn: () => getClusters(20, 6),
    // No auto-refetch — only refetch on explicit user action.
    // This prevents clusters from shuffling while the user is naming/dismissing.
    refetchOnWindowFocus: false,
    refetchOnMount: true,
    staleTime: Infinity,
  });

  const people = peopleQuery.data?.pages.flatMap((p) => p.items) ?? [];
  const allClusters = clustersQuery.data?.clusters ?? [];
  const truncated = clustersQuery.data?.truncated ?? false;

  // Filter out fully removed clusters but keep fading ones visible
  const clusters = useMemo(
    () => allClusters.filter((c) => !removedIndices.has(c.cluster_index)),
    [allClusters, removedIndices],
  );

  const startFadeSequence = useCallback((clusterIndex: number) => {
    // Phase 1: lock (desaturated, disabled) for 400ms
    setFadingIndices((prev) => new Map(prev).set(clusterIndex, "locked"));
    setTimeout(() => {
      // Phase 2: fade out over 300ms
      setFadingIndices((prev) => new Map(prev).set(clusterIndex, "fading"));
      setTimeout(() => {
        // Phase 3: remove from DOM
        setFadingIndices((prev) => { const next = new Map(prev); next.delete(clusterIndex); return next; });
        setRemovedIndices((prev) => new Set(prev).add(clusterIndex));
      }, 300);
    }, 400);
  }, []);

  const handleClusterProcessed = useCallback((clusterIndex: number) => {
    startFadeSequence(clusterIndex);
    queryClient.invalidateQueries({ queryKey: ["people"] });
  }, [queryClient, startFadeSequence]);

  const handleDismissStarted = useCallback((clusterIndex: number) => {
    startFadeSequence(clusterIndex);
    if (undoState) clearTimeout(undoState.timer);
    const timer = setTimeout(() => setUndoState(null), 5000);
    setUndoState({ clusterIndex, personId: null, timer });
  }, [startFadeSequence, undoState]);

  const handleDismissComplete = useCallback((clusterIndex: number, personId: string) => {
    // Fill in the personId so undo becomes available
    setUndoState((prev) => prev && prev.clusterIndex === clusterIndex ? { ...prev, personId } : prev);
    queryClient.invalidateQueries({ queryKey: ["people"] });
  }, [queryClient]);

  const handleUndo = useCallback(async () => {
    if (!undoState || !undoState.personId) return;
    clearTimeout(undoState.timer);
    try {
      await deletePerson(undoState.personId);
      // Re-add the cluster to view and mark clusters dirty for next refresh
      setRemovedIndices((prev) => { const next = new Set(prev); next.delete(undoState.clusterIndex); return next; });
      queryClient.invalidateQueries({ queryKey: ["people"] });
    } catch { /* ignore */ }
    setUndoState(null);
  }, [undoState, queryClient]);

  const handleRefreshClusters = useCallback(async () => {
    if (undoState) { clearTimeout(undoState.timer); setUndoState(null); }
    await queryClient.refetchQueries({ queryKey: ["face-clusters"] });
    setRemovedIndices(new Set());
    setFadingIndices(new Map());
  }, [queryClient, undoState]);

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">People</h1>
        <Link to="/people/dismissed" className="text-xs text-gray-400 hover:text-gray-200">
          Dismissed
        </Link>
      </div>

      {people.length === 0 && !peopleQuery.isLoading && clusters.length === 0 && (
        <p className="text-sm text-gray-500">
          No named people yet. Use face clustering below to identify and name people in your photos.
        </p>
      )}

      {people.length > 0 && (
        <div className="mb-8 grid grid-cols-3 gap-4 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
          {people.map((person) => (
            <PersonCard key={person.person_id} person={person} />
          ))}
        </div>
      )}

      <InfiniteScrollSentinel
        hasNextPage={peopleQuery.hasNextPage}
        isFetchingNextPage={peopleQuery.isFetchingNextPage}
        fetchNextPage={peopleQuery.fetchNextPage}
      />

      {/* Unnamed clusters */}
      {(clusters.length > 0 || clustersQuery.isLoading) && (
        <div className="mt-4">
          <div className="mb-4 flex items-center gap-3">
            <button
              type="button"
              onClick={() => setClustersExpanded(!clustersExpanded)}
              className="flex items-center gap-2 text-lg font-semibold text-gray-200 hover:text-white"
            >
              <svg
                className={`h-4 w-4 transition-transform ${clustersExpanded ? "rotate-90" : ""}`}
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
              </svg>
              Unnamed clusters
              <span className="text-sm font-normal text-gray-500">
                ({clusters.reduce((sum, c) => sum + c.size, 0)} faces in {clusters.length} clusters)
              </span>
            </button>
            {removedIndices.size > 0 && (
              <button
                type="button"
                onClick={handleRefreshClusters}
                className="text-xs text-indigo-400 hover:text-indigo-300"
              >
                Refresh clusters
              </button>
            )}
          </div>

          {clustersExpanded && (
            <>
              {truncated && (
                <p className="mb-4 text-xs text-yellow-500">
                  Showing top clusters. Name the largest clusters first to see more.
                </p>
              )}
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4">
                {clusters.map((cluster) => (
                  <ClusterCard
                    key={cluster.cluster_index}
                    cluster={cluster}
                    people={people}
                    fading={fadingIndices.get(cluster.cluster_index) ?? false}
                    onProcessed={handleClusterProcessed}
                    onDismissStarted={handleDismissStarted}
                    onDismissComplete={handleDismissComplete}
                  />
                ))}
              </div>
            </>
          )}
        </div>
      )}

      {clustersQuery.isLoading && (
        <div className="mt-4">
          <div className="h-6 w-48 animate-pulse rounded bg-gray-700" />
        </div>
      )}

      {peopleQuery.isLoading && (
        <div className="grid grid-cols-3 gap-4 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
          {Array.from({ length: 12 }).map((_, i) => (
            <div key={i} className="flex flex-col items-center gap-2 rounded-xl bg-gray-800/50 p-4">
              <div className="h-24 w-24 animate-pulse rounded-full bg-gray-700" />
              <div className="h-4 w-16 animate-pulse rounded bg-gray-700" />
            </div>
          ))}
        </div>
      )}

      {/* Undo dismiss toast */}
      {undoState && (
        <div className="fixed bottom-6 left-1/2 z-50 -translate-x-1/2 animate-fade-in rounded-lg border border-gray-600 bg-gray-800 px-4 py-2.5 shadow-lg">
          <span className="text-sm text-gray-200">Cluster dismissed.</span>
          <button
            type="button"
            onClick={handleUndo}
            disabled={!undoState.personId}
            className="ml-3 text-sm font-medium text-indigo-400 hover:text-indigo-300 disabled:text-gray-500"
          >
            Undo
          </button>
        </div>
      )}
    </div>
  );
}
