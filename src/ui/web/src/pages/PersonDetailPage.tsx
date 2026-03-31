import { useState, useCallback, useMemo } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { useQuery, useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { getPerson, updatePerson, deletePerson, listPersonFaces } from "../api/client";
import type { PersonFaceItem } from "../api/client";
import type { AssetPageItem } from "../api/types";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import { Lightbox } from "../components/Lightbox";

function FaceThumbnail({ face, onClick }: { face: PersonFaceItem; onClick: () => void }) {
  const { url, isLoading } = useAuthenticatedImage(face.asset_id, "thumbnail");

  return (
    <button
      type="button"
      onClick={onClick}
      className="group relative aspect-square w-full overflow-hidden rounded-lg bg-gray-800 focus:outline-none focus:ring-2 focus:ring-indigo-500"
    >
      {isLoading ? (
        <div className="h-full w-full animate-pulse bg-gray-700" />
      ) : url ? (
        <img src={url} alt={face.rel_path ?? ""} className="h-full w-full object-cover" />
      ) : (
        <div className="flex h-full w-full items-center justify-center text-gray-600">No image</div>
      )}
    </button>
  );
}

export default function PersonDetailPage() {
  const { personId } = useParams<{ personId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const [isEditing, setIsEditing] = useState(false);
  const [editName, setEditName] = useState("");
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null);

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

  const startEditing = useCallback(() => {
    if (personQuery.data) {
      setEditName(personQuery.data.display_name);
      setIsEditing(true);
    }
  }, [personQuery.data]);

  const person = personQuery.data;
  const faces = facesQuery.data?.pages.flatMap((p) => p.items) ?? [];

  // Build minimal AssetPageItem[] for Lightbox
  const lightboxAssets: AssetPageItem[] = useMemo(
    () =>
      faces.map((f) => ({
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
    [faces],
  );

  if (personQuery.isLoading) {
    return (
      <div className="mx-auto max-w-6xl px-4 py-8">
        <div className="h-8 w-48 animate-pulse rounded bg-gray-700" />
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
              className="rounded-lg border border-gray-600 bg-gray-800 px-3 py-1.5 text-lg font-bold text-white focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
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
            className="cursor-pointer text-2xl font-bold text-white hover:text-indigo-400"
            onClick={startEditing}
            title="Click to rename"
          >
            {person.display_name}
          </h1>
        )}

        <span className="text-sm text-gray-500">
          {person.face_count} {person.face_count === 1 ? "photo" : "photos"}
        </span>

        <div className="ml-auto">
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
      </div>

      {/* Photo grid */}
      {faces.length > 0 && (
        <div className="grid grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-5 lg:grid-cols-6">
          {faces.map((face, i) => (
            <FaceThumbnail key={face.face_id} face={face} onClick={() => setLightboxIndex(i)} />
          ))}
        </div>
      )}

      {facesQuery.hasNextPage && (
        <div className="mt-4 text-center">
          <button
            type="button"
            onClick={() => facesQuery.fetchNextPage()}
            disabled={facesQuery.isFetchingNextPage}
            className="rounded-lg border border-gray-700 bg-gray-800/50 px-4 py-2 text-sm text-gray-300 hover:bg-gray-700"
          >
            {facesQuery.isFetchingNextPage ? "Loading..." : "Load more"}
          </button>
        </div>
      )}

      {faces.length === 0 && !facesQuery.isLoading && (
        <p className="text-sm text-gray-500">No photos assigned to this person yet.</p>
      )}

      {lightboxIndex !== null && lightboxAssets[lightboxIndex] && (
        <Lightbox
          asset={lightboxAssets[lightboxIndex]}
          assets={lightboxAssets}
          hasMore={facesQuery.hasNextPage}
          onClose={() => setLightboxIndex(null)}
          onNavigate={(i) => setLightboxIndex(i)}
        />
      )}
    </div>
  );
}
