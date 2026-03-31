import { useState, useEffect, useRef, useMemo } from "react";
import { Link } from "react-router-dom";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { listDismissedPeople, undismissPerson, getApiKey } from "../api/client";
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
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
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

      {naming ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) restoreMutation.mutate(name.trim());
          }}
          className="flex items-center gap-2"
        >
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Enter name..."
            className="w-40 rounded-lg border border-gray-600 bg-gray-800 px-3 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none"
            autoFocus
          />
          <button
            type="submit"
            disabled={restoreMutation.isPending || !name.trim()}
            className="rounded-lg bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
          >
            {restoreMutation.isPending ? "..." : "Restore"}
          </button>
          <button
            type="button"
            onClick={() => { setNaming(false); setName(""); }}
            className="text-xs text-gray-400 hover:text-white"
          >
            Cancel
          </button>
        </form>
      ) : (
        <button
          type="button"
          onClick={() => setNaming(true)}
          className="rounded-lg border border-gray-600 px-3 py-1.5 text-xs font-medium text-gray-300 hover:bg-gray-700"
        >
          Restore &amp; Name
        </button>
      )}

      {restoreMutation.isError && (
        <p className="text-xs text-red-400">{restoreMutation.error?.message ?? "Failed"}</p>
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
