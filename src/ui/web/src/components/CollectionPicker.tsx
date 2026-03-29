import { useState, useMemo } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listCollections,
  createCollection,
  addAssetsToCollection,
} from "../api/client";

interface CollectionPickerProps {
  assetIds: string[];
  onClose: () => void;
  onDone?: () => void;
}

export function CollectionPicker({ assetIds, onClose, onDone }: CollectionPickerProps) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");
  const [newName, setNewName] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [adding, setAdding] = useState<string | null>(null);
  const [error, setError] = useState("");

  const { data: collections } = useQuery({
    queryKey: ["collections"],
    queryFn: listCollections,
  });

  const filtered = useMemo(() => {
    if (!collections) return [];
    if (!search.trim()) return collections;
    const q = search.toLowerCase();
    return collections.filter((c) => c.name.toLowerCase().includes(q));
  }, [collections, search]);

  const addMutation = useMutation({
    mutationFn: (collectionId: string) =>
      addAssetsToCollection(collectionId, assetIds),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["collections"] });
      queryClient.invalidateQueries({ queryKey: ["collection-assets"] });
      onDone?.();
      onClose();
    },
    onError: (err: Error) => {
      setError(err.message);
      setAdding(null);
    },
  });

  const createMutation = useMutation({
    mutationFn: () =>
      createCollection(newName.trim(), { asset_ids: assetIds }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["collections"] });
      onDone?.();
      onClose();
    },
    onError: (err: Error) => setError(err.message),
  });

  const handleSelect = (collectionId: string) => {
    setAdding(collectionId);
    setError("");
    addMutation.mutate(collectionId);
  };

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    setError("");
    createMutation.mutate();
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="flex w-full max-w-sm flex-col rounded-xl bg-gray-900 shadow-2xl"
        style={{ maxHeight: "70vh" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-b border-gray-800 px-4 py-3">
          <h2 className="text-sm font-semibold text-gray-100">
            Add {assetIds.length} {assetIds.length === 1 ? "item" : "items"} to collection
          </h2>
        </div>

        {error && (
          <div className="mx-4 mt-3 rounded-lg border border-red-800/50 bg-red-900/20 px-3 py-2 text-xs text-red-400">
            {error}
          </div>
        )}

        <div className="px-4 pt-3">
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search collections..."
            autoFocus
            className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2">
          {/* Create new */}
          {showCreate ? (
            <form onSubmit={handleCreate} className="mx-2 mb-2 rounded-lg border border-gray-700 bg-gray-800/50 p-3">
              <input
                type="text"
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="Collection name"
                autoFocus
                className="mb-2 w-full rounded border border-gray-600 bg-gray-800 px-2 py-1.5 text-sm text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none"
              />
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  onClick={() => setShowCreate(false)}
                  className="rounded px-2 py-1 text-xs text-gray-400 hover:text-gray-300"
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={createMutation.isPending || !newName.trim()}
                  className="rounded bg-indigo-600 px-3 py-1 text-xs font-medium text-white hover:bg-indigo-500 disabled:opacity-50"
                >
                  {createMutation.isPending ? "Creating..." : "Create & add"}
                </button>
              </div>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => setShowCreate(true)}
              className="mb-1 flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-indigo-400 transition-colors hover:bg-gray-800/80"
            >
              <svg
                className="h-4 w-4"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden
              >
                <line x1="12" y1="5" x2="12" y2="19" />
                <line x1="5" y1="12" x2="19" y2="12" />
              </svg>
              Create new collection
            </button>
          )}

          {/* Existing collections */}
          {filtered.length === 0 && !showCreate ? (
            <div className="px-3 py-4 text-center text-xs text-gray-500">
              {search ? "No matching collections" : "No collections yet"}
            </div>
          ) : (
            filtered.map((col) => (
              <button
                key={col.collection_id}
                type="button"
                onClick={() => handleSelect(col.collection_id)}
                disabled={addMutation.isPending}
                className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-sm text-gray-300 transition-colors hover:bg-gray-800/80 disabled:opacity-50"
              >
                <span className="truncate">{col.name}</span>
                <span className="shrink-0 text-xs text-gray-600">
                  {adding === col.collection_id
                    ? "Adding..."
                    : `${col.asset_count} items`}
                </span>
              </button>
            ))
          )}
        </div>

        <div className="border-t border-gray-800 px-4 py-2">
          <button
            type="button"
            onClick={onClose}
            className="w-full rounded-lg py-1.5 text-sm text-gray-400 hover:text-gray-300"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
