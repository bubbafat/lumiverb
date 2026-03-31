import { useState, useEffect, useRef, useMemo } from "react";
import { Link } from "react-router-dom";
import { useInfiniteQuery, useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listDismissedPeople, undismissPerson, getNearestPeopleForPerson, searchPeople, getApiKey } from "../api/client";
import type { PersonItem } from "../api/client";
import type { AssetPageItem } from "../api/types";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import { Lightbox } from "../components/Lightbox";

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
        if (!res.ok) return;
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

function DismissedPersonCard({
  person,
  onRestored,
}: {
  person: PersonItem;
  onRestored: () => void;
}) {
  const [mode, setMode] = useState<"idle" | "naming">("idle");
  const [newName, setNewName] = useState("");
  const [assignSearch, setAssignSearch] = useState("");
  const [searchResults, setSearchResults] = useState<PersonItem[]>([]);
  const [searching, setSearching] = useState(false);
  const [showLightbox, setShowLightbox] = useState(false);
  const crop = useFaceCrop(person.representative_face_id ?? "");
  const hasCrop = !!crop.url;
  const fallback = useAuthenticatedImage(
    person.representative_asset_id ?? "",
    "thumbnail",
    { enabled: !hasCrop && !crop.isLoading && !!person.representative_asset_id },
  );
  const imgUrl = crop.url ?? fallback.url;
  const imgLoading = crop.isLoading || (!hasCrop && fallback.isLoading);

  const lightboxAsset: AssetPageItem | null = useMemo(() => {
    if (!person.representative_asset_id) return null;
    return {
      asset_id: person.representative_asset_id,
      rel_path: "",
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
    };
  }, [person.representative_asset_id]);

  const nearestQuery = useQuery({
    queryKey: ["nearest-people-person", person.person_id],
    queryFn: () => getNearestPeopleForPerson(person.person_id, 5),
    enabled: mode === "naming",
    staleTime: Infinity,
  });

  useEffect(() => {
    if (mode !== "naming" || !assignSearch.trim()) {
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

  const restoreMutation = useMutation({
    mutationFn: (displayName: string) => undismissPerson(person.person_id, displayName),
    onSuccess: onRestored,
  });


  return (
    <div className="flex items-center gap-4 rounded-xl border border-gray-700 bg-gray-800/30 p-4">
      <button
        type="button"
        onClick={() => lightboxAsset && setShowLightbox(true)}
        className="h-16 w-16 flex-shrink-0 overflow-hidden rounded-full bg-gray-700 focus:outline-none focus:ring-2 focus:ring-indigo-500"
      >
        {imgLoading ? (
          <div className="h-full w-full animate-pulse bg-gray-600" />
        ) : imgUrl ? (
          <img src={imgUrl} alt="" className="h-full w-full object-cover" />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-lg text-gray-500">?</div>
        )}
      </button>

      {showLightbox && lightboxAsset && (
        <Lightbox
          asset={lightboxAsset}
          assets={[lightboxAsset]}
          onClose={() => setShowLightbox(false)}
          onNavigate={() => {}}
          highlightFaceId={person.representative_face_id ?? undefined}
        />
      )}

      <div className="flex-1 min-w-0">
        <p className="text-sm text-gray-400">
          {person.face_count} {person.face_count === 1 ? "photo" : "photos"}
        </p>
      </div>

      {mode === "naming" ? (
        <div className="flex-1 space-y-2">
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
                  onClick={() => restoreMutation.mutate(np.display_name)}
                  disabled={restoreMutation.isPending}
                  className="rounded-lg bg-gray-700 px-2.5 py-1 text-xs text-gray-200 hover:bg-indigo-600 hover:text-white disabled:opacity-50 transition-colors"
                >
                  {np.display_name} <span className="text-gray-500">({np.face_count})</span>
                </button>
              ))}
            </div>
          )}
          {/* New name or search */}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (newName.trim()) restoreMutation.mutate(newName.trim());
            }}
            className="flex gap-2"
          >
            <div className="relative flex-1">
              <input
                type="text"
                value={newName}
                onChange={(e) => { setNewName(e.target.value); setAssignSearch(e.target.value); }}
                placeholder="New name or search..."
                className="w-full rounded-lg border border-gray-600 bg-gray-800 px-3 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none"
                autoFocus
              />
              {assignSearch.trim() && searchResults.length > 0 && (
                <div className="absolute left-0 right-0 top-full z-10 mt-1 max-h-40 overflow-y-auto rounded-lg border border-gray-600 bg-gray-800 shadow-lg">
                  {searchResults.map((p) => (
                    <button
                      key={p.person_id}
                      type="button"
                      onClick={() => restoreMutation.mutate(p.display_name)}
                      disabled={restoreMutation.isPending}
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
              type="submit"
              disabled={restoreMutation.isPending || !newName.trim()}
              className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
            >
              {restoreMutation.isPending ? "..." : "Restore"}
            </button>
            <button
              type="button"
              onClick={() => { setMode("idle"); setNewName(""); setAssignSearch(""); setSearchResults([]); }}
              className="text-xs text-gray-400 hover:text-white"
            >
              Cancel
            </button>
          </form>
          {restoreMutation.isError && (
            <p className="text-xs text-red-400">{restoreMutation.error?.message ?? "Failed"}</p>
          )}
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setMode("naming")}
          className="rounded-lg border border-gray-600 px-3 py-1.5 text-xs font-medium text-gray-300 hover:bg-gray-700"
        >
          Restore &amp; Name
        </button>
      )}
    </div>
  );
}

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

export default function DismissedPeoplePage() {
  const queryClient = useQueryClient();

  const dismissedQuery = useInfiniteQuery({
    queryKey: ["dismissed-people"],
    queryFn: ({ pageParam }) => listDismissedPeople(pageParam, 50),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const people = dismissedQuery.data?.pages.flatMap((p) => p.items) ?? [];

  const handleRestored = () => {
    queryClient.invalidateQueries({ queryKey: ["dismissed-people"] });
    queryClient.invalidateQueries({ queryKey: ["people"] });
    queryClient.invalidateQueries({ queryKey: ["face-clusters"] });
  };

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <div className="mb-6 flex items-center gap-4">
        <Link
          to="/people"
          className="text-gray-400 hover:text-white"
          aria-label="Back to people"
        >
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
        </Link>
        <h1 className="text-2xl font-bold text-white">Dismissed People</h1>
      </div>

      {people.length === 0 && !dismissedQuery.isLoading && (
        <p className="text-sm text-gray-500">No dismissed people.</p>
      )}

      <div className="space-y-3">
        {people.map((person) => (
          <DismissedPersonCard
            key={person.person_id}
            person={person}
            onRestored={handleRestored}
          />
        ))}
      </div>

      <InfiniteScrollSentinel
        hasNextPage={dismissedQuery.hasNextPage}
        isFetchingNextPage={dismissedQuery.isFetchingNextPage}
        fetchNextPage={dismissedQuery.fetchNextPage}
      />

      {dismissedQuery.isLoading && (
        <div className="space-y-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-20 animate-pulse rounded-xl bg-gray-800/50" />
          ))}
        </div>
      )}
    </div>
  );
}
