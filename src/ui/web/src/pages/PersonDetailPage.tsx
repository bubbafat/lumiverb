import { useState, useCallback, useMemo, useRef, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery, useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getPerson, updatePerson, deletePerson, listPersonFaces, mergePerson, searchPeople, getNearestPeopleForPerson } from "../api/client";
import type { PersonItem } from "../api/client";
import type { AssetPageItem } from "../api/types";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import { FaceCropImage } from "../components/FaceCropImage";
import { Lightbox } from "../components/Lightbox";
import { clipCount } from "../components/ProjectUsageList";
import { clipOfFace, isVideoFace, momentLabel, personClips, type PersonClip } from "../lib/faceMoments";
import { useCanEdit } from "../lib/useCanEdit";
import { THUMBNAIL } from "../lib/names";

/** One of the person's clips: a photo, or a video with the moments they're seen at (each opens it there). */
function ClipThumbnail({ clip, onOpen }: { clip: PersonClip; onOpen: (atMs?: number) => void }) {
  const { url, isLoading } = useAuthenticatedImage(clip.face.asset_id, THUMBNAIL);
  const video = isVideoFace(clip.face);
  const name = clip.face.rel_path ?? "";

  return (
    <div className="space-y-1">
      <button
        type="button"
        onClick={() => onOpen(video ? clip.face.frame_ms ?? clip.moments[0]?.start : undefined)}
        aria-label={video ? `Play ${name}` : `Open ${name}`}
        className="group relative aspect-square w-full overflow-hidden rounded-lg bg-gray-800 focus:outline-hidden focus:ring-2 focus:ring-indigo-500"
      >
        {isLoading ? (
          <div className="h-full w-full animate-pulse bg-gray-700" />
        ) : url ? (
          <img src={url} alt={name} className="h-full w-full object-cover" />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-gray-600">No image</div>
        )}
        {video && (
          <>
            {/* Whose tile it is: a video's still can show others first. */}
            <FaceCropImage faceId={clip.face.face_id} size={32} className="absolute left-1.5 top-1.5 ring-2 ring-black/60" />
            <span className="absolute bottom-1.5 left-1.5 rounded-sm bg-black/70 px-1.5 py-0.5 text-[10px] font-medium text-white">
              ▶ Video
            </span>
          </>
        )}
      </button>
      {video && clip.moments.length > 0 && (
        <div className="flex flex-wrap gap-1" aria-label={`When they're seen in ${name}`}>
          {clip.moments.slice(0, MOMENTS_SHOWN).map((m) => (
            <button
              key={m.start}
              type="button"
              onClick={() => onOpen(m.start)}
              className="rounded-sm bg-gray-800 px-1.5 py-0.5 text-[11px] tabular-nums text-gray-300 hover:bg-indigo-600 hover:text-white"
            >
              {momentLabel(m)}
            </button>
          ))}
          {clip.moments.length > MOMENTS_SHOWN && (
            <span className="px-1 py-0.5 text-[11px] text-gray-500">+{clip.moments.length - MOMENTS_SHOWN} more</span>
          )}
        </div>
      )}
    </div>
  );
}

// A long video can see someone many times: the first few, then how many more (the video holds them all).
const MOMENTS_SHOWN = 4;

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

export default function PersonDetailPage() {
  const { personId } = useParams<{ personId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  // Renaming, merging and deleting people need an editor, as on the server.
  const canEdit = useCanEdit();

  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);
  // Seconds into a video to open it at: the moment picked.
  const [startAt, setStartAt] = useState<number | undefined>(undefined);
  const [mergeMode, setMergeMode] = useState(false);
  const [mergeSearch, setMergeSearch] = useState("");
  const [mergeResults, setMergeResults] = useState<PersonItem[]>([]);
  const [mergeSearching, setMergeSearching] = useState(false);

  const personQuery = useQuery({
    queryKey: ["person", personId],
    queryFn: () => getPerson(personId!),
    enabled: !!personId,
  });

  const facesQuery = useInfiniteQuery({
    queryKey: ["person-faces", personId],
    queryFn: ({ pageParam }) => listPersonFaces(personId!, pageParam, 50),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: !!personId,
  });

  const renameMutation = useMutation({
    mutationFn: (name: string) => updatePerson(personId!, name),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["person", personId] });
      queryClient.invalidateQueries({ queryKey: ["people"] });
      setIsEditing(false);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: () => deletePerson(personId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["people"] });
      navigate("/people");
    },
  });

  const mergeNearestQuery = useQuery({
    queryKey: ["nearest-people-person", personId],
    queryFn: () => getNearestPeopleForPerson(personId!, 5),
    enabled: mergeMode && !!personId,
    staleTime: Infinity,
  });

  const mergeIntoMutation = useMutation({
    mutationFn: (targetPersonId: string) => mergePerson(targetPersonId, personId!),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["people"] });
      navigate("/people");
    },
  });

  // Debounced search for merge target
  useEffect(() => {
    if (!mergeMode || !mergeSearch.trim()) {
      setMergeResults([]);
      return;
    }
    setMergeSearching(true);
    const timer = setTimeout(async () => {
      try {
        const res = await searchPeople(mergeSearch.trim());
        // Exclude self from results
        setMergeResults(res.items.filter((p) => p.person_id !== personId));
      } catch { /* ignore */ }
      setMergeSearching(false);
    }, 250);
    return () => clearTimeout(timer);
  }, [mergeSearch, mergeMode, personId]);

  const startEditing = useCallback(() => {
    if (personQuery.data) {
      setEditName(personQuery.data.display_name);
      setIsEditing(true);
    }
  }, [personQuery.data]);

  const person = personQuery.data;
  const faces = useMemo(() => facesQuery.data?.pages.flatMap((p) => p.items) ?? [], [facesQuery.data]);
  // One tile a clip: a video they're seen in many times is one video.
  const clips = useMemo(() => personClips(faces), [faces]);
  const lightboxAssets: AssetPageItem[] = useMemo(() => clips.map((c) => clipOfFace(c.face)), [clips]);

  if (personQuery.isLoading) {
    return (
      <div className="mx-auto max-w-6xl px-4 py-8">
        <div className="h-8 w-48 animate-pulse rounded-sm bg-gray-700" />
      </div>
    );
  }

  if (!person) {
    return (
      <div className="mx-auto max-w-6xl px-4 py-8">
        <p className="text-gray-500">Person not found.</p>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      {/* Header */}
      <div className="mb-8 flex items-center gap-4">
        <button
          type="button"
          onClick={() => navigate("/people")}
          className="text-gray-400 hover:text-white"
          aria-label="Back to people"
        >
          <svg className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
        </button>

        {isEditing ? (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (editName.trim()) renameMutation.mutate(editName.trim());
            }}
            className="flex items-center gap-2"
          >
            <input
              type="text"
              value={editName}
              onChange={(e) => setEditName(e.target.value)}
              className="rounded-lg border border-gray-600 bg-gray-800 px-3 py-1.5 text-lg font-bold text-white focus:border-indigo-500 focus:outline-hidden focus:ring-1 focus:ring-indigo-500"
              autoFocus
            />
            <button
              type="submit"
              disabled={renameMutation.isPending}
              className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm text-white hover:bg-indigo-500"
            >
              Save
            </button>
            <button
              type="button"
              onClick={() => setIsEditing(false)}
              className="rounded-lg border border-gray-600 px-3 py-1.5 text-sm text-gray-400 hover:text-white"
            >
              Cancel
            </button>
          </form>
        ) : (
          <h1
            className={canEdit ? "cursor-pointer text-2xl font-bold text-white hover:text-indigo-400" : "text-2xl font-bold text-white"}
            onClick={canEdit ? startEditing : undefined}
            title={canEdit ? "Click to rename" : undefined}
          >
            {person.display_name}
          </h1>
        )}

        <span className="text-sm text-gray-500">
          {clipCount(person.face_count)}
        </span>

        {canEdit && (
        <div className="ml-auto flex items-center gap-3">
          {mergeMode ? (
            <div className="flex items-center gap-2 flex-wrap">
              {mergeNearestQuery.data && mergeNearestQuery.data.length > 0 && (
                <>
                  {mergeNearestQuery.data.map((np) => (
                    <button
                      key={np.person_id}
                      type="button"
                      onClick={() => {
                        if (window.confirm(`Merge "${person.display_name}" into "${np.display_name}"? All faces will be moved.`)) {
                          mergeIntoMutation.mutate(np.person_id);
                        }
                      }}
                      disabled={mergeIntoMutation.isPending}
                      className="rounded-lg bg-gray-700 px-2.5 py-1 text-xs text-gray-200 hover:bg-indigo-600 hover:text-white disabled:opacity-50 transition-colors"
                    >
                      {np.display_name} <span className="text-gray-500">({np.face_count})</span>
                    </button>
                  ))}
                </>
              )}
              <div className="relative">
                <input
                  type="text"
                  value={mergeSearch}
                  onChange={(e) => setMergeSearch(e.target.value)}
                  placeholder="Merge into..."
                  className="w-40 rounded-lg border border-gray-600 bg-gray-800 px-3 py-1 text-xs text-white focus:border-indigo-500 focus:outline-hidden"
                  autoFocus
                />
                {mergeSearch.trim() && mergeResults.length > 0 && (
                  <div className="absolute left-0 right-0 top-full z-10 mt-1 max-h-40 overflow-y-auto rounded-lg border border-gray-600 bg-gray-800 shadow-lg">
                    {mergeResults.map((p) => (
                      <button
                        key={p.person_id}
                        type="button"
                        onClick={() => {
                          if (window.confirm(`Merge "${person.display_name}" into "${p.display_name}"? All faces will be moved.`)) {
                            mergeIntoMutation.mutate(p.person_id);
                          }
                        }}
                        disabled={mergeIntoMutation.isPending}
                        className="block w-full px-3 py-1.5 text-left text-xs text-gray-200 hover:bg-indigo-600 hover:text-white disabled:opacity-50"
                      >
                        {p.display_name} ({p.face_count})
                      </button>
                    ))}
                  </div>
                )}
                {mergeSearch.trim() && mergeSearching && (
                  <div className="absolute right-2 top-1/2 -translate-y-1/2">
                    <div className="h-3 w-3 animate-spin rounded-full border border-gray-600 border-t-white" />
                  </div>
                )}
              </div>
              <button
                type="button"
                onClick={() => { setMergeMode(false); setMergeSearch(""); setMergeResults([]); }}
                className="text-xs text-gray-400 hover:text-white"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setMergeMode(true)}
              className="text-xs text-gray-400 hover:text-gray-200"
            >
              Merge into...
            </button>
          )}
          <button
            type="button"
            onClick={() => {
              if (window.confirm(`Delete "${person.display_name}"? This will remove all face assignments.`)) {
                deleteMutation.mutate();
              }
            }}
            className="text-xs text-red-400 hover:text-red-300"
          >
            Delete person
          </button>
        </div>
        )}
      </div>

      {/* Their photos and videos */}
      {clips.length > 0 && (
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
          {clips.map((clip, i) => (
            <ClipThumbnail
              key={clip.face.asset_id}
              clip={clip}
              onOpen={(atMs) => {
                setStartAt(atMs != null ? atMs / 1000 : undefined);
                setLightboxIndex(i);
              }}
            />
          ))}
        </div>
      )}

      {/* Infinite scroll sentinel */}
      <InfiniteScrollSentinel
        hasNextPage={facesQuery.hasNextPage}
        isFetchingNextPage={facesQuery.isFetchingNextPage}
        fetchNextPage={facesQuery.fetchNextPage}
      />

      {faces.length === 0 && !facesQuery.isLoading && (
        <p className="text-sm text-gray-500">No photos or videos assigned to this person yet.</p>
      )}

      {lightboxIndex !== null && lightboxAssets[lightboxIndex] && (
        <Lightbox
          asset={lightboxAssets[lightboxIndex]}
          assets={lightboxAssets}
          hasMore={facesQuery.hasNextPage}
          onClose={() => setLightboxIndex(null)}
          onNavigate={(i) => {
            // Another clip: a video opens where they're first shown in it.
            const face = clips[i]?.face;
            setStartAt(face && isVideoFace(face) && face.frame_ms != null ? face.frame_ms / 1000 : undefined);
            setLightboxIndex(i);
          }}
          startAt={startAt}
        />
      )}
    </div>
  );
}
