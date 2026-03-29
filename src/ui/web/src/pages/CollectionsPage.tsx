import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listCollections,
  createCollection,
  deleteCollection,
  ApiError,
} from "../api/client";
import { useAuthenticatedImage } from "../api/useAuthenticatedImage";
import { Modal } from "../components/Modal";
import { SkeletonRow } from "../components/SkeletonRow";
import type { CollectionItem } from "../api/types";

function CollectionCard({
  collection,
  onDelete,
  deleteConfirmId,
  setDeleteConfirmId,
  isDeleting,
}: {
  collection: CollectionItem;
  onDelete: (id: string) => void;
  deleteConfirmId: string | null;
  setDeleteConfirmId: (id: string | null) => void;
  isDeleting: boolean;
}) {
  const { url: coverUrl } = useAuthenticatedImage(
    collection.cover_asset_id ?? "",
    "thumbnail",
    { enabled: !!collection.cover_asset_id },
  );

  return (
    <div className="group relative overflow-hidden rounded-lg border border-gray-700/50 bg-gray-900/50 transition-colors duration-150 hover:border-gray-600/50">
      <Link to={`/collections/${collection.collection_id}`}>
        <div className="aspect-[4/3] bg-gray-800">
          {coverUrl ? (
            <img
              src={coverUrl}
              alt={collection.name}
              className="h-full w-full object-cover"
            />
          ) : (
            <div className="flex h-full w-full items-center justify-center text-gray-600">
              <svg
                className="h-12 w-12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                aria-hidden
              >
                <rect x="3" y="3" width="18" height="18" rx="2" />
                <path d="M3 15l5-5 4 4 4-6 5 7" />
              </svg>
            </div>
          )}
        </div>
      </Link>
      <div className="p-3">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <Link
              to={`/collections/${collection.collection_id}`}
              className="block truncate font-medium text-gray-100 hover:text-indigo-300"
            >
              {collection.name}
            </Link>
            <div className="mt-0.5 flex items-center gap-2">
              <span className="text-xs text-gray-500">
                {collection.asset_count}{" "}
                {collection.asset_count === 1 ? "item" : "items"}
              </span>
              {collection.ownership === "shared" && (
                <span className="rounded bg-gray-700/60 px-1.5 py-0.5 text-[10px] text-gray-400">
                  Shared
                </span>
              )}
              {collection.visibility === "public" && (
                <span className="rounded bg-indigo-900/40 px-1.5 py-0.5 text-[10px] text-indigo-400">
                  Public
                </span>
              )}
            </div>
          </div>
          <div className="shrink-0">
            {deleteConfirmId === collection.collection_id ? (
              <div className="flex items-center gap-1">
                <button
                  type="button"
                  onClick={() => onDelete(collection.collection_id)}
                  disabled={isDeleting}
                  className="rounded px-2 py-1 text-xs font-medium text-red-400 hover:bg-red-900/30"
                >
                  Confirm
                </button>
                <button
                  type="button"
                  onClick={() => setDeleteConfirmId(null)}
                  className="rounded px-2 py-1 text-xs text-gray-400 hover:text-gray-300"
                >
                  Cancel
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => setDeleteConfirmId(collection.collection_id)}
                className="rounded px-2 py-1 text-xs text-gray-500 opacity-0 transition-opacity group-hover:opacity-100 hover:text-red-400"
              >
                Delete
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function CollectionsPage() {
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const [createDesc, setCreateDesc] = useState("");
  const [createError, setCreateError] = useState("");
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  const { data: collections, isLoading, error } = useQuery({
    queryKey: ["collections"],
    queryFn: listCollections,
    refetchInterval: 10_000,
  });

  const createMutation = useMutation({
    mutationFn: () =>
      createCollection(createName.trim(), {
        description: createDesc.trim() || undefined,
      }),
    onSuccess: () => {
      setCreateOpen(false);
      setCreateName("");
      setCreateDesc("");
      setCreateError("");
      queryClient.invalidateQueries({ queryKey: ["collections"] });
    },
    onError: (err: ApiError) => setCreateError(err.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteCollection(id),
    onSuccess: () => {
      setDeleteConfirmId(null);
      queryClient.invalidateQueries({ queryKey: ["collections"] });
    },
  });

  const handleCreateSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setCreateError("");
    if (!createName.trim()) return;
    createMutation.mutate();
  };

  return (
    <div className="mx-auto max-w-4xl px-6 py-6">
      <div className="space-y-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <h1 className="text-2xl font-semibold">Collections</h1>
          <button
            type="button"
            onClick={() => setCreateOpen(true)}
            className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-indigo-500"
          >
            New collection
          </button>
        </div>

        {error && (
          <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-400">
            {(error as Error).message}
          </div>
        )}

        {isLoading ? (
          <div className="space-y-4">
            <SkeletonRow />
            <SkeletonRow />
          </div>
        ) : collections?.length === 0 ? (
          <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-8 text-center text-gray-400">
            No collections yet. Create one to start curating.
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
            {collections?.map((col) => (
              <CollectionCard
                key={col.collection_id}
                collection={col}
                onDelete={(id) => deleteMutation.mutate(id)}
                deleteConfirmId={deleteConfirmId}
                setDeleteConfirmId={setDeleteConfirmId}
                isDeleting={deleteMutation.isPending}
              />
            ))}
          </div>
        )}
      </div>

      <Modal
        isOpen={createOpen}
        onClose={() => {
          setCreateOpen(false);
          setCreateError("");
        }}
        title="New collection"
      >
        <form onSubmit={handleCreateSubmit} className="space-y-4">
          {createError && (
            <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-3 py-2 text-sm text-red-400">
              {createError}
            </div>
          )}
          <div>
            <label
              htmlFor="col-name"
              className="mb-1 block text-sm text-gray-400"
            >
              Name
            </label>
            <input
              id="col-name"
              type="text"
              value={createName}
              onChange={(e) => setCreateName(e.target.value)}
              placeholder="Best of Europe"
              required
              autoFocus
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div>
            <label
              htmlFor="col-desc"
              className="mb-1 block text-sm text-gray-400"
            >
              Description (optional)
            </label>
            <input
              id="col-desc"
              type="text"
              value={createDesc}
              onChange={(e) => setCreateDesc(e.target.value)}
              placeholder="My favorite travel photos"
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setCreateOpen(false)}
              className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 transition-colors duration-150 hover:bg-gray-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || !createName.trim()}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-indigo-500 disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating..." : "Create"}
            </button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
