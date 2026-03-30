import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listPeople,
  getClusters,
  nameCluster,
  getApiKey,
} from "../api/client";
import type { PersonItem, ClusterItem, PersonFaceItem } from "../api/client";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";

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

function ClusterFaceThumbnail({ face }: { face: PersonFaceItem }) {
  // Prefer server-generated face crop; fall back to full asset thumbnail
  const crop = useFaceCrop(face.face_id);
  const fallback = useAuthenticatedImage(face.asset_id, "thumbnail", { enabled: !crop.url && !crop.isLoading });

  const url = crop.url ?? fallback.url;
  const isLoading = crop.isLoading || (!crop.url && fallback.isLoading);

  return (
    <div className="relative aspect-square overflow-hidden rounded-lg bg-gray-800">
      {isLoading ? (
        <div className="h-full w-full animate-pulse bg-gray-700" />
      ) : url ? (
        <img src={url} alt={face.rel_path ?? ""} className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-gray-600 text-xs">
          No image
        </div>
      )}
    </div>
  );
}

function ClusterCard({
  cluster,
  people,
  onNamed,
}: {
  cluster: ClusterItem;
  people: PersonItem[];
  onNamed: () => void;
}) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"idle" | "name" | "assign">("idle");
  const [newName, setNewName] = useState("");
  const [selectedPersonId, setSelectedPersonId] = useState("");

  const nameMutation = useMutation({
    mutationFn: (name: string) =>
      nameCluster(cluster.cluster_index, { displayName: name }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["people"] });
      queryClient.invalidateQueries({ queryKey: ["face-clusters"] });
      setMode("idle");
      setNewName("");
      onNamed();
    },
  });

  const assignMutation = useMutation({
    mutationFn: (personId: string) =>
      nameCluster(cluster.cluster_index, { personId, displayName: "" }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["people"] });
      queryClient.invalidateQueries({ queryKey: ["face-clusters"] });
      setMode("idle");
      setSelectedPersonId("");
      onNamed();
    },
  });

  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800/30 p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-medium text-gray-300">
          {cluster.size} {cluster.size === 1 ? "photo" : "photos"}
        </span>
      </div>

      {/* Face thumbnails */}
      <div className="mb-3 grid grid-cols-3 gap-1.5">
        {cluster.faces.map((face) => (
          <ClusterFaceThumbnail key={face.face_id} face={face} />
        ))}
      </div>

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
        <div className="flex gap-2">
          <select
            value={selectedPersonId}
            onChange={(e) => setSelectedPersonId(e.target.value)}
            className="flex-1 rounded-lg border border-gray-600 bg-gray-800 px-3 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none"
            autoFocus
          >
            <option value="">Select person...</option>
            {people.map((p) => (
              <option key={p.person_id} value={p.person_id}>
                {p.display_name} ({p.face_count})
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => {
              if (selectedPersonId) assignMutation.mutate(selectedPersonId);
            }}
            disabled={assignMutation.isPending || !selectedPersonId}
            className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {assignMutation.isPending ? "..." : "Assign"}
          </button>
          <button
            type="button"
            onClick={() => { setMode("idle"); setSelectedPersonId(""); }}
            className="rounded-lg border border-gray-600 px-2 py-1.5 text-xs text-gray-400 hover:text-white"
          >
            Cancel
          </button>
        </div>
      )}

      {(nameMutation.isError || assignMutation.isError) && (
        <p className="mt-2 text-xs text-red-400">
          {(nameMutation.error || assignMutation.error)?.message ?? "Failed"}
        </p>
      )}
    </div>
  );
}

export default function PeoplePage() {
  const queryClient = useQueryClient();
  const [clustersExpanded, setClustersExpanded] = useState(true);

  const peopleQuery = useInfiniteQuery({
    queryKey: ["people"],
    queryFn: ({ pageParam }) => listPeople(pageParam, 50),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const clustersQuery = useQuery({
    queryKey: ["face-clusters"],
    queryFn: () => getClusters(20, 6),
  });

  const people = peopleQuery.data?.pages.flatMap((p) => p.items) ?? [];
  const clusters = clustersQuery.data?.clusters ?? [];
  const truncated = clustersQuery.data?.truncated ?? false;

  const handleClusterNamed = () => {
    queryClient.invalidateQueries({ queryKey: ["face-clusters"] });
    queryClient.invalidateQueries({ queryKey: ["people"] });
  };

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <h1 className="mb-6 text-2xl font-bold text-white">People</h1>

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

      {peopleQuery.hasNextPage && (
        <button
          type="button"
          onClick={() => peopleQuery.fetchNextPage()}
          disabled={peopleQuery.isFetchingNextPage}
          className="mb-8 rounded-lg border border-gray-700 bg-gray-800/50 px-4 py-2 text-sm text-gray-300 hover:bg-gray-700"
        >
          {peopleQuery.isFetchingNextPage ? "Loading..." : "Load more"}
        </button>
      )}

      {/* Unnamed clusters */}
      {clusters.length > 0 && (
        <div className="mt-4">
          <button
            type="button"
            onClick={() => setClustersExpanded(!clustersExpanded)}
            className="mb-4 flex items-center gap-2 text-lg font-semibold text-gray-200 hover:text-white"
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
                    onNamed={handleClusterNamed}
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
    </div>
  );
}
