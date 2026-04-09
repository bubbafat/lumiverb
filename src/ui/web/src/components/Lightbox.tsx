import { useEffect, useCallback, useState, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getAsset, findSimilar, listFaces, listPeople, getNearestPeopleForFace, searchPeople, assignFace, unassignFace, uploadTranscript, deleteTranscript, updateNote, deleteNote } from "../api/client";
import TranscriptViewer from "./TranscriptViewer";
import { useLocalStorage } from "../lib/useLocalStorage";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import type { AssetPageItem, AssetRating, RatingColor, SimilarHit } from "../api/types";
import { HeartButton, StarPicker, ColorPicker } from "./RatingControls";
import { basename, formatFileSize, formatDate, formatExposure } from "../lib/format";

interface LightboxProps {
  asset: AssetPageItem;
  assets: AssetPageItem[];
  hasMore?: boolean;
  onClose: () => void;
  onNavigate: (index: number) => void;
  onTagClick?: (tag: string) => void;
  onSimilarClick?: (asset: AssetPageItem) => void;
  onDateClick?: (dateStr: string) => void;
  onNearbyClick?: (lat: number, lon: number) => void;
  onFilterClick?: (params: Record<string, string>) => void;
  onPathClick?: (path: string) => void;
  onAddToCollection?: (assetId: string) => void;
  rating?: AssetRating;
  onRatingChange?: (assetId: string, update: { favorite?: boolean; stars?: number; color?: RatingColor | null }) => void;
  libraryId?: string;
  isPublic?: boolean;
  publicLibraryId?: string;
  /** Highlight a specific face with a red border (e.g. the clustered face). */
  highlightFaceId?: string;
}

function SimilarThumbnail({
  hit,
  onClick,
  isPublic,
  publicLibraryId,
}: {
  hit: SimilarHit;
  onClick: () => void;
  isPublic?: boolean;
  publicLibraryId?: string;
}) {
  const { url, isLoading } = useAuthenticatedImage(hit.asset_id, "thumbnail", {
    isPublic,
    publicLibraryId,
  });
  const filename = basename(hit.rel_path);
  return (
    <button
      type="button"
      onClick={onClick}
      className="group relative aspect-square w-full overflow-hidden rounded-lg bg-gray-800 focus:outline-none focus:ring-2 focus:ring-indigo-500"
    >
      {isLoading && (
        <div className="absolute inset-0 animate-pulse bg-gray-700" />
      )}
      {url && (
        <img
          src={url}
          alt={filename}
          className="h-full w-full object-cover transition-opacity group-hover:opacity-80"
        />
      )}
    </button>
  );
}

function NoteSection({
  asset,
  note,
  noteAuthor,
  noteUpdatedAt,
  loading,
  queryClient,
}: {
  asset: AssetPageItem;
  note: string | null;
  noteAuthor: string | null;
  noteUpdatedAt: string | null;
  loading: boolean;
  queryClient: ReturnType<typeof useQueryClient>;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");

  const startEdit = () => {
    setDraft(note || "");
    setEditing(true);
  };

  const save = async () => {
    await updateNote(asset.asset_id, draft);
    queryClient.invalidateQueries({ queryKey: ["asset", asset.asset_id] });
    setEditing(false);
  };

  const cancel = () => {
    setEditing(false);
  };

  return (
    <>
      <hr className="border-gray-700" />
      <div>
        <div className="mb-1 flex items-center justify-between">
          <span className="text-xs font-medium uppercase tracking-wide text-gray-500">
            Notes
            {noteAuthor && noteUpdatedAt && (
              <span className="ml-1 normal-case font-normal">
                · {noteAuthor} · {formatDate(noteUpdatedAt)}
              </span>
            )}
          </span>
          {!editing && note && (
            <div className="flex gap-2">
              <button
                type="button"
                className="text-xs text-indigo-400 hover:text-indigo-300"
                onClick={startEdit}
              >
                Edit
              </button>
              <button
                type="button"
                className="text-xs text-red-400 hover:text-red-300"
                onClick={async () => {
                  await deleteNote(asset.asset_id);
                  queryClient.invalidateQueries({ queryKey: ["asset", asset.asset_id] });
                }}
              >
                Delete
              </button>
            </div>
          )}
        </div>
        {loading ? (
          <MetadataSkeleton />
        ) : editing ? (
          <div className="space-y-2">
            <textarea
              className="w-full rounded border border-gray-600 bg-gray-900 p-2 text-sm text-gray-200 placeholder-gray-500 focus:border-indigo-500 focus:outline-none resize-y"
              rows={3}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="Add a note..."
              autoFocus
            />
            <div className="flex gap-2">
              <button
                type="button"
                className="rounded bg-indigo-600 px-3 py-1 text-xs text-white hover:bg-indigo-500"
                onClick={save}
              >
                Save
              </button>
              <button
                type="button"
                className="rounded bg-gray-700 px-3 py-1 text-xs text-gray-300 hover:bg-gray-600"
                onClick={cancel}
              >
                Cancel
              </button>
            </div>
          </div>
        ) : note ? (
          <p
            className="text-sm text-gray-300 whitespace-pre-wrap cursor-pointer hover:text-gray-200"
            onClick={startEdit}
          >
            {note}
          </p>
        ) : (
          <button
            type="button"
            className="rounded bg-gray-700/60 px-3 py-1.5 text-xs text-gray-300 hover:bg-indigo-600/40 hover:text-indigo-200 transition-colors"
            onClick={startEdit}
          >
            Add note
          </button>
        )}
      </div>
    </>
  );
}

function MetadataSkeleton() {
  return (
    <div className="space-y-3">
      <div className="h-4 w-3/4 animate-pulse rounded bg-gray-700" />
      <div className="h-3 w-full animate-pulse rounded bg-gray-700/80" />
      <div className="h-3 w-1/2 animate-pulse rounded bg-gray-700/80" />
    </div>
  );
}

function FilterLink({
  children,
  params,
  onFilterClick,
  onClose,
  title,
}: {
  children: React.ReactNode;
  params: Record<string, string>;
  onFilterClick?: (params: Record<string, string>) => void;
  onClose: () => void;
  title?: string;
}) {
  if (!onFilterClick) {
    return <span className="text-gray-300">{children}</span>;
  }
  return (
    <button
      type="button"
      onClick={() => {
        onFilterClick(params);
        onClose();
      }}
      title={title ?? "Filter by this value"}
      className="text-gray-300 hover:text-indigo-400 hover:underline transition-colors text-left"
    >
      {children}
    </button>
  );
}

export function Lightbox({
  asset,
  assets,
  hasMore = false,
  onClose,
  onNavigate,
  onTagClick,
  onSimilarClick,
  onDateClick,
  onNearbyClick,
  onFilterClick,
  onPathClick,
  onAddToCollection,
  rating,
  onRatingChange,
  libraryId,
  isPublic,
  publicLibraryId,
  highlightFaceId,
}: LightboxProps) {
  const navigate = useNavigate();
  const [showSimilar, setShowSimilar] = useState(false);
  const [showFaces, setShowFaces] = useLocalStorage("lv_show_faces", false);
  const [metaOpen, setMetaOpen] = useState(true);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [showHints, setShowHints] = useState(true);
  const [slideshowActive, setSlideshowActive] = useState(false);
  const lightboxRef = useRef<HTMLDivElement>(null);
  const hintTimerRef = useRef<number>();
  const slideshowRef = useRef<number>();

  const currentIndex = assets.findIndex((a) => a.asset_id === asset.asset_id);
  const hasPrev = currentIndex > 0;
  const hasNext =
    (currentIndex >= 0 && currentIndex < assets.length - 1) ||
    (currentIndex === assets.length - 1 && hasMore);

  // Reset state when asset changes
  useEffect(() => {
    setShowSimilar(false);
    setMetaOpen(true);
  }, [asset.asset_id]);

  // Keyboard hint: show for 3s on open, reset on key use
  const resetHintTimer = useCallback(() => {
    setShowHints(true);
    clearTimeout(hintTimerRef.current);
    hintTimerRef.current = window.setTimeout(() => setShowHints(false), 3000);
  }, []);

  useEffect(() => {
    resetHintTimer();
    return () => clearTimeout(hintTimerRef.current);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Slideshow
  useEffect(() => {
    clearInterval(slideshowRef.current);
    if (!slideshowActive) return;
    slideshowRef.current = window.setInterval(() => {
      onNavigate(Math.min(currentIndex + 1, assets.length - 1));
    }, 3000);
    return () => clearInterval(slideshowRef.current);
  }, [slideshowActive, currentIndex, assets.length, onNavigate]);

  // Stop slideshow at end
  useEffect(() => {
    if (slideshowActive && currentIndex === assets.length - 1 && !hasMore) {
      setSlideshowActive(false);
    }
  }, [slideshowActive, currentIndex, assets.length, hasMore]);

  // Fullscreen sync
  useEffect(() => {
    const onFsChange = () =>
      setIsFullscreen(Boolean(document.fullscreenElement));
    document.addEventListener("fullscreenchange", onFsChange);
    return () => document.removeEventListener("fullscreenchange", onFsChange);
  }, []);

  const toggleFullscreen = useCallback(() => {
    if (!isFullscreen) {
      lightboxRef.current?.requestFullscreen().catch(() => {
        setIsFullscreen(true);
      });
    } else {
      if (document.fullscreenElement) {
        document.exitFullscreen();
      } else {
        setIsFullscreen(false);
      }
    }
  }, [isFullscreen]);

  const isVideo =
    asset.media_type === "video" || asset.media_type.startsWith("video/");

  const {
    url: mediaUrl,
    isLoading: mediaLoading,
    generating,
  } = useAuthenticatedImage(asset.asset_id, isVideo ? "video-preview" : "proxy", {
    isPublic,
    publicLibraryId,
  });
  const { data: detail, isLoading: detailLoading } = useQuery({
    queryKey: ["asset", asset.asset_id, publicLibraryId ?? null],
    queryFn: () => getAsset(asset.asset_id, isPublic ? publicLibraryId : undefined),
    enabled: !isPublic || !!publicLibraryId,
    refetchInterval: 10_000,
  });

  const { data: similarData, isLoading: similarLoading } = useQuery({
    queryKey: ["similar", asset.asset_id, libraryId],
    queryFn: () =>
      findSimilar({
        assetId: asset.asset_id,
        libraryId: libraryId ?? "",
        limit: 20,
      }),
    enabled: showSimilar && !!libraryId,
  });

  const isImage = !isVideo;
  const hasFaces = isImage && (asset.face_count ?? 0) > 0;

  const queryClient = useQueryClient();

  const { data: facesData } = useQuery({
    queryKey: ["faces", asset.asset_id],
    queryFn: () => listFaces(asset.asset_id),
    enabled: showFaces && hasFaces,
  });

  // Face assignment popover state
  const [assignFaceId, setAssignFaceId] = useState<string | null>(null);
  const [assignMode, setAssignMode] = useState<"pick" | "name">("pick");
  const [newPersonName, setNewPersonName] = useState("");
  // Typeahead search over the full people list for when the right
  // person isn't in the nearest-by-embedding suggestions (happens on
  // partial profiles, heavy occlusion, or when a person has too few
  // confirmed faces for their centroid to be meaningful).
  const [assignSearch, setAssignSearch] = useState("");
  const [searchResults, setSearchResults] = useState<
    { person_id: string; display_name: string; face_count: number }[]
  >([]);
  const [searching, setSearching] = useState(false);

  // Reset popover when asset changes
  useEffect(() => {
    setAssignFaceId(null);
    setAssignSearch("");
    setSearchResults([]);
  }, [asset.asset_id]);

  // Clear search state when popover closes or switches face
  useEffect(() => {
    setAssignSearch("");
    setSearchResults([]);
  }, [assignFaceId]);

  // Debounced typeahead — mirrors PeoplePage cluster-card pattern.
  useEffect(() => {
    if (assignFaceId == null || !assignSearch.trim()) {
      setSearchResults([]);
      setSearching(false);
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
  }, [assignSearch, assignFaceId]);

  // When the lightbox is opened from cluster review (or anywhere else
  // that passes a highlightFaceId), force the face overlay on. The
  // whole point of arriving with a highlighted face is to interact
  // with that one face — without overlays you're staring at a photo
  // with no clickable hit target, and the only escape is the cluster
  // card's "name everything" button. Only kicks once per session
  // since lv_show_faces is in localStorage.
  useEffect(() => {
    if (highlightFaceId && !showFaces && hasFaces) {
      setShowFaces(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [highlightFaceId, hasFaces]);

  // Once faces have loaded for the current asset, auto-open the
  // assign popover on the highlighted face. This collapses the
  // cluster-review-to-tagged flow from "click face crop → wait →
  // click red border → wait" down to "click face crop → wait → pick
  // a name". Skips when the highlighted face is already named (no
  // need to nag the user about a face that's already done).
  const autoOpenedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!highlightFaceId || !facesData?.faces) return;
    const target = facesData.faces.find((f) => f.face_id === highlightFaceId);
    if (!target) return;
    const isNamed = target.person != null && target.person.dismissed === false;
    if (isNamed) return;
    // Only auto-open once per (asset, highlightFaceId) pair so the
    // popover doesn't reopen on every render or after the user
    // explicitly closes it.
    const key = `${asset.asset_id}|${highlightFaceId}`;
    if (autoOpenedRef.current === key) return;
    autoOpenedRef.current = key;
    setAssignFaceId(highlightFaceId);
    setAssignMode("pick");
  }, [asset.asset_id, highlightFaceId, facesData]);

  // Per-face nearest-people: ranks named people by cosine distance
  // from the clicked face's embedding to each person's centroid.
  // This is the *signal* the user actually wants in the popover —
  // "who looks like this face?" — and is computed server-side via
  // GET /v1/faces/{face_id}/nearest-people. Limit 8 keeps the
  // popover compact while still surfacing the relevant candidates.
  const { data: nearestForFaceData } = useQuery({
    queryKey: ["nearest-people-for-face", assignFaceId],
    queryFn: () => getNearestPeopleForFace(assignFaceId!, 8),
    enabled: assignFaceId != null,
    staleTime: 60_000,
  });

  // Fallback alphabetical-by-face-count list, used when:
  // - The face has no embedding (nearest endpoint returns empty)
  // - The user is in "Change to:" mode on an already-named face
  // - As a safety net while the nearest query is in flight
  const { data: peopleData } = useQuery({
    queryKey: ["people-for-assign"],
    queryFn: () => listPeople(undefined, 100),
    enabled: assignFaceId != null,
  });

  /// Composite candidate list for the popover. Prefer the nearest-by-
  /// embedding ranking; fall back to the full list if that's empty.
  /// Returns the same shape (PersonItem[]) as listPeople so the JSX
  /// rendering doesn't need to know which source it came from.
  const candidatePeople = (() => {
    const nearest = nearestForFaceData ?? [];
    if (nearest.length > 0) {
      // NearestPersonItem doesn't have representativeFaceId — synthesize
      // a minimal PersonItem-shaped object with the fields the popover
      // actually reads.
      return nearest.map((np) => ({
        person_id: np.person_id,
        display_name: np.display_name,
        face_count: np.face_count,
        representative_face_id: null,
        representative_asset_id: null,
        confirmation_count: 0,
      }));
    }
    return peopleData?.items ?? [];
  })();

  const assignMutation = useMutation({
    mutationFn: (opts: { faceId: string } & ({ personId: string } | { newPersonName: string })) => {
      const { faceId, ...rest } = opts;
      return assignFace(faceId, "personId" in rest ? { personId: rest.personId } : { newPersonName: rest.newPersonName });
    },
    onSuccess: (_data, variables) => {
      const taggedHighlight = variables.faceId === highlightFaceId;
      setAssignFaceId(null);
      setNewPersonName("");
      setAssignMode("pick");
      setAssignSearch("");
      setSearchResults([]);
      queryClient.invalidateQueries({ queryKey: ["faces", asset.asset_id] });
      queryClient.invalidateQueries({ queryKey: ["people"] });
      queryClient.invalidateQueries({ queryKey: ["people-for-assign"] });
      // The nearest-people ranking is centroid-based, so an assign
      // shifts the named person's centroid and invalidates every
      // existing per-face ranking. Wipe the whole prefix.
      queryClient.invalidateQueries({ queryKey: ["nearest-people-for-face"] });
      queryClient.invalidateQueries({ queryKey: ["face-clusters"] });
      // Cluster-review face pages are keyed by cluster_index; invalidate
      // the whole prefix so the caller's paginated list reshapes.
      queryClient.invalidateQueries({ queryKey: ["cluster-faces"] });

      // If the user just tagged the *highlighted* face (the one they
      // arrived at from cluster review), advance to the next asset in
      // the lightbox so the cluster visibly "moves along". Brief delay
      // lets them see the red→green transition first. If there's no
      // next asset, close the lightbox entirely.
      if (taggedHighlight) {
        window.setTimeout(() => {
          if (hasNext) {
            onNavigate(currentIndex + 1);
          } else {
            onClose();
          }
        }, 350);
      }
    },
  });

  const unassignMutation = useMutation({
    mutationFn: (faceId: string) => unassignFace(faceId),
    onSuccess: () => {
      setAssignFaceId(null);
      queryClient.invalidateQueries({ queryKey: ["faces", asset.asset_id] });
      queryClient.invalidateQueries({ queryKey: ["people"] });
      queryClient.invalidateQueries({ queryKey: ["people-for-assign"] });
      queryClient.invalidateQueries({ queryKey: ["face-clusters"] });
    },
  });

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      const el = document.activeElement as HTMLElement | null;
      if (
        el?.tagName === "INPUT" ||
        el?.tagName === "TEXTAREA" ||
        el?.isContentEditable
      )
        return;

      switch (e.key) {
        case "Escape":
          if (isFullscreen) {
            toggleFullscreen();
          } else {
            onClose();
          }
          break;
        case "ArrowLeft":
          if (hasPrev) {
            setSlideshowActive(false);
            onNavigate(currentIndex - 1);
            resetHintTimer();
          }
          break;
        case "ArrowRight":
          if (hasNext) {
            setSlideshowActive(false);
            onNavigate(currentIndex + 1);
            resetHintTimer();
          }
          break;
        case "d":
          if (hasFaces) setShowFaces(!showFaces);
          break;
        case "f":
          // f = toggle favorite (Lightroom convention); Shift+F = fullscreen
          if (onRatingChange) {
            onRatingChange(asset.asset_id, { favorite: !(rating?.favorite ?? false) });
          }
          break;
        case "F":
          toggleFullscreen();
          break;
        case "1": case "2": case "3": case "4": case "5":
          if (onRatingChange) {
            const n = Number(e.key);
            onRatingChange(asset.asset_id, { stars: n === (rating?.stars ?? 0) ? 0 : n });
          }
          break;
        case "0":
          if (onRatingChange) {
            onRatingChange(asset.asset_id, { stars: 0 });
          }
          break;
        case "6": case "7": case "8": case "9": {
          if (onRatingChange) {
            const colorMap: Record<string, string> = { "6": "red", "7": "orange", "8": "yellow", "9": "green" };
            const c = colorMap[e.key] as import("../api/types").RatingColor;
            onRatingChange(asset.asset_id, { color: c === (rating?.color ?? null) ? null : c });
          }
          break;
        }
        case "`":
          if (onRatingChange) {
            onRatingChange(asset.asset_id, { color: null });
          }
          break;
        case " ":
          e.preventDefault();
          setSlideshowActive((a) => !a);
          resetHintTimer();
          break;
        case "?":
          setShowHints((h) => {
            clearTimeout(hintTimerRef.current);
            return !h;
          });
          break;
        default:
          resetHintTimer();
      }
    },
    [
      onClose,
      onNavigate,
      hasPrev,
      hasNext,
      currentIndex,
      isFullscreen,
      toggleFullscreen,
      resetHintTimer,
      onRatingChange,
      asset.asset_id,
      rating,
      hasFaces,
      showFaces,
    ],
  );

  useEffect(() => {
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [handleKeyDown]);

  const filename = basename(asset.rel_path);
  const formatGps = (lat: number, lon: number): string =>
    `${lat.toFixed(5)}, ${lon.toFixed(5)}`;

  return (
    <div
      ref={lightboxRef}
      data-lightbox="true"
      className={`fixed inset-0 z-50 flex bg-black/90 ${isFullscreen ? "bg-black" : ""}`}
    >
      {/* Slideshow progress bar */}
      {slideshowActive && (
        <div className="absolute top-0 inset-x-0 h-0.5 bg-gray-800 z-10">
          <div
            key={`${asset.asset_id}-slideshow`}
            className="h-full bg-indigo-500 motion-reduce:hidden"
            style={{
              animation: "lv-progress 3s linear forwards",
            }}
          />
        </div>
      )}

      {/* Header buttons */}
      <div className="absolute right-4 top-4 z-10 mt-safe mr-safe flex items-center gap-2">
        {/* Fullscreen toggle */}
        <button
          type="button"
          onClick={toggleFullscreen}
          className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full p-2 text-white/80 transition-colors hover:bg-white/10 hover:text-white"
          aria-label={isFullscreen ? "Exit fullscreen" : "Enter fullscreen"}
        >
          {isFullscreen ? (
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3" />
            </svg>
          ) : (
            <svg className="h-5 w-5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden>
              <path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3" />
            </svg>
          )}
        </button>
        {/* Close */}
        <button
          type="button"
          onClick={onClose}
          className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full p-2 text-2xl text-white/80 transition-colors hover:bg-white/10 hover:text-white"
          aria-label="Close"
        >
          ×
        </button>
      </div>

      {/* Two-column layout */}
      <div className="flex w-full flex-col lg:flex-row">
        {/* Left: image */}
        <div className="relative flex flex-1 items-center justify-center p-4 lg:p-8 min-h-[50vh] lg:min-h-0">
          <div className="relative flex max-h-full min-h-0 flex-1 items-center justify-center">
            {mediaLoading ? (
              <div className="flex h-64 w-64 items-center justify-center">
                <div
                  className="h-12 w-12 animate-spin rounded-full border-2 border-white/30 border-t-white"
                  aria-hidden
                />
              </div>
            ) : generating ? (
              <div className="flex flex-col items-center gap-3 text-gray-400">
                <svg
                  className="h-12 w-12"
                  fill="none"
                  viewBox="0 0 24 24"
                  stroke="currentColor"
                  aria-hidden
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={1.5}
                    d="M15 10l4.553-2.277A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M3 8a2 2 0 012-2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V8z"
                  />
                </svg>
                <span className="text-sm">Preview generating…</span>
              </div>
            ) : mediaUrl && isVideo ? (
              <video
                key={asset.asset_id}
                src={mediaUrl}
                controls
                playsInline
                className="max-h-[calc(100vh-4rem)] max-w-full"
              />
            ) : mediaUrl ? (
              <div className="relative inline-block">
                <img
                  src={mediaUrl}
                  alt={filename}
                  className="max-h-[calc(100vh-4rem)] max-w-full object-contain"
                />
                {showFaces && facesData?.faces.map((face) => {
                  if (!face.bounding_box) return null;
                  const isHighlighted = highlightFaceId === face.face_id;
                  const isDismissed = face.person?.dismissed === true;
                  const isNamed = face.person != null && !isDismissed;
                  // Named beats highlighted: once the cluster-review
                  // face has been tagged, it should flash green even
                  // before the lightbox auto-advances — otherwise the
                  // success state is invisible.
                  const borderColor = isNamed ? "border-emerald-400" : isHighlighted ? "border-red-500" : isDismissed ? "border-gray-500" : "border-white";
                  const isPopoverTarget = assignFaceId === face.face_id;
                  // Auto-flip the popover anchor based on which side
                  // of the face has the most room. The previous
                  // heuristic (anchor above when faceBottom > 0.55)
                  // was wrong for tall faces near the top of the
                  // image — a face spanning y=0.05..0.75 would anchor
                  // above and clip off the *top* of the screen because
                  // there was no room above. Compare room-above to
                  // room-below directly and pick the larger; do the
                  // same horizontally. The popover also gets
                  // max-h-[80vh] overflow-y-auto so when neither side
                  // has enough room (very large face) the contents
                  // scroll inside the viewport instead of clipping.
                  const roomAbove = face.bounding_box.y;
                  const roomBelow = 1 - (face.bounding_box.y + face.bounding_box.h);
                  const roomLeft = face.bounding_box.x;
                  const roomRight = 1 - (face.bounding_box.x + face.bounding_box.w);
                  const anchorAbove = roomAbove > roomBelow;
                  const anchorRight = roomLeft > roomRight;
                  const popoverAnchorClass = [
                    anchorRight ? "right-0" : "left-0",
                    anchorAbove ? "bottom-full mb-1" : "top-full mt-1",
                  ].join(" ");
                  return (
                    <div
                      key={face.face_id}
                      className={`absolute border-2 ${borderColor} rounded cursor-pointer`}
                      style={{
                        left: `${face.bounding_box.x * 100}%`,
                        top: `${face.bounding_box.y * 100}%`,
                        width: `${face.bounding_box.w * 100}%`,
                        height: `${face.bounding_box.h * 100}%`,
                      }}
                      onClick={(e) => {
                        e.stopPropagation();
                        setAssignFaceId(isPopoverTarget ? null : face.face_id);
                        setAssignMode("pick");
                        setNewPersonName("");
                      }}
                    >
                      {isNamed && (
                        <span className="absolute -bottom-5 left-0 whitespace-nowrap rounded bg-black/70 px-1 text-xs text-white">
                          {face.person!.display_name}
                        </span>
                      )}
                      {/* Face popover — assign (unidentified) or manage (identified) */}
                      {isPopoverTarget && (
                        <div
                          className={`absolute z-50 w-64 max-h-[80vh] overflow-y-auto rounded-lg border border-gray-600 bg-gray-800 p-3 shadow-xl ${popoverAnchorClass}`}
                          onClick={(e) => e.stopPropagation()}
                        >
                          {isNamed && assignMode === "pick" ? (
                            <div className="space-y-2">
                              <p className="text-xs font-bold text-white">{face.person!.display_name}</p>
                              <button
                                type="button"
                                onClick={() => { setAssignFaceId(null); navigate(`/people/${face.person!.person_id}`); }}
                                className="w-full rounded px-2 py-1 text-left text-xs text-gray-200 hover:bg-gray-700"
                              >
                                View person
                              </button>
                              <button
                                type="button"
                                onClick={() => unassignMutation.mutate(face.face_id)}
                                disabled={unassignMutation.isPending}
                                className="w-full rounded px-2 py-1 text-left text-xs text-red-400 hover:bg-gray-700"
                              >
                                {unassignMutation.isPending ? "Removing..." : "Remove tag"}
                              </button>
                              <hr className="border-gray-700" />
                              <p className="text-xs text-gray-500">Change to:</p>
                              {candidatePeople.filter((p) => p.person_id !== face.person!.person_id).length > 0 && (
                                <div className="max-h-24 space-y-1 overflow-y-auto">
                                  {candidatePeople
                                    .filter((p) => p.person_id !== face.person!.person_id)
                                    .map((p) => (
                                      <button
                                        key={p.person_id}
                                        type="button"
                                        onClick={async () => {
                                          await unassignFace(face.face_id);
                                          assignMutation.mutate({ faceId: face.face_id, personId: p.person_id });
                                        }}
                                        disabled={assignMutation.isPending}
                                        className="w-full rounded px-2 py-1 text-left text-xs text-gray-200 hover:bg-gray-700"
                                      >
                                        {p.display_name} <span className="text-gray-500">({p.face_count})</span>
                                      </button>
                                    ))}
                                </div>
                              )}
                              {/* Search the full people list — nearest-by-
                                  embedding only surfaces the top 8, so we
                                  need a way to reach everyone else. */}
                              <div className="relative">
                                <input
                                  type="text"
                                  value={assignSearch}
                                  onChange={(e) => setAssignSearch(e.target.value)}
                                  placeholder="Search by name..."
                                  className="w-full rounded border border-gray-600 bg-gray-900 px-2 py-1 text-xs text-white focus:border-indigo-500 focus:outline-none"
                                />
                                {assignSearch.trim() && searchResults.length > 0 && (
                                  <div className="absolute left-0 right-0 top-full z-10 mt-1 max-h-40 overflow-y-auto rounded border border-gray-600 bg-gray-900 shadow-lg">
                                    {searchResults
                                      .filter((p) => p.person_id !== face.person!.person_id)
                                      .map((p) => (
                                        <button
                                          key={p.person_id}
                                          type="button"
                                          onClick={async () => {
                                            await unassignFace(face.face_id);
                                            assignMutation.mutate({ faceId: face.face_id, personId: p.person_id });
                                          }}
                                          disabled={assignMutation.isPending}
                                          className="block w-full px-2 py-1 text-left text-xs text-gray-200 hover:bg-indigo-600 hover:text-white disabled:opacity-50"
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
                              {unassignMutation.isError && (
                                <p className="mt-1 text-xs text-red-400">{unassignMutation.error?.message ?? "Failed"}</p>
                              )}
                            </div>
                          ) : assignMode === "pick" ? (
                            <div className="space-y-2">
                              <p className="text-xs font-medium text-gray-300">Who is this?</p>
                              {candidatePeople.length > 0 && (
                                <div className="max-h-32 space-y-1 overflow-y-auto">
                                  {candidatePeople.map((p) => (
                                    <button
                                      key={p.person_id}
                                      type="button"
                                      onClick={() => assignMutation.mutate({ faceId: face.face_id, personId: p.person_id })}
                                      disabled={assignMutation.isPending}
                                      className="w-full rounded px-2 py-1 text-left text-xs text-gray-200 hover:bg-gray-700"
                                    >
                                      {p.display_name} <span className="text-gray-500">({p.face_count})</span>
                                    </button>
                                  ))}
                                </div>
                              )}
                              {/* Search the full people list — the
                                  nearest ranking only surfaces the top 8,
                                  so partial profiles / occluded faces
                                  need a name escape hatch. */}
                              <div className="relative">
                                <input
                                  type="text"
                                  value={assignSearch}
                                  onChange={(e) => setAssignSearch(e.target.value)}
                                  placeholder="Search by name..."
                                  className="w-full rounded border border-gray-600 bg-gray-900 px-2 py-1 text-xs text-white focus:border-indigo-500 focus:outline-none"
                                />
                                {assignSearch.trim() && searchResults.length > 0 && (
                                  <div className="absolute left-0 right-0 top-full z-10 mt-1 max-h-40 overflow-y-auto rounded border border-gray-600 bg-gray-900 shadow-lg">
                                    {searchResults.map((p) => (
                                      <button
                                        key={p.person_id}
                                        type="button"
                                        onClick={() => assignMutation.mutate({ faceId: face.face_id, personId: p.person_id })}
                                        disabled={assignMutation.isPending}
                                        className="block w-full px-2 py-1 text-left text-xs text-gray-200 hover:bg-indigo-600 hover:text-white disabled:opacity-50"
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
                                onClick={() => setAssignMode("name")}
                                className="w-full rounded-lg border border-dashed border-gray-600 px-2 py-1.5 text-xs text-indigo-400 hover:border-indigo-500 hover:text-indigo-300"
                              >
                                + New person
                              </button>
                            </div>
                          ) : (
                            <form
                              onSubmit={(e) => {
                                e.preventDefault();
                                if (newPersonName.trim()) {
                                  assignMutation.mutate({ faceId: face.face_id, newPersonName: newPersonName.trim() });
                                }
                              }}
                              className="space-y-2"
                            >
                              <p className="text-xs font-medium text-gray-300">New person name</p>
                              <input
                                type="text"
                                value={newPersonName}
                                onChange={(e) => setNewPersonName(e.target.value)}
                                placeholder="Enter name..."
                                className="w-full rounded-lg border border-gray-600 bg-gray-900 px-2 py-1.5 text-xs text-white focus:border-indigo-500 focus:outline-none"
                                autoFocus
                              />
                              <div className="flex gap-2">
                                <button
                                  type="submit"
                                  disabled={assignMutation.isPending || !newPersonName.trim()}
                                  className="flex-1 rounded-lg bg-indigo-600 px-2 py-1 text-xs text-white hover:bg-indigo-500 disabled:opacity-50"
                                >
                                  {assignMutation.isPending ? "..." : "Create"}
                                </button>
                                <button
                                  type="button"
                                  onClick={() => setAssignMode("pick")}
                                  className="rounded-lg border border-gray-600 px-2 py-1 text-xs text-gray-400 hover:text-white"
                                >
                                  Back
                                </button>
                              </div>
                            </form>
                          )}
                          {assignMutation.isError && (
                            <p className="mt-1 text-xs text-red-400">{assignMutation.error?.message ?? "Failed"}</p>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="text-gray-500">
                {isVideo ? "Preview unavailable" : "Image unavailable"}
              </div>
            )}

            {/* Navigation arrows */}
            {hasPrev && (
              <button
                type="button"
                onClick={() => {
                  setSlideshowActive(false);
                  onNavigate(currentIndex - 1);
                }}
                className="absolute left-0 top-1/2 -translate-y-1/2 ml-safe rounded-full bg-white/10 p-5 min-h-[44px] min-w-[44px] text-white transition-colors hover:bg-white/20"
                aria-label="Previous"
              >
                <svg className="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
                </svg>
              </button>
            )}
            {hasNext && (
              <button
                type="button"
                onClick={() => {
                  setSlideshowActive(false);
                  onNavigate(currentIndex + 1);
                }}
                className="absolute right-0 top-1/2 -translate-y-1/2 mr-safe rounded-full bg-white/10 p-5 min-h-[44px] min-w-[44px] text-white transition-colors hover:bg-white/20"
                aria-label="Next"
              >
                <svg className="h-8 w-8" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
                </svg>
              </button>
            )}
          </div>

          {/* Keyboard hint overlay */}
          <div
            className={`absolute bottom-12 inset-x-0 flex justify-center pointer-events-none transition-opacity duration-500 motion-reduce:transition-none ${
              showHints ? "opacity-100" : "opacity-0"
            }`}
            aria-hidden
          >
            <div className="flex flex-wrap justify-center gap-3 rounded-lg bg-black/60 px-4 py-2 text-xs text-gray-400 backdrop-blur-sm">
              <span><kbd className="font-mono">←/→</kbd> Navigate</span>
              <span><kbd className="font-mono">Esc</kbd> Close</span>
              <span><kbd className="font-mono">Shift+F</kbd> Fullscreen</span>
              <span><kbd className="font-mono">Space</kbd> Slideshow</span>
              <span><kbd className="font-mono">d</kbd> Faces</span>
              <span><kbd className="font-mono">f</kbd> Favorite</span>
              <span><kbd className="font-mono">1-5</kbd> Stars</span>
              <span><kbd className="font-mono">6-9</kbd> Colors</span>
              <span><kbd className="font-mono">?</kbd> Hints</span>
            </div>
          </div>
        </div>

        {/* Right: metadata panel — hidden in fullscreen */}
        {!isFullscreen && (
          <div
            className={`w-full border-t border-gray-700 bg-gray-900/50 p-4 lg:p-6 lg:w-80 lg:border-l lg:border-t-0 transition-[max-height] duration-200 motion-reduce:transition-none ${
              metaOpen
                ? "overflow-y-auto max-h-none"
                : "max-h-[3.5rem] overflow-hidden"
            } lg:overflow-y-auto lg:max-h-none`}
          >
            {/* Details toggle — mobile only */}
            <button
              type="button"
              aria-expanded={metaOpen}
              className="flex w-full items-center justify-between md:hidden mb-4"
              onClick={() => setMetaOpen((o) => !o)}
            >
              <span className="text-sm font-medium text-gray-300">Details</span>
              <svg
                className={`h-4 w-4 text-gray-400 transition-transform motion-reduce:transition-none ${
                  metaOpen ? "" : "rotate-180"
                }`}
                viewBox="0 0 24 24"
                fill="none"
                aria-hidden
              >
                <path
                  d="M5 15l7-7 7 7"
                  stroke="currentColor"
                  strokeWidth="1.7"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            </button>

            <div className="space-y-4">
              {/* Section 1: Basic info */}
              <div>
                <div className="text-lg font-medium text-gray-100">{filename}</div>
                <div className="mt-1 font-mono text-xs text-gray-500">
                  {(() => {
                    const parts = asset.rel_path.split("/");
                    const dirParts = parts.slice(0, -1);
                    if (!onPathClick || dirParts.length === 0) return asset.rel_path;
                    return (
                      <>
                        {dirParts.map((seg, i) => {
                          const path = dirParts.slice(0, i + 1).join("/");
                          return (
                            <span key={path}>
                              {i > 0 && <span className="text-gray-600">/</span>}
                              <button
                                type="button"
                                onClick={() => onPathClick(path)}
                                className="text-gray-400 hover:text-indigo-300 hover:underline"
                              >
                                {seg}
                              </button>
                            </span>
                          );
                        })}
                        <span className="text-gray-600">/</span>
                        <span>{parts[parts.length - 1]}</span>
                      </>
                    );
                  })()}
                </div>
                <div className="mt-2 text-sm text-gray-400">
                  {/* Prefer the asset list-item file_size, but fall back
                      to the detail fetch when the list item is a
                      placeholder (file_size === 0). The cluster review
                      face drill-down constructs minimal AssetPageItems
                      with file_size: 0 because the cluster faces
                      endpoint only returns face_id + asset_id + rel_path
                      — the real bytes only come back via the detail
                      response, which now includes file_size. */}
                  {formatFileSize(
                    asset.file_size || detail?.file_size || 0,
                  )}{" "}
                  · {detail?.media_type ?? asset.media_type}
                </div>
              </div>

              <hr className="border-gray-700" />

              {/* Section 2: AI description */}
              <div>
                <div className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-500">
                  Description
                </div>
                {detailLoading ? (
                  <MetadataSkeleton />
                ) : detail?.ai_description ? (
                  <p className="italic text-gray-300">{detail.ai_description}</p>
                ) : (
                  <p className="text-gray-500">No description yet</p>
                )}
              </div>

              {/* Section: OCR Text */}
              {detail?.ocr_text && (
                <>
                  <hr className="border-gray-700" />
                  <div>
                    <div className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-500">
                      Text in Image
                    </div>
                    <p className="text-sm text-gray-300 whitespace-pre-wrap">{detail.ocr_text}</p>
                  </div>
                </>
              )}

              {/* Section 3: Tags */}
              {(detailLoading ||
                (detail?.ai_tags && detail.ai_tags.length > 0)) && (
                <>
                  <hr className="border-gray-700" />
                  <div>
                    <div className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-500">
                      Tags
                    </div>
                    {detailLoading && !detail ? (
                      <MetadataSkeleton />
                    ) : (
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        {detail?.ai_tags?.map((tag) =>
                          onTagClick ? (
                            <button
                              key={tag}
                              type="button"
                              onClick={() => {
                                onTagClick(tag);
                                onClose();
                              }}
                              className="rounded-full bg-gray-700/60 px-2.5 py-0.5 text-xs text-gray-300 hover:bg-indigo-600/40 hover:text-indigo-200 transition-colors cursor-pointer"
                            >
                              {tag}
                            </button>
                          ) : (
                            <span
                              key={tag}
                              className="rounded-full bg-gray-700/60 px-2.5 py-0.5 text-xs text-gray-300"
                            >
                              {tag}
                            </span>
                          ),
                        )}
                      </div>
                    )}
                  </div>
                </>
              )}

              {/* Section: Transcript (video only) */}
              {detail?.media_type === "video" && (
                <>
                  <hr className="border-gray-700" />
                  <div>
                    <div className="mb-1 flex items-center justify-between">
                      <span className="text-xs font-medium uppercase tracking-wide text-gray-500">
                        Transcript
                        {detail.transcript_language && (
                          <span className="ml-1 normal-case">({detail.transcript_language})</span>
                        )}
                      </span>
                      {detail.transcript_srt && (
                        <div className="flex gap-2">
                          <button
                            type="button"
                            className="text-xs text-indigo-400 hover:text-indigo-300"
                            onClick={() => {
                              const blob = new Blob([detail.transcript_srt!], { type: "text/srt" });
                              const url = URL.createObjectURL(blob);
                              const a = document.createElement("a");
                              const stem = asset.rel_path.replace(/\.[^.]+$/, "").split("/").pop() || "transcript";
                              a.href = url;
                              a.download = `${stem}.srt`;
                              a.click();
                              URL.revokeObjectURL(url);
                            }}
                          >
                            Download
                          </button>
                          <label className="cursor-pointer text-xs text-indigo-400 hover:text-indigo-300">
                            Replace
                            <input
                              type="file"
                              accept=".srt"
                              className="hidden"
                              onChange={async (e) => {
                                const file = e.target.files?.[0];
                                if (!file) return;
                                const text = await file.text();
                                await uploadTranscript(asset.asset_id, text);
                                queryClient.invalidateQueries({ queryKey: ["asset", asset.asset_id] });
                                e.target.value = "";
                              }}
                            />
                          </label>
                          <button
                            type="button"
                            className="text-xs text-red-400 hover:text-red-300"
                            onClick={async () => {
                              await deleteTranscript(asset.asset_id);
                              queryClient.invalidateQueries({ queryKey: ["asset", asset.asset_id] });
                            }}
                          >
                            Remove
                          </button>
                        </div>
                      )}
                    </div>
                    {detailLoading ? (
                      <MetadataSkeleton />
                    ) : detail?.transcript_srt ? (
                      <TranscriptViewer srt={detail.transcript_srt} />
                    ) : (
                      <label className="inline-flex cursor-pointer items-center gap-1.5 rounded bg-gray-700/60 px-3 py-1.5 text-xs text-gray-300 hover:bg-indigo-600/40 hover:text-indigo-200 transition-colors">
                        Upload SRT
                        <input
                          type="file"
                          accept=".srt"
                          className="hidden"
                          onChange={async (e) => {
                            const file = e.target.files?.[0];
                            if (!file) return;
                            const text = await file.text();
                            await uploadTranscript(asset.asset_id, text);
                            queryClient.invalidateQueries({ queryKey: ["asset", asset.asset_id] });
                            e.target.value = "";
                          }}
                        />
                      </label>
                    )}
                  </div>
                </>
              )}

              {/* Section: Rating */}
              {onRatingChange && (
                <>
                  <hr className="border-gray-700" />
                  <div className="space-y-2.5">
                    <div className="mb-1 text-xs font-medium uppercase tracking-wide text-gray-500">
                      Rating
                    </div>
                    <div className="flex items-center gap-3">
                      <HeartButton
                        favorite={rating?.favorite ?? false}
                        onClick={() => onRatingChange(asset.asset_id, { favorite: !(rating?.favorite ?? false) })}
                      />
                      <div className="h-4 w-px bg-gray-700" />
                      <StarPicker
                        stars={rating?.stars ?? 0}
                        onChange={(stars) => onRatingChange(asset.asset_id, { stars })}
                      />
                    </div>
                    <ColorPicker
                      color={rating?.color ?? null}
                      onChange={(color) => onRatingChange(asset.asset_id, { color })}
                    />
                  </div>
                </>
              )}

              {/* Section: Notes */}
              <NoteSection
                asset={asset}
                note={detail?.note ?? null}
                noteAuthor={detail?.note_author ?? null}
                noteUpdatedAt={detail?.note_updated_at ?? null}
                loading={detailLoading}
                queryClient={queryClient}
              />

              {/* Section 4: Details */}
              <hr className="border-gray-700" />
              <div>
                <div className="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500">
                  Details
                </div>
                {detailLoading ? (
                  <MetadataSkeleton />
                ) : (
                  <dl className="space-y-2">
                    {(detail?.taken_at || asset.file_mtime) && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">
                          {detail?.taken_at ? "Taken" : "File date"}
                        </dt>
                        <dd className="w-3/5 text-sm">
                          {onDateClick ? (
                            <button
                              type="button"
                              onClick={() =>
                                onDateClick(
                                  (detail?.taken_at ?? asset.file_mtime)!.slice(0, 10),
                                )
                              }
                              title="Filter to this date"
                              className="text-gray-300 hover:text-indigo-400 hover:underline transition-colors text-left"
                            >
                              {formatDate(detail?.taken_at ?? asset.file_mtime)}
                            </button>
                          ) : (
                            <span className="text-gray-300">
                              {formatDate(detail?.taken_at ?? asset.file_mtime)}
                            </span>
                          )}
                        </dd>
                      </div>
                    )}
                    {detail &&
                      (detail.camera_make || detail.camera_model) && (
                        <div className="flex">
                          <dt className="w-2/5 text-xs text-gray-500">Camera</dt>
                          <dd className="w-3/5 text-sm">
                            <FilterLink
                              params={{
                                ...(detail.camera_make
                                  ? { camera_make: detail.camera_make }
                                  : {}),
                                ...(detail.camera_model
                                  ? { camera_model: detail.camera_model }
                                  : {}),
                              }}
                              onFilterClick={onFilterClick}
                              onClose={onClose}
                              title="Filter by this camera"
                            >
                              {[detail.camera_make, detail.camera_model]
                                .filter(Boolean)
                                .join(" ")}
                            </FilterLink>
                          </dd>
                        </div>
                      )}
                    {detail && detail.lens_model && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">Lens</dt>
                        <dd className="w-3/5 text-sm">
                          <FilterLink
                            params={{ lens_model: detail.lens_model }}
                            onFilterClick={onFilterClick}
                            onClose={onClose}
                            title="Filter by this lens"
                          >
                            {detail.lens_model}
                          </FilterLink>
                        </dd>
                      </div>
                    )}
                    {detail && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">Exposure</dt>
                        <dd className="w-3/5 text-sm">
                          {detail.exposure_time_us != null ||
                          detail.aperture != null ||
                          detail.iso != null ? (
                            <span className="flex flex-wrap items-center gap-x-1.5">
                              {onFilterClick ? (
                                <button
                                  type="button"
                                  onClick={() => {
                                    const p: Record<string, string> = {};
                                    if (detail.exposure_time_us != null) {
                                      p.exposure_min_us = String(detail.exposure_time_us);
                                      p.exposure_max_us = String(detail.exposure_time_us);
                                    }
                                    if (detail.iso != null) {
                                      p.iso_min = String(detail.iso);
                                      p.iso_max = String(detail.iso);
                                    }
                                    if (detail.aperture != null) {
                                      p.aperture_min = String(detail.aperture);
                                      p.aperture_max = String(detail.aperture);
                                    }
                                    onFilterClick(p);
                                    onClose();
                                  }}
                                  title="Filter by all exposure settings"
                                  className="text-gray-500 hover:text-indigo-400 transition-colors text-xs mr-0.5"
                                >
                                  ▸
                                </button>
                              ) : null}
                              {detail.exposure_time_us != null && (
                                <FilterLink
                                  params={{
                                    exposure_min_us: String(detail.exposure_time_us),
                                    exposure_max_us: String(detail.exposure_time_us),
                                  }}
                                  onFilterClick={onFilterClick}
                                  onClose={onClose}
                                  title="Filter by this shutter speed"
                                >
                                  {formatExposure(detail.exposure_time_us)}
                                </FilterLink>
                              )}
                              {detail.aperture != null && (
                                <FilterLink
                                  params={{
                                    aperture_min: String(detail.aperture),
                                    aperture_max: String(detail.aperture),
                                  }}
                                  onFilterClick={onFilterClick}
                                  onClose={onClose}
                                  title="Filter by this aperture"
                                >
                                  f/{detail.aperture}
                                </FilterLink>
                              )}
                              {detail.iso != null && (
                                <FilterLink
                                  params={{
                                    iso_min: String(detail.iso),
                                    iso_max: String(detail.iso),
                                  }}
                                  onFilterClick={onFilterClick}
                                  onClose={onClose}
                                  title="Filter by this ISO"
                                >
                                  ISO {detail.iso}
                                </FilterLink>
                              )}
                            </span>
                          ) : (
                            <FilterLink
                              params={{ has_exposure: "false" }}
                              onFilterClick={onFilterClick}
                              onClose={onClose}
                              title="Find images with no exposure data"
                            >
                              Unknown
                            </FilterLink>
                          )}
                        </dd>
                      </div>
                    )}
                    {detail &&
                      detail.focal_length != null && (
                        <div className="flex">
                          <dt className="w-2/5 text-xs text-gray-500">
                            Focal length
                          </dt>
                          <dd className="w-3/5 text-sm">
                            <FilterLink
                              params={{
                                focal_length_min: String(detail.focal_length),
                                focal_length_max: String(detail.focal_length),
                              }}
                              onFilterClick={onFilterClick}
                              onClose={onClose}
                              title="Filter by this focal length"
                            >
                              {detail.focal_length}mm
                              {detail.focal_length_35mm != null &&
                                detail.focal_length_35mm !== detail.focal_length &&
                                ` (${detail.focal_length_35mm}mm eq.)`}
                            </FilterLink>
                          </dd>
                        </div>
                      )}
                    {detail && detail.flash_fired != null && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">Flash</dt>
                        <dd className="w-3/5 text-sm text-gray-300">
                          {detail.flash_fired ? "Fired" : "No flash"}
                        </dd>
                      </div>
                    )}
                    {detail &&
                      detail.width != null &&
                      detail.height != null && (
                        <div className="flex">
                          <dt className="w-2/5 text-xs text-gray-500">
                            Dimensions
                          </dt>
                          <dd className="w-3/5 text-sm text-gray-300">
                            {detail.width} × {detail.height}
                          </dd>
                        </div>
                      )}
                    {detail &&
                      detail.gps_lat != null &&
                      detail.gps_lon != null && (
                        <>
                          <div className="flex">
                            <dt className="w-2/5 text-xs text-gray-500">GPS</dt>
                            <dd className="w-3/5 text-sm">
                              <a
                                href={`https://maps.google.com/?q=${detail.gps_lat},${detail.gps_lon}`}
                                target="_blank"
                                rel="noopener noreferrer"
                                title={formatGps(detail.gps_lat, detail.gps_lon)}
                                className="text-indigo-400 hover:text-indigo-300 hover:underline transition-colors"
                              >
                                {formatGps(detail.gps_lat, detail.gps_lon)}
                              </a>
                            </dd>
                          </div>
                          {onNearbyClick && (
                            <div className="flex">
                              <dt className="w-2/5" />
                              <dd className="w-3/5">
                                <button
                                  type="button"
                                  onClick={() => onNearbyClick(detail.gps_lat!, detail.gps_lon!)}
                                  className="text-xs text-indigo-400 hover:text-indigo-300 hover:underline transition-colors"
                                >
                                  Photos nearby
                                </button>
                              </dd>
                            </div>
                          )}
                        </>
                      )}
                    <div className="flex">
                      <dt className="w-2/5 text-xs text-gray-500">File size</dt>
                      <dd className="w-3/5 text-sm text-gray-300">
                        {formatFileSize(
                          asset.file_size || detail?.file_size || 0,
                        )}
                      </dd>
                    </div>
                    {(detail?.sha256 || asset.sha256) && (
                      <div className="flex">
                        <dt className="w-2/5 text-xs text-gray-500">SHA256</dt>
                        <dd
                          className="w-3/5 font-mono text-xs text-gray-400"
                          title={
                            (detail?.sha256 || asset.sha256) ?? undefined
                          }
                        >
                          {(detail?.sha256 || asset.sha256)?.slice(0, 16)}…
                        </dd>
                      </div>
                    )}
                  </dl>
                )}
              </div>

              {onAddToCollection && (
                <>
                  <hr className="border-gray-700" />
                  <button
                    type="button"
                    onClick={() => onAddToCollection(asset.asset_id)}
                    className="w-full rounded-lg border border-gray-700 bg-gray-800/50 px-3 py-2 text-sm text-gray-300 transition-colors hover:bg-gray-700 hover:text-gray-100"
                  >
                    Add to collection
                  </button>
                </>
              )}

              {hasFaces && (
                <>
                  <hr className="border-gray-700" />
                  <button
                    type="button"
                    onClick={() => setShowFaces(!showFaces)}
                    className="w-full rounded-lg border border-gray-700 bg-gray-800/50 px-3 py-2 text-sm text-gray-300 transition-colors hover:bg-gray-700 hover:text-gray-100"
                  >
                    {showFaces ? "Hide faces" : "Show faces"}
                  </button>
                </>
              )}

              {libraryId && (
                <>
                  <hr className="border-gray-700" />
                  <button
                    type="button"
                    onClick={() => setShowSimilar((s) => !s)}
                    className="w-full rounded-lg border border-gray-700 bg-gray-800/50 px-3 py-2 text-sm text-gray-300 transition-colors hover:bg-gray-700 hover:text-gray-100"
                  >
                    {showSimilar ? "Hide similar" : "Find similar"}
                  </button>
                  {showSimilar && (
                    <div className="space-y-2">
                      {similarLoading && (
                        <div className="grid grid-cols-2 gap-2">
                          {[1, 2, 3, 4].map((i) => (
                            <div
                              key={i}
                              className="aspect-square animate-pulse rounded-lg bg-gray-700"
                            />
                          ))}
                        </div>
                      )}
                      {!similarLoading &&
                        !similarData?.embedding_available && (
                          <p className="text-xs text-gray-500">
                            No visual embedding yet for this image.
                          </p>
                        )}
                      {!similarLoading &&
                        similarData?.embedding_available &&
                        similarData.hits.length === 0 && (
                          <p className="text-xs text-gray-500">
                            No similar images found.
                          </p>
                        )}
                      {!similarLoading &&
                        similarData?.embedding_available &&
                        similarData.hits.length > 0 && (
                          <div className="grid grid-cols-2 gap-2">
                            {similarData.hits.map((hit) => (
                              <SimilarThumbnail
                                key={hit.asset_id}
                                hit={hit}
                                isPublic={isPublic}
                                publicLibraryId={publicLibraryId ?? libraryId}
                                onClick={() => {
                                  if (onSimilarClick) {
                                    onSimilarClick({
                                      asset_id: hit.asset_id,
                                      rel_path: hit.rel_path,
                                      file_size: hit.file_size ?? 0,
                                      file_mtime: null,
                                      sha256: null,
                                      media_type:
                                        hit.media_type ?? "image/jpeg",
                                      width: hit.width ?? null,
                                      height: hit.height ?? null,
                                      taken_at: null,
                                      status: "indexed",
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
                                      created_at: null,
                                    });
                                  }
                                }}
                              />
                            ))}
                          </div>
                        )}
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
