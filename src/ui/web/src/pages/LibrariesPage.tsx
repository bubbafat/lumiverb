import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  listLibraries,
  createLibrary,
  deleteLibrary,
  emptyTrash,
  ApiError,
  updateLibraryVisibility,
} from "../api/client";
import { Badge } from "../components/Badge";
import { Modal } from "../components/Modal";
import { SkeletonRow } from "../components/SkeletonRow";

function formatLastIngest(lastScanAt: string | null): string {
  if (!lastScanAt) return "Never ingested";
  const d = new Date(lastScanAt);
  const now = new Date();
  const diffMs = now.getTime() - d.getTime();
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));
  if (diffDays === 0) return "Today";
  if (diffDays === 1) return "Yesterday";
  if (diffDays < 7) return `${diffDays} days ago`;
  if (diffDays < 30) return `${Math.floor(diffDays / 7)} weeks ago`;
  return `${Math.floor(diffDays / 30)} months ago`;
}

export default function LibrariesPage() {
  const queryClient = useQueryClient();
  const [addOpen, setAddOpen] = useState(false);
  const [addName, setAddName] = useState("");
  const [addPath, setAddPath] = useState("");
  const [addError, setAddError] = useState("");
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);
  const [emptyTrashConfirm, setEmptyTrashConfirm] = useState(false);
  const [visibilityUpdatingId, setVisibilityUpdatingId] = useState<
    string | null
  >(null);
  const [visibilityError, setVisibilityError] = useState<string | null>(null);

  const { data: libraries, isLoading, error } = useQuery({
    queryKey: ["libraries", true],
    queryFn: () => listLibraries(true),
    refetchInterval: 10_000,
  });

  const createMutation = useMutation({
    mutationFn: () => createLibrary(addName.trim(), addPath.trim()),
    onSuccess: () => {
      setAddOpen(false);
      setAddName("");
      setAddPath("");
      setAddError("");
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
    },
    onError: (err: ApiError) => {
      setAddError(err.message);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteLibrary(id),
    onSuccess: () => {
      setDeleteConfirmId(null);
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
    },
  });

  const emptyTrashMutation = useMutation({
    mutationFn: emptyTrash,
    onSuccess: () => {
      setEmptyTrashConfirm(false);
      queryClient.invalidateQueries({ queryKey: ["libraries"] });
    },
  });

  const visibilityMutation = useMutation({
    mutationFn: (vars: {
      libraryId: string;
      is_public: boolean;
    }) => updateLibraryVisibility(vars.libraryId, vars.is_public),
    onMutate: (vars) => {
      setVisibilityUpdatingId(vars.libraryId);
      setVisibilityError(null);
    },
    onSuccess: () => {
      setVisibilityUpdatingId(null);
      queryClient.invalidateQueries({ queryKey: ["libraries", true] });
    },
    onError: (err: ApiError) => {
      setVisibilityUpdatingId(null);
      setVisibilityError(err.message);
    },
  });

  async function copyShareLink(libraryId: string): Promise<void> {
    const url = `${window.location.origin}/libraries/${libraryId}/browse`;
    try {
      await navigator.clipboard.writeText(url);
    } catch {
      window.prompt("Copy link", url);
    }
  }

  const trashedCount = libraries?.filter((l) => l.status === "trashed").length ?? 0;

  const handleAddSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setAddError("");
    if (!addName.trim() || !addPath.trim()) return;
    createMutation.mutate();
  };

  return (
    <div className="mx-auto max-w-3xl px-6 py-6">
      <div className="space-y-6">
        <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
          <h1 className="text-2xl font-semibold">Libraries</h1>
          <div className="flex items-center gap-3">
            {trashedCount > 0 && (
              <button
                type="button"
                onClick={() => setEmptyTrashConfirm(true)}
                className="rounded-lg border border-amber-700/50 bg-amber-900/20 px-4 py-2 text-sm font-medium text-amber-400 transition-colors duration-150 hover:bg-amber-900/40"
              >
                Empty trash ({trashedCount})
              </button>
            )}
            <button
              type="button"
              onClick={() => setAddOpen(true)}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-indigo-500"
            >
              Add library
            </button>
          </div>
        </div>

        {error && (
          <div className="flex items-center justify-between rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-400">
            <span>{(error as Error).message}</span>
          </div>
        )}
        {visibilityError && (
          <div className="flex items-center justify-between rounded-lg border border-red-800/50 bg-red-900/20 px-4 py-3 text-red-400">
            <span>{visibilityError}</span>
          </div>
        )}

        {isLoading ? (
          <div className="space-y-4">
            <SkeletonRow />
            <SkeletonRow />
            <SkeletonRow />
          </div>
        ) : (
          <div className="space-y-4">
            {libraries?.length === 0 ? (
              <div className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-8 text-center text-gray-400">
                No libraries yet. Add one to get started.
              </div>
            ) : (
              libraries?.map((lib) => (
                <div
                  key={lib.library_id}
                  className="rounded-lg border border-gray-700/50 bg-gray-900/50 p-4 transition-colors duration-150 hover:border-gray-600/50"
                >
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-semibold text-gray-100">
                          {lib.name}
                        </span>
                        {lib.status === "trashed" && (
                          <Badge variant="trashed">Trashed</Badge>
                        )}
                      </div>
                      <p className="mt-0.5 font-mono text-sm text-gray-400">
                        {lib.root_path}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-3">
                      {lib.status !== "trashed" && (
                        <>
                          <Link
                            to={`/libraries/${lib.library_id}/browse`}
                            className="rounded-lg border border-gray-600 px-3 py-1.5 text-sm font-medium text-gray-300 transition-colors duration-150 hover:border-gray-500 hover:bg-gray-800/50"
                          >
                            Browse
                          </Link>
                          <Link
                            to={`/libraries/${lib.library_id}/settings`}
                            className="rounded-lg border border-gray-600 px-3 py-1.5 text-sm font-medium text-gray-300 transition-colors duration-150 hover:border-gray-500 hover:bg-gray-800/50"
                          >
                            Settings
                          </Link>
                        </>
                      )}
                      {lib.status !== "trashed" && (
                        <>
                          <button
                            type="button"
                            disabled={
                              visibilityMutation.isPending &&
                              visibilityUpdatingId === lib.library_id
                            }
                            onClick={() => {
                              const currentIsPublic = lib.is_public ?? false;
                              const nextIsPublic = !currentIsPublic;
                              if (nextIsPublic) {
                                const ok = window.confirm(
                                  "Anyone with this link will be able to view this library's photos. Continue?",
                                );
                                if (!ok) return;
                              }
                              visibilityMutation.mutate({
                                libraryId: lib.library_id,
                                is_public: nextIsPublic,
                              });
                            }}
                            className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors duration-150 ${
                              (lib.is_public ?? false)
                                ? "bg-indigo-600 text-white hover:bg-indigo-500"
                                : "border border-gray-600 text-gray-300 hover:bg-gray-800/50 hover:border-gray-500"
                            }`}
                          >
                            {lib.is_public ? "Public" : "Private"}
                          </button>
                          {lib.is_public && (
                            <button
                              type="button"
                              onClick={() => void copyShareLink(lib.library_id)}
                              className="rounded-lg border border-gray-600 bg-gray-900/30 px-3 py-1.5 text-sm font-medium text-gray-300 transition-colors duration-150 hover:border-gray-500 hover:bg-gray-800/50 disabled:opacity-50 disabled:cursor-not-allowed"
                              disabled={
                                visibilityMutation.isPending &&
                                visibilityUpdatingId === lib.library_id
                              }
                            >
                              Copy link
                            </button>
                          )}
                        </>
                      )}
                      <span className="text-sm text-gray-500">
                        {formatLastIngest(lib.last_scan_at)}
                      </span>
                      {lib.status !== "trashed" && (
                        <div>
                          {deleteConfirmId === lib.library_id ? (
                            <div className="flex items-center gap-2">
                              <span className="text-sm text-gray-400">
                                Delete {lib.name}? This moves it to trash. You
                                can recover it until trash is emptied.
                              </span>
                              <button
                                type="button"
                                onClick={() =>
                                  deleteMutation.mutate(lib.library_id)
                                }
                                disabled={deleteMutation.isPending}
                                className="rounded px-2 py-1 text-sm font-medium text-red-400 transition-colors duration-150 hover:bg-red-900/30"
                              >
                                Confirm
                              </button>
                              <button
                                type="button"
                                onClick={() => setDeleteConfirmId(null)}
                                className="rounded px-2 py-1 text-sm text-gray-400 transition-colors duration-150 hover:text-gray-300"
                              >
                                Cancel
                              </button>
                            </div>
                          ) : (
                            <button
                              type="button"
                              onClick={() => setDeleteConfirmId(lib.library_id)}
                              className="rounded px-3 py-1.5 text-sm font-medium text-red-400 transition-colors duration-150 hover:bg-red-900/30"
                            >
                              Delete
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>

      <Modal
        isOpen={addOpen}
        onClose={() => {
          setAddOpen(false);
          setAddError("");
        }}
        title="Add library"
      >
        <form onSubmit={handleAddSubmit} className="space-y-4">
          {addError && (
            <div className="rounded-lg border border-red-800/50 bg-red-900/20 px-3 py-2 text-sm text-red-400">
              {addError}
            </div>
          )}
          <div>
            <label
              htmlFor="lib-name"
              className="mb-1 block text-sm text-gray-400"
            >
              Name
            </label>
            <input
              id="lib-name"
              type="text"
              value={addName}
              onChange={(e) => setAddName(e.target.value)}
              placeholder="My Photos"
              required
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div>
            <label
              htmlFor="lib-path"
              className="mb-1 block text-sm text-gray-400"
            >
              Root path
            </label>
            <input
              id="lib-path"
              type="text"
              value={addPath}
              onChange={(e) => setAddPath(e.target.value)}
              placeholder="/Volumes/Photos/2024"
              required
              className="w-full rounded-lg border border-gray-700 bg-gray-800 px-3 py-2 font-mono text-sm text-gray-100 placeholder-gray-500 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
            />
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              onClick={() => setAddOpen(false)}
              className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 transition-colors duration-150 hover:bg-gray-800"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={createMutation.isPending || !addName.trim() || !addPath.trim()}
              className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-indigo-500 disabled:opacity-50"
            >
              {createMutation.isPending ? "Creating…" : "Create library"}
            </button>
          </div>
        </form>
      </Modal>

      {emptyTrashConfirm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setEmptyTrashConfirm(false)}
        >
          <div
            className="w-full max-w-md rounded-xl bg-gray-900 p-6 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="mb-2 text-lg font-semibold text-gray-100">
              Empty trash
            </h2>
            <p className="mb-4 text-sm text-gray-400">
              This will permanently delete {trashedCount} trashed
              {trashedCount === 1 ? " library" : " libraries"} and all their
              assets. This cannot be undone.
            </p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setEmptyTrashConfirm(false)}
                className="rounded-lg border border-gray-600 px-4 py-2 text-sm font-medium text-gray-300 transition-colors duration-150 hover:bg-gray-800"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={() => emptyTrashMutation.mutate()}
                disabled={emptyTrashMutation.isPending}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white transition-colors duration-150 hover:bg-red-500 disabled:opacity-50"
              >
                {emptyTrashMutation.isPending ? "Deleting…" : "Empty trash"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
